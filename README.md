# duo-mcp

A small local MCP server that lets **Claude Code** and **Codex** consult each other as independent peers.

The two models make different mistakes. By default the peer gets a neutral question and the repository,
**not** the caller's hypothesis. It investigates the code itself, without editing it, and answers with
evidence. The caller settles any disagreement from evidence. The goal is useful disagreement, not consensus.

```
duo-mcp for-claude   # server for Claude Code → ask_codex, review_with_codex, continue_codex, get_codex_result, cancel_codex
duo-mcp for-codex    # server for Codex       → ask_claude, review_with_claude, continue_claude, get_claude_result, cancel_claude
duo-mcp doctor       # detected CLIs, config, valid models and efforts
```

## Install

Requires Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), and logged-in `claude` and `codex` CLIs.
On macOS, duo-mcp also finds the Codex CLI bundled in the ChatGPT/Codex app.

```bash
git clone https://github.com/kanapitsas/duo-mcp && cd duo-mcp
uv tool install --editable .      # installs ~/.local/bin/duo-mcp; keep the checkout
duo-mcp doctor
```

In the client configs below, use the **absolute path** to `duo-mcp`: GUI apps don't inherit your shell's PATH.

### Register with Claude Code (CLI and desktop app)

```bash
claude mcp add-json -s user duo \
  '{"type":"stdio","command":"/Users/YOU/.local/bin/duo-mcp","args":["for-claude"],"timeout":1800000}'
```

`timeout` (ms) is Claude Code's per-call limit. Keep it above duo-mcp's `timeout_sec` (900s by default).
Only new sessions see the server. For headless `claude -p`, add
`--allowedTools "mcp__duo__*"`.

### Register with Codex (CLI and desktop app)

In `~/.codex/config.toml`:

```toml
[mcp_servers.duo]
command = "/Users/YOU/.local/bin/duo-mcp"
args = ["for-codex"]
tool_timeout_sec = 1800   # Codex's default is far too short
# env_vars = ["ANTHROPIC_API_KEY"]   # only if Claude authenticates with an API key
```

The tools are annotated read-only, so Codex calls them without an approval prompt, including under `codex exec`.

### Tell the agents when to use it

Paste [`snippets/CLAUDE.md`](snippets/CLAUDE.md) into `~/.claude/CLAUDE.md` and
[`snippets/AGENTS.md`](snippets/AGENTS.md) into `~/.codex/AGENTS.md` (or into the project files).

## Tools

| tool | what it does |
|---|---|
| `ask_<peer>(question, mode?, context?, model?, effort?, cwd?)` | New investigation. `mode="independent"` (default): neutral question, `context` holds facts only. `mode="debate"`: `context` holds your position, and the peer tries to break it. |
| `review_with_<peer>(instructions?, base?, model?, effort?, cwd?)` | Reviews the real repo state (`git status`, `git diff`, code, tests), not your summary. `base` adds commits since a branch or commit. |
| `continue_<peer>(session_id, question, cwd?)` | Follow-up in the same peer session: push back, share your view, ask it to test something. |
| `get_<peer>_result(job_id?, wait_sec?)` | Answer of a background job, or its status and elapsed time if still running. `wait_sec` (≤ 600) waits for it. No `job_id` lists jobs. |
| `cancel_<peer>(job_id)` | Stops a background job and kills the peer. |

Always pass `cwd`, the absolute path of the repository. For example:

```
ask_codex(question="Why does test_total fail after set_price()?",
          context="<pytest output>", cwd="/abs/path/to/repo")
```

Each answer starts with `[codex · model=… · effort=… · 45s · session_id=…]`. A call takes from about 30s
to several minutes, with a progress notification every 15s.

### Background jobs

`ask_`, `review_with_` and `continue_` accept `background=true`. The call returns a `job_id` immediately
and the agent keeps working, then collects the answer:

```
review_with_codex(cwd="/abs/repo", background=true)        → job_id=job-1a2b3c4d
... other work ...
get_codex_result(job_id="job-1a2b3c4d", wait_sec=300)      → the review, or "still running after 412s"
```

Background jobs get `job_timeout_sec` (3600s) instead of `timeout_sec`, and each `get_…_result` call stays
short, so the client's tool timeout no longer caps long reviews. On a job still running, the agent decides
whether to wait more or `cancel_…` it. At most `max_jobs` (3) run at once. Finished jobs are forgotten after an
hour, and all jobs die with the MCP server. The server cannot wake the agent up when a job ends: the agent
has to come back for it.

In Claude Code, a background subagent (`Agent` tool) that calls `ask_codex` is an alternative: Claude is
notified when it finishes. It is still subject to the blocking timeouts.

## How the peer runs

| | Claude as peer | Codex as peer |
|---|---|---|
| command | `claude -p --output-format json` | `codex exec --json` |
| isolation | edit tools removed; only an allowlist of read-only commands and test runners (`--permission-mode dontAsk`) | `-s read-only` sandbox, `approval_policy="never"` |
| no recursion | no MCP servers (`--strict-mcp-config`) | declared MCP servers and plugins disabled (except names containing `.` or `"`, logged as a warning); `apps`, `multi_agent`, `image_generation`, `goals` off |
| follow-up | `--resume <session_id>` | `codex exec resume <thread_id>` |

A peer can never call back (Claude → Codex → Claude …). As a second guard, peer processes get
`DUO_MCP_PEER=1`, and a duo-mcp started under it exposes no tools.

- **Timeout:** 900s for blocking calls, 3600s for background jobs. On timeout, cancellation or client shutdown, the peer's whole process group is killed.
- **Output:** capped at 20,000 chars. CLI errors (auth, capacity, bad model) and permission denials are reported to the caller.
- **Writes:** the Claude peer has no OS sandbox. The tests it may run can write files, caches at least.
  The Codex sandbox blocks all writes, so some test suites fail there.
- **Privacy:** both peers send repository content to their model provider, as the primary agent does.

## Configuration (optional)

`~/.config/duo-mcp/config.toml`, or `$DUO_MCP_CONFIG`. Every key is optional. Unset `model` and `effort`
fall back to the CLI's own defaults. Per-call `model` and `effort` override them.

```toml
timeout_sec = 900        # blocking calls; keep below the client's tool timeout
job_timeout_sec = 3600   # background jobs
max_jobs = 3             # background jobs running at once
max_output_chars = 20000
# log_file = "~/.local/state/duo-mcp/duo-mcp.log"   # or DUO_MCP_DEBUG=1

[claude]                 # Claude as the peer (called from Codex)
# model = "opus"         # see `duo-mcp doctor`
# effort = "high"        # low | medium | high | xhigh | max
# allowed_models = ["opus", "sonnet"]
# allowed_tools = [...]  # REPLACES the read-only allowlist in config.py
# command = "/path/to/claude"

[codex]                  # Codex as the peer (called from Claude)
# model = "…"            # see `duo-mcp doctor`
# effort = "high"
# allowed_models = []
# disable_features = [...]  # REPLACES the default list above
# command = "/path/to/codex"
```

Successful calls log metadata only (peer, cwd, model, effort, duration, sizes). A failed call may also log
the first 300 chars of the CLI error or output.

## Examples

[`examples/README.md`](examples/README.md) shows real end-to-end runs in both directions.

## Development

```bash
uv run pytest                                   # unit tests with fake peer CLIs, no API calls
uv run scripts/mcp_call.py for-claude           # list tools over real MCP stdio
uv run scripts/mcp_call.py for-claude ask_codex '{"question": "...", "cwd": "/abs/repo"}'
```
