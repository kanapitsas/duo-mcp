"""MCP stdio server. `for-claude` exposes Codex tools; `for-codex` exposes Claude tools."""

import asyncio
import logging
import os
import secrets
import time
from dataclasses import dataclass
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
Background = Annotated[bool, Field(description=(
    "true: start the peer as a background job and return a job_id immediately, so you can keep working. "
    "Collect the answer later with the get-result tool. Use it for long investigations or reviews."))]

MAX_WAIT_SEC = 600       # a single get-result call must stay well below the client's tool timeout
FINISHED_TTL_SEC = 3600  # finished jobs are forgotten after this


@dataclass
class Job:
    id: str
    tool: str
    task: asyncio.Task
    started: float
    finished: float | None = None

    def status(self) -> str:
        if not self.task.done():
            return "running"
        if self.task.cancelled():
            return "cancelled"
        return "failed" if self.task.exception() else "done"

    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started


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
    jobs: dict[str, Job] = {}

    def resolve_cwd(cwd: str | None) -> str:
        path = Path(cwd).expanduser() if cwd else Path.cwd()
        if not path.is_absolute():
            raise ToolError(f"cwd must be an absolute path, got {cwd!r}")
        if not path.is_dir():
            raise ToolError(f"cwd does not exist or is not a directory: {path}")
        if str(path) == "/" and not cwd:
            raise ToolError("the MCP server was started in '/'; pass `cwd` (your repository's absolute path)")
        return str(path.resolve())

    def progress(ctx: Context, what: str):
        async def heartbeat(elapsed: float) -> None:
            await ctx.report_progress(elapsed, None, f"{what} ({elapsed:.0f}s)")
        return heartbeat

    async def consult(prompt: str, cwd: str, model, effort, resume=None, heartbeat=None, timeout=None) -> str:
        try:
            r: PeerResult = await run_peer(cfg, peer, prompt, cwd, model, effort, resume, heartbeat, timeout)
        except PeerError as e:
            hint = " Or run it with background=true." if timeout is None and "timed out" in str(e) else ""
            raise ToolError(f"{peer} peer failed: {e}{hint}") from None
        if r.session_id:
            sessions[r.session_id] = (cwd, model, effort)
        head = f"[{peer} · model={r.model or 'default'} · effort={effort or 'default'} · {r.seconds:.0f}s"
        head += f" · session_id={r.session_id}]" if r.session_id else "]"
        foot = f"\n\n(Follow up with continue_{peer}(session_id=\"{r.session_id}\", ...).)" if r.session_id else ""
        return f"{head}\n\n{r.text}{foot}"

    def prune() -> None:
        now = time.monotonic()
        for jid, job in list(jobs.items()):
            if job.finished and now - job.finished > FINISHED_TTL_SEC:
                del jobs[jid]

    def start_job(tool: str, prompt: str, cwd: str, model, effort, resume=None) -> str:
        prune()
        running = sum(1 for j in jobs.values() if not j.task.done())
        if running >= cfg.max_jobs:
            raise ToolError(f"{running} {peer} jobs already running (max_jobs={cfg.max_jobs}). "
                            f"Collect or cancel one first (get_{peer}_result / cancel_{peer}).")
        jid = f"job-{secrets.token_hex(4)}"
        # No MCP context here: the request that started the job is over by the time the peer answers.
        task = asyncio.create_task(consult(prompt, cwd, model, effort, resume, timeout=cfg.job_timeout_sec))
        job = jobs[jid] = Job(jid, tool, task, time.monotonic())

        def finished(t: asyncio.Task) -> None:
            job.finished = time.monotonic()
            if not t.cancelled():
                t.exception()  # mark retrieved: no "exception was never retrieved" noise
            log.info("job %s %s after %.0fs", jid, job.status(), job.elapsed())
        task.add_done_callback(finished)
        log.info("job %s started (%s)", jid, tool)
        return (f"[{peer} · background job_id={jid}]\n\nStarted {tool} in the background (limit "
                f"{cfg.job_timeout_sec}s). Keep working, then collect the answer with "
                f"get_{peer}_result(job_id=\"{jid}\", wait_sec=...). Collect it before you finish your task.")

    async def run(ctx: Context, tool: str, background: bool, prompt: str, cwd: str, model, effort, resume=None) -> str:
        if background:
            return start_job(tool, prompt, cwd, model, effort, resume)
        return await consult(prompt, cwd, model, effort, resume, progress(ctx, f"{peer} still working"))

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
        background: Background = False,
    ) -> str:
        wd = resolve_cwd(cwd)
        return await run(ctx, f"ask_{peer}", background, prompts.ask_prompt(P, Me, question, mode, context),
                         wd, model, effort)

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
        background: Background = False,
    ) -> str:
        wd = resolve_cwd(cwd)
        return await run(ctx, f"review_with_{peer}", background, prompts.review_prompt(P, Me, instructions, base),
                         wd, model, effort)

    @server.tool(name=f"continue_{peer}", annotations=ANNOTATIONS, description=(
        f"Send a follow-up to an earlier {P} consultation (keeps its context). Use it to push back with "
        "evidence, share your own view after its independent answer, or ask it to test a specific hypothesis."))
    async def cont(
        session_id: Annotated[str, Field(description=f"session_id returned by an earlier ask_{peer}/review_with_{peer} call.")],
        question: Annotated[str, Field(description="The follow-up message.")],
        ctx: Context,
        cwd: Cwd = None,
        background: Background = False,
    ) -> str:
        known = sessions.get(session_id)
        wd = resolve_cwd(cwd) if cwd or not known else known[0]
        model, effort = (known[1], known[2]) if known else (None, None)
        return await run(ctx, f"continue_{peer}", background, prompts.follow_up_prompt(Me, question),
                         wd, model, effort, resume=session_id)

    def job_line(job: Job) -> str:
        return f"{job.id}  {job.tool}  {job.status()}  {job.elapsed():.0f}s"

    @server.tool(name=f"get_{peer}_result", annotations=ANNOTATIONS, description=(
        f"Get the answer of a background {P} job (started with background=true). Returns the answer if the job "
        "is done, otherwise its status and elapsed time. `wait_sec` waits up to that long for it to finish "
        f"(max {MAX_WAIT_SEC}). Omit job_id to list all jobs. If a job runs longer than it is worth, cancel it."))
    async def get_result(
        ctx: Context,
        job_id: Annotated[str | None, Field(description="job_id returned by a background call. Omit to list jobs.")] = None,
        wait_sec: Annotated[int, Field(description=f"Seconds to wait for the job to finish (0-{MAX_WAIT_SEC}).")] = 0,
    ) -> str:
        prune()
        if not job_id:
            return "\n".join(job_line(j) for j in jobs.values()) or f"no {peer} jobs"
        job = jobs.get(job_id)
        if not job:
            raise ToolError(f"unknown job_id {job_id!r} (finished jobs are forgotten after {FINISHED_TTL_SEC}s)")
        deadline = time.monotonic() + max(0, min(wait_sec, MAX_WAIT_SEC))
        heartbeat = progress(ctx, f"waiting for {job_id}")
        while not job.task.done() and (remaining := deadline - time.monotonic()) > 0:
            await asyncio.wait({job.task}, timeout=min(15, remaining))  # never cancels the job itself
            if not job.task.done():
                try:
                    await heartbeat(job.elapsed())
                except Exception:
                    pass
        status = job.status()
        if status == "running":
            return (f"{job_id} ({job.tool}) still running after {job.elapsed():.0f}s "
                    f"(limit {cfg.job_timeout_sec}s). Wait again with get_{peer}_result, "
                    f"or stop it with cancel_{peer}(job_id=\"{job_id}\").")
        if status == "cancelled":
            raise ToolError(f"{job_id} was cancelled after {job.elapsed():.0f}s")
        exc = job.task.exception()
        if exc:
            raise ToolError(str(exc))
        return job.task.result()

    @server.tool(name=f"cancel_{peer}", annotations=ANNOTATIONS, description=(
        f"Cancel a running background {P} job and kill its process."))
    async def cancel(
        job_id: Annotated[str, Field(description="job_id of the job to cancel.")],
    ) -> str:
        job = jobs.get(job_id)
        if not job:
            raise ToolError(f"unknown job_id {job_id!r}")
        if job.task.done():
            return f"{job_id} already {job.status()}"
        job.task.cancel()
        await asyncio.wait({job.task}, timeout=10)  # run_process kills the peer's process group
        return f"{job_id} cancelled after {job.elapsed():.0f}s"

    return server
