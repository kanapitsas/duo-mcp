import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from duo_mcp import prompts
from duo_mcp.config import Config
from duo_mcp.peers import PeerError, run_peer
from duo_mcp.server import build_server

FAKE = str(Path(__file__).with_name("fake_peer.py"))


@pytest.fixture
def cfg():
    c = Config(timeout_sec=30, max_output_chars=1000)
    c.claude.command = c.codex.command = FAKE
    return c


@pytest.fixture
def record(tmp_path, monkeypatch):
    path = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(path))
    return lambda: json.loads(path.read_text())


def run(coro):
    return asyncio.run(coro)


def test_independent_prompt_hides_hypothesis_framing():
    p = prompts.ask_prompt("Codex", "Claude", "Why does test_x fail?", "independent", "Traceback: KeyError 'a'")
    assert "deliberately NOT shared its own hypothesis" in p
    assert "facts they supplied; verify them" in p
    assert "challenged" not in p


def test_debate_prompt_frames_position_for_challenge():
    p = prompts.ask_prompt("Codex", "Claude", "Is the cache the culprit?", "debate", "I think the TTL is off by one")
    assert "wants it challenged" in p and "I think the TTL is off by one" in p


def test_review_prompt_makes_peer_inspect_repo():
    p = prompts.review_prompt("Claude", "Codex", None, "main")
    assert "git status" in p and "git diff main...HEAD" in p and "Do not rely on any summary" in p


def test_claude_ok_and_flags(cfg, record, tmp_path):
    r = run(run_peer(cfg, "claude", "hello", str(tmp_path), model="opus", effort="high"))
    assert r.text == "fake answer" and r.session_id.startswith("1111") and r.model == "claude-fake-1"
    rec = record()
    assert rec["prompt"] == "hello" and rec["peer_env"] == "1" and rec["cwd"] == str(tmp_path.resolve())
    argv = rec["argv"]
    for flag in ("--strict-mcp-config", "--restricted", "dontAsk"):
        assert flag in argv
    assert argv[argv.index("--model") + 1] == "opus" and argv[argv.index("--effort") + 1] == "high"


def test_codex_ok_disables_mcp_and_sandboxes(cfg, record, tmp_path, monkeypatch):
    home = tmp_path / "codexhome"
    home.mkdir()
    (home / "config.toml").write_text(
        '[mcp_servers.duo]\ncommand = "duo-mcp"\n[mcp_servers.off]\ncommand = "x"\nenabled = false\n'
        '[plugins."computer-use@openai-bundled"]\nenabled = true\n')
    monkeypatch.setenv("CODEX_HOME", str(home))
    r = run(run_peer(cfg, "codex", "hello", str(tmp_path), model="m1", effort="high"))
    assert r.text == "fake answer" and r.session_id == "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"
    argv = record()["argv"]
    assert "mcp_servers.duo.enabled=false" in argv and "mcp_servers.off.enabled=false" not in argv
    assert "plugins.computer-use@openai-bundled.enabled=false" in argv
    assert argv[argv.index("-s") + 1] == "read-only" and argv[-1] == "-"
    assert 'model_reasoning_effort="high"' in argv and "multi_agent" in argv


def test_codex_resume(cfg, record, tmp_path):
    sid = "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"
    r = run(run_peer(cfg, "codex", "again", str(tmp_path), resume=sid))
    argv = record()["argv"]
    assert argv[:2] == ["exec", "resume"] and argv[-2:] == [sid, "-"] and r.session_id == sid


def test_bad_session_id_rejected(cfg, tmp_path):
    with pytest.raises(PeerError, match="invalid session_id"):
        run(run_peer(cfg, "codex", "x", str(tmp_path), resume="--last"))


def test_invalid_effort_rejected(cfg, tmp_path):
    with pytest.raises(PeerError, match="valid: low, medium"):
        run(run_peer(cfg, "claude", "x", str(tmp_path), effort="bogus"))


def test_model_allowlist(cfg, tmp_path):
    cfg.codex.allowed_models = ["a"]
    with pytest.raises(PeerError, match="allowed_models"):
        run(run_peer(cfg, "codex", "x", str(tmp_path), model="b"))


def test_nonzero_exit_surfaces_stderr(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")
    with pytest.raises(PeerError, match="boom: something broke"):
        run(run_peer(cfg, "codex", "x", str(tmp_path)))


def test_output_truncated(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "big")
    r = run(run_peer(cfg, "claude", "x", str(tmp_path)))
    assert len(r.text) < 1200 and "truncated at 1000 of 50000" in r.text


def test_timeout_kills_process(cfg, record, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "sleep")
    cfg.timeout_sec = 2
    t0 = time.monotonic()
    with pytest.raises(PeerError, match="timed out after 2s"):
        run(run_peer(cfg, "claude", "x", str(tmp_path)))
    assert time.monotonic() - t0 < 10
    with pytest.raises(ProcessLookupError):
        os.kill(record()["pid"], 0)


def test_cancellation_kills_process(cfg, record, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "sleep")

    async def go():
        task = asyncio.create_task(run_peer(cfg, "codex", "x", str(tmp_path)))
        await asyncio.sleep(1.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(go())
    with pytest.raises(ProcessLookupError):
        os.kill(record()["pid"], 0)


def test_recursion_guard_exposes_no_tools(cfg, monkeypatch):
    names = lambda s: {t.name for t in s._tool_manager.list_tools()}
    assert names(build_server("codex", cfg)) == {"ask_codex", "review_with_codex", "continue_codex"}
    monkeypatch.setenv("DUO_MCP_PEER", "1")
    assert names(build_server("codex", cfg)) == set()
