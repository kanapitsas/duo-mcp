"""MCP stdio server. `for-claude` exposes Codex tools; `for-codex` exposes Claude tools."""

import logging
import os
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import prompts
from .config import Config
from .peers import PEER_ENV, PeerError, PeerResult, run_peer

log = logging.getLogger("duo-mcp")

# The peer runs read-only in the local repo, but it does send code to another model provider.
ANNOTATIONS = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)
NAMES = {"claude": "Claude (Claude Code, Anthropic)", "codex": "Codex (OpenAI)"}

Cwd = Annotated[str | None, Field(description=(
    "Absolute path of the repository the peer should work in. Pass your current working directory. "
    "Defaults to the MCP server's working directory."))]
Model = Annotated[str | None, Field(description="Peer model override (native model name/alias). Omit for the configured default.")]
Effort = Annotated[str | None, Field(description="Peer reasoning effort override (native level, e.g. low/medium/high/xhigh/max). Omit for the default.")]


def build_server(peer: str, cfg: Config) -> MCPServer:
    primary = "codex" if peer == "claude" else "claude"
    P, Me = NAMES[peer], NAMES[primary]
    server = MCPServer(
        name=f"duo-mcp ({peer})",
        instructions=(f"Tools to consult {P} as an independent peer agent working in the same repository. "
                      "Use it for ambiguous decisions, competing explanations, stuck debugging, or reviewing risky "
                      "changes. The peer is not an authority: resolve disagreements from evidence."))
    if os.environ.get(PEER_ENV):
        # We are running inside a peer (e.g. Claude -> Codex -> duo-mcp): expose nothing, no loops.
        log.info("started inside a peer process (%s set): no tools exposed", PEER_ENV)
        return server

    sessions: dict[str, tuple[str, str | None, str | None]] = {}  # session_id -> (cwd, model, effort)

    def resolve_cwd(cwd: str | None) -> str:
        path = Path(cwd).expanduser() if cwd else Path.cwd()
        if not path.is_absolute():
            raise ToolError(f"cwd must be an absolute path, got {cwd!r}")
        if not path.is_dir():
            raise ToolError(f"cwd does not exist or is not a directory: {path}")
        if str(path) == "/" and not cwd:
            raise ToolError("the MCP server was started in '/'; pass `cwd` (your repository's absolute path)")
        return str(path.resolve())

    async def call(ctx: Context, prompt: str, cwd: str, model, effort, resume=None) -> str:
        async def heartbeat(elapsed: float) -> None:
            await ctx.report_progress(elapsed, None, f"{peer} still working ({elapsed:.0f}s)")
        try:
            r: PeerResult = await run_peer(cfg, peer, prompt, cwd, model, effort, resume, heartbeat)
        except PeerError as e:
            raise ToolError(f"{peer} peer failed: {e}") from None
        if r.session_id:
            sessions[r.session_id] = (cwd, model, effort)
        head = f"[{peer} · model={r.model or 'default'} · effort={effort or 'default'} · {r.seconds:.0f}s"
        head += f" · session_id={r.session_id}]" if r.session_id else "]"
        foot = f"\n\n(Follow up with continue_{peer}(session_id=\"{r.session_id}\", ...).)" if r.session_id else ""
        return f"{head}\n\n{r.text}{foot}"

    @server.tool(name=f"ask_{peer}", annotations=ANNOTATIONS, description=(
        f"Consult {P} as an independent peer agent. It runs non-interactively in the same repository with "
        "read-only access, investigates the code itself (may run read-only commands and tests), and returns a "
        "concise answer plus a session_id. Takes from ~30s to several minutes.\n"
        "mode='independent' (default): phrase the question neutrally and put only FACTS in `context` "
        "(error output, repro steps, relevant paths). Do NOT include your own hypothesis, suspicion or conclusion; "
        "the value is an independent second opinion.\n"
        "mode='debate': you deliberately want your position challenged; put your position and reasoning in `context`.\n"
        "The peer is not an authority. If it disagrees, pin down the exact disagreement and settle it from code, "
        "tests and evidence."))
    async def ask(
        question: Annotated[str, Field(description="The question or task, phrased neutrally (no leading hypothesis in independent mode).")],
        ctx: Context,
        mode: Annotated[Literal["independent", "debate"], Field(description="independent (default) or debate.")] = "independent",
        context: Annotated[str | None, Field(description="independent: facts only (errors, logs, paths). debate: your position to be challenged.")] = None,
        model: Model = None,
        effort: Effort = None,
        cwd: Cwd = None,
    ) -> str:
        wd = resolve_cwd(cwd)
        return await call(ctx, prompts.ask_prompt(P, Me, question, mode, context), wd, model, effort)

    @server.tool(name=f"review_with_{peer}", annotations=ANNOTATIONS, description=(
        f"Ask {P} to review the current repository changes. The peer inspects the real state itself "
        "(git status, git diff, surrounding code, tests), not your summary, and reports findings by severity: "
        "correctness, regressions, edge cases, complexity, architecture, performance/security, tests, "
        "accidental API changes, wrong assumptions, solving the wrong problem. Takes minutes on large diffs. "
        "Treat findings as claims to verify, not verdicts."))
    async def review(
        ctx: Context,
        instructions: Annotated[str | None, Field(description="Optional review focus (e.g. 'concurrency in cache.py'). Avoid arguing for your change here.")] = None,
        base: Annotated[str | None, Field(description="Optional base branch/commit: also review committed changes since it. Default: uncommitted changes vs HEAD.")] = None,
        model: Model = None,
        effort: Effort = None,
        cwd: Cwd = None,
    ) -> str:
        wd = resolve_cwd(cwd)
        return await call(ctx, prompts.review_prompt(P, Me, instructions, base), wd, model, effort)

    @server.tool(name=f"continue_{peer}", annotations=ANNOTATIONS, description=(
        f"Send a follow-up to an earlier {P} consultation (keeps its context). Use it to push back with "
        "evidence, share your own view after its independent answer, or ask it to test a specific hypothesis."))
    async def cont(
        session_id: Annotated[str, Field(description=f"session_id returned by an earlier ask_{peer}/review_with_{peer} call.")],
        question: Annotated[str, Field(description="The follow-up message.")],
        ctx: Context,
        cwd: Cwd = None,
    ) -> str:
        known = sessions.get(session_id)
        wd = resolve_cwd(cwd) if cwd or not known else known[0]
        model, effort = (known[1], known[2]) if known else (None, None)
        return await call(ctx, prompts.follow_up_prompt(Me, question), wd, model, effort, resume=session_id)

    return server
