"""Run the peer CLI (claude -p / codex exec) headlessly and bounded."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Awaitable, Callable

from .config import Config, PeerConfig

log = logging.getLogger("duo-mcp")

PEER_ENV = "DUO_MCP_PEER"  # set in every peer process; a duo-mcp started under it exposes no tools
HOME = Path.home()
EXTRA_PATH = [str(HOME / ".local/bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
CANDIDATES = {
    "claude": [HOME / ".local/bin/claude", HOME / ".claude/local/claude",
               Path("/opt/homebrew/bin/claude"), Path("/usr/local/bin/claude")],
    "codex": [Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
              Path("/Applications/Codex.app/Contents/Resources/codex"),
              HOME / ".local/bin/codex", Path("/opt/homebrew/bin/codex"), Path("/usr/local/bin/codex")],
}
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,63}$")

_active: set[int] = set()  # process-group ids of running peers


class PeerError(Exception):
    """An error worth showing to the calling agent verbatim."""


@dataclass
class PeerResult:
    text: str
    session_id: str | None
    model: str | None
    seconds: float


# ---------------------------------------------------------------- helpers

def child_env(peer: str) -> dict[str, str]:
    env = dict(os.environ)
    env[PEER_ENV] = "1"
    # GUI-launched MCP servers often get a minimal PATH; peers need git, test runners, node...
    parts = env.get("PATH", "").split(":")
    env["PATH"] = ":".join(parts + [p for p in EXTRA_PATH if p not in parts])
    if peer == "claude":
        # Markers of an enclosing Claude Code session; the peer is a fresh, separate session.
        for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            env.pop(key, None)
    return env


def find_binary(peer: str, pc: PeerConfig) -> str:
    if pc.command:
        path = Path(pc.command).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise PeerError(f"configured [{peer}].command not executable: {pc.command}")
    found = shutil.which(peer, path=child_env(peer)["PATH"])
    if found:
        return found
    for cand in CANDIDATES[peer]:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    raise PeerError(f"`{peer}` CLI not found. Install it or set [{peer}].command in the duo-mcp config.")


@lru_cache(maxsize=4)
def claude_efforts(binary: str) -> tuple[str, ...]:
    """Effort levels as advertised by `claude --help` (the CLI only warns on bad values)."""
    try:
        out = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ()
    m = re.search(r"--effort <level>[^(]*\(([^)]*)\)", out)
    return tuple(v.strip() for v in m.group(1).split(",")) if m else ()


def codex_models() -> dict[str, list[str]]:
    """Model slug -> supported effort levels, from Codex's own model cache (if present)."""
    path = Path(os.environ.get("CODEX_HOME", HOME / ".codex")) / "models_cache.json"
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    models = data.get("models", data) if isinstance(data, dict) else data
    out = {}
    for m in models if isinstance(models, list) else []:
        if isinstance(m, dict) and m.get("slug"):
            out[m["slug"]] = [e.get("effort") for e in m.get("supported_reasoning_levels", []) if isinstance(e, dict)]
    return out


def resolve_model_effort(peer: str, pc: PeerConfig, binary: str, model: str | None, effort: str | None):
    model = (model or pc.model or "").strip() or None
    effort = (effort or pc.effort or "").strip() or None
    if model and pc.allowed_models and model not in pc.allowed_models:
        raise PeerError(f"model {model!r} not in [{peer}].allowed_models {pc.allowed_models}")
    if effort:
        if peer == "claude":
            valid = claude_efforts(binary)
        else:
            valid = codex_models().get(model or "", [])
        if valid and effort not in valid:
            raise PeerError(f"invalid effort {effort!r} for {peer}{' model ' + model if model else ''}; valid: {', '.join(valid)}")
    return model, effort


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[duo-mcp: output truncated at {limit} of {len(text)} chars]"


def tail(path: Path, n: int = 1500) -> str:
    try:
        data = path.read_bytes()[-n:].decode("utf-8", "replace").strip()
    except OSError:
        return ""
    return data


def kill_group(pgid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def kill_all() -> None:
    for pgid in list(_active):
        kill_group(pgid, signal.SIGKILL)


# ---------------------------------------------------------------- process runner

async def run_process(argv: list[str], stdin: str, cwd: str, env: dict, timeout: int, workdir: Path,
                      heartbeat: Callable[[float], Awaitable[None]] | None) -> tuple[int, Path, Path]:
    """Run argv in its own process group. stdout/stderr go to files (no pipe limits)."""
    out_path, err_path = workdir / "stdout", workdir / "stderr"
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE, stdout=out, stderr=err,
            start_new_session=True)
    assert proc.stdin is not None
    pgid = proc.pid
    _active.add(pgid)
    started = time.monotonic()
    try:
        try:
            proc.stdin.write(stdin.encode())
            await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the peer died immediately; its exit code and stderr tell the story
        while True:
            remaining = timeout - (time.monotonic() - started)
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=max(0.1, min(15, remaining)))
                break
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - started
                if elapsed >= timeout:
                    raise PeerError(f"peer timed out after {timeout}s and was killed "
                                    f"(ask a narrower question, or raise the timeout in the duo-mcp config)")
                if heartbeat:
                    try:
                        await heartbeat(elapsed)
                    except Exception:
                        pass
        return proc.returncode or 0, out_path, err_path
    finally:
        if proc.returncode is None:  # timeout, cancellation, or error: take the whole tree down
            kill_group(pgid)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                kill_group(pgid, signal.SIGKILL)
        kill_group(pgid, signal.SIGKILL)  # stray grandchildren
        _active.discard(pgid)


# ---------------------------------------------------------------- claude

def claude_argv(binary: str, pc: PeerConfig, model, effort, resume: str | None) -> list[str]:
    argv = [binary, "-p", "--output-format", "json",
            # Hard tool whitelist + deny anything not pre-approved; ignore user settings files.
            "--restricted", "--tools", "Read,Grep,Glob,Bash",
            "--permission-mode", "dontAsk", "--allowedTools", *pc.allowed_tools,
            # No MCP servers at all: the peer cannot reach duo-mcp (no Claude -> Codex -> Claude loops).
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--no-chrome"]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if resume:
        argv += ["--resume", resume]
    return argv + list(pc.extra_args)


def parse_claude(code: int, out: Path, err: Path) -> tuple[str, str | None, str | None]:
    raw = out.read_text(errors="replace").strip()
    try:
        data = json.loads(raw.splitlines()[-1]) if raw else None
    except (json.JSONDecodeError, IndexError):
        data = None
    if not isinstance(data, dict):
        raise PeerError(f"claude exited {code} without a JSON result. stderr: {tail(err) or '(empty)'}"
                        f"{' stdout: ' + raw[-800:] if raw else ''}")
    text = (data.get("result") or "").strip()
    if data.get("is_error") or code != 0:
        raise PeerError(f"claude reported an error (exit {code}, {data.get('subtype')}): {text or tail(err)}")
    usage = data.get("modelUsage") or {}
    # Claude Code also calls a small helper model; report the one that did the work.
    model = max(usage, key=lambda m: (usage[m] or {}).get("outputTokens", 0)) if usage else None
    denials = data.get("permission_denials") or []
    if denials:
        shown = [f"{d.get('tool_name')}: {str((d.get('tool_input') or {}).get('command', ''))[:80]}" for d in denials[:3]]
        text += (f"\n\n[duo-mcp: the read-only policy denied {len(denials)} peer tool call(s), e.g. "
                 f"{'; '.join(shown)}. Extend [claude].allowed_tools if these should be allowed.]")
    return text or "(peer returned an empty answer)", data.get("session_id"), model


# ---------------------------------------------------------------- codex

def codex_declared(cwd: str) -> list[str]:
    """`-c` overrides disabling every MCP server and plugin declared in Codex config files
    (user config + project .codex/ dirs up the tree). The consultant needs none of them, and this is
    what keeps it from reaching duo-mcp. Only declared names are touched: overriding an undeclared
    (plugin-provided) server makes Codex reject its config; disabling the plugin removes those.
    """
    files = [Path(os.environ.get("CODEX_HOME", HOME / ".codex")) / "config.toml"]
    files += [d / ".codex" / "config.toml" for d in [Path(cwd), *Path(cwd).parents]]
    overrides: list[str] = []
    for f in files:
        try:
            with f.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        for table in ("mcp_servers", "plugins"):
            for name, value in (data.get(table) or {}).items():
                if not isinstance(value, dict) or value.get("enabled") is False:
                    continue
                if "." in name or '"' in name:  # `-c` splits keys on dots and has no quoting
                    log.warning("cannot disable codex %s entry %r", table, name)
                    continue
                o = f"{table}.{name}.enabled=false"
                if o not in overrides:
                    overrides.append(o)
    return overrides


def codex_argv(binary: str, pc: PeerConfig, model, effort, resume: str | None, cwd: str, last_msg: Path) -> list[str]:
    argv = [binary, "exec"] + (["resume"] if resume else [])
    argv += ["--json", "--skip-git-repo-check", "-o", str(last_msg),
             "-c", 'approval_policy="never"', "-c", 'sandbox_mode="read-only"']
    if not resume:
        argv += ["-s", "read-only", "-C", cwd]
    for override in codex_declared(cwd):
        argv += ["-c", override]
    for feature in pc.disable_features:
        argv += ["--disable", feature]
    if model:
        argv += ["-m", model]
    if effort:
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    argv += list(pc.extra_args)
    argv += [resume, "-"] if resume else ["-"]  # prompt is read from stdin
    return argv


def parse_codex(code: int, out: Path, err: Path, last_msg: Path) -> tuple[str, str | None, str | None]:
    session_id, last_agent, errors = None, None, []
    with out.open(errors="replace") as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "thread.started":
                session_id = ev.get("thread_id")
            elif kind == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                last_agent = ev["item"].get("text")
            elif kind in ("error", "turn.failed"):
                msg = ev.get("message") or (ev.get("error") or {}).get("message") or json.dumps(ev)[:500]
                errors.append(msg)
    text = last_msg.read_text(errors="replace").strip() if last_msg.exists() else ""
    text = text or (last_agent or "").strip()
    if code != 0 or (errors and not text):
        detail = "; ".join(errors[-3:]) or tail(err) or "(no output)"
        raise PeerError(f"codex exited {code}: {detail}")
    return text or "(peer returned an empty answer)", session_id, None


# ---------------------------------------------------------------- entry point

async def run_peer(cfg: Config, peer: str, prompt: str, cwd: str, model: str | None = None,
                   effort: str | None = None, resume: str | None = None,
                   heartbeat: Callable[[float], Awaitable[None]] | None = None,
                   timeout: int | None = None) -> PeerResult:
    pc = cfg.claude if peer == "claude" else cfg.codex
    if resume and not SESSION_RE.match(resume):
        raise PeerError(f"invalid session_id {resume!r}")
    binary = find_binary(peer, pc)
    model, effort = resolve_model_effort(peer, pc, binary, model, effort)
    env = child_env(peer)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="duo-mcp-") as tmp:
        work = Path(tmp)
        last_msg = work / "last_message.txt"
        if peer == "claude":
            argv = claude_argv(binary, pc, model, effort, resume)
        else:
            argv = codex_argv(binary, pc, model, effort, resume, cwd, last_msg)
        log.info("start peer=%s cwd=%s model=%s effort=%s resume=%s prompt_chars=%d",
                 peer, cwd, model or "default", effort or "default", bool(resume), len(prompt))
        try:
            code, out, err = await run_process(argv, prompt, cwd, env, timeout or cfg.timeout_sec, work, heartbeat)
            if peer == "claude":
                text, session_id, used_model = parse_claude(code, out, err)
            else:
                text, session_id, used_model = parse_codex(code, out, err, last_msg)
        except PeerError as e:
            log.info("fail peer=%s after %.0fs: %s", peer, time.monotonic() - started, str(e)[:300])
            raise
        except asyncio.CancelledError:
            log.info("cancelled peer=%s after %.0fs", peer, time.monotonic() - started)
            raise
    seconds = time.monotonic() - started
    log.info("done peer=%s exit=%d %.0fs answer_chars=%d", peer, code, seconds, len(text))
    return PeerResult(truncate(text, cfg.max_output_chars), session_id or resume, used_model or model, seconds)
