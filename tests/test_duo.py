import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from duo_mcp import prompts
from duo_mcp.config import Config
from duo_mcp.peers import PeerError, run_peer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError

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
    assert names(build_server("codex", cfg)) == {"ask_codex", "review_with_codex", "continue_codex",
                                                 "get_codex_result", "cancel_codex"}
    monkeypatch.setenv("DUO_MCP_PEER", "1")
    assert names(build_server("codex", cfg)) == set()


def tool(server):
    async def call(name, **args):
        return await server._tool_manager.call_tool(name, args, Context(mcp_server=server))
    return call


def job_id(text):
    return text.split("job_id=")[1].split("]")[0]


def test_background_job_returns_immediately_then_result(cfg, tmp_path):
    async def go():
        call = tool(build_server("codex", cfg))
        started = await call("ask_codex", question="q", cwd=str(tmp_path), background=True)
        jid = job_id(started)
        assert "get_codex_result" in started
        out = await call("get_codex_result", job_id=jid, wait_sec=20)
        assert "fake answer" in out and "session_id=0199aaaa" in out
        assert f"{jid}  ask_codex  done" in await call("get_codex_result")
        # the session is known for follow-ups, like after a blocking call
        again = await call("continue_codex", session_id="0199aaaa-bbbb-cccc-dddd-eeeeffff0000",
                           question="more", background=True)
        assert "fake answer" in await call("get_codex_result", job_id=job_id(again), wait_sec=20)
    run(go())


def test_background_job_status_limit_and_cancel(cfg, record, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "sleep")
    cfg.max_jobs = 1

    async def go():
        call = tool(build_server("codex", cfg))
        jid = job_id(await call("review_with_codex", cwd=str(tmp_path), background=True))
        t0 = time.monotonic()
        status = await call("get_codex_result", job_id=jid, wait_sec=1)
        assert "still running" in status and time.monotonic() - t0 < 5
        with pytest.raises(ToolError, match="max_jobs=1"):
            await call("ask_codex", question="q", cwd=str(tmp_path), background=True)
        assert "cancelled" in await call("cancel_codex", job_id=jid)
        with pytest.raises(ProcessLookupError):
            os.kill(record()["pid"], 0)
        with pytest.raises(ToolError, match="was cancelled"):
            await call("get_codex_result", job_id=jid)
    run(go())


def test_background_job_uses_job_timeout(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "sleep")
    cfg.timeout_sec, cfg.job_timeout_sec = 1, 2

    async def go():
        call = tool(build_server("claude", cfg))
        jid = job_id(await call("ask_claude", question="q", cwd=str(tmp_path), background=True))
        with pytest.raises(ToolError, match="timed out after 2s"):
            await call("get_claude_result", job_id=jid, wait_sec=20)
    run(go())


def test_stdio_shutdown_kills_background_peer(tmp_path, record, monkeypatch):
    """Real MCP stdio: a job outlives its request, and closing the client kills the peer."""
    import sys
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    conf = tmp_path / "config.toml"
    conf.write_text(f'[codex]\ncommand = "{FAKE}"\n')
    env = {**os.environ, "DUO_MCP_CONFIG": str(conf), "FAKE_MODE": "sleep"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "duo_mcp", "for-claude"], env=env)

    async def go():
        async with stdio_client(params) as streams, ClientSession(*streams) as s:
            await s.initialize()
            r = await s.call_tool("ask_codex", {"question": "q", "cwd": str(tmp_path), "background": True})
            jid = job_id(r.content[0].text)
            with anyio.move_on_after(1):  # a get_result request abandoned mid-wait
                await s.call_tool("get_codex_result", {"job_id": jid, "wait_sec": 30})
            r = await s.call_tool("get_codex_result", {"job_id": jid})
            assert "still running" in r.content[0].text
            os.kill(record()["pid"], 0)  # peer alive
    anyio.run(go)
    for _ in range(50):
        try:
            os.kill(record()["pid"], 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    pytest.fail("peer still running after the MCP client closed")
