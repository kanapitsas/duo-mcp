# duo-mcp

A small local MCP server that lets **Claude Code** and **Codex** consult each other as independent peers.

Claude and Codex make different mistakes. duo-mcp keeps their reasoning independent: by default the peer
gets a neutral question and the repository, **not** the caller's hypothesis. It investigates the code
itself, read-only, and returns a short answer with evidence. The caller then settles any disagreement
from evidence. The goal is useful disagreement, not consensus.

```
duo-mcp for-claude   # MCP server for Claude Code  → tools: ask_codex, review_with_codex, continue_codex
duo-mcp for-codex    # MCP server for Codex        → tools: ask_claude, review_with_claude, continue_claude
duo-mcp doctor       # show detected CLIs, config, valid models/efforts
```

## Tools

| tool | what it does |
|---|---|
| `ask_<peer>(question, mode?, context?, model?, effort?, cwd?)` | Fresh peer investigation. `mode="independent"` (default): neutral question, `context` = facts only. `mode="debate"`: `context` = your position, which the peer tries to break. |
| `review_with_<peer>(instructions?, base?, model?, effort?, cwd?)` | Peer reviews the real repo state (`git status`, `git diff`, surrounding code, tests), not your summary. `base` also covers commits since a branch. |
| `continue_<peer>(session_id, question, cwd?)` | Follow-up in the same peer session (push back with evidence, reveal your view, ask it to test something). |

Each answer starts with a header like `[codex · model=… · effort=… · 45s · session_id=…]`.

## How the peer runs

| | Claude as peer | Codex as peer |
|---|---|---|
| command | `claude -p --output-format json` (prompt on stdin) | `codex exec --json -o <file> -` (prompt on stdin) |
| read-only | `--restricted --tools Read,Grep,Glob,Bash --permission-mode dontAsk --allowedTools <read-only list>`: anything not on the list is denied | `-s read-only`, `approval_policy="never"` |
| no recursion | `--strict-mcp-config --mcp-config '{"mcpServers":{}}'`: no MCP at all | every MCP server and plugin declared in Codex config is disabled with `-c …enabled=false`; `apps`, `multi_agent`, `image_generation`, `goals` features off |
| model / effort | `--model`, `--effort` | `-m`, `-c model_reasoning_effort=…` |
| follow-up | `--resume <session_id>` | `codex exec resume <thread_id>` |

As a second guard, every peer process gets `DUO_MCP_PEER=1`, and a duo-mcp started under it exposes no
tools. Either way a peer can never call back (Claude → Codex → Claude …). The *primary* agent can call
again as often as it wants.

Safeguards: a wall-clock timeout (default 900s) kills the peer's whole process group. Cancelling the tool
call or shutting down the client also kills it. Output is capped (default 20,000 chars). Non-zero exits,
CLI errors (auth, model at capacity, bad model) and permission denials are reported to the caller verbatim.
Invalid efforts are rejected up front, since `claude` only warns about them. Progress notifications go out
every 15s.

**Limits.** The Claude peer can run test runners, and they may write caches (`__pycache__`, `.pytest_cache`).
The Codex read-only sandbox blocks all writes, so some test suites fail there. Both peers send repository
content to their model provider, just as the primary agent does.

## Install

Requires Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), and logged-in `claude` and `codex` CLIs.
On macOS, the Codex CLI ships inside the ChatGPT/Codex app
(`/Applications/ChatGPT.app/Contents/Resources/codex`), and duo-mcp finds it there.

```bash
uv tool install --editable /path/to/duo-mcp     # puts duo-mcp in ~/.local/bin
duo-mcp doctor
```

Use the **absolute path** (`~/.local/bin/duo-mcp` expanded) in client configs. GUI apps don't have your shell's PATH.

## MCP configuration (user-level, once)

### Claude Code: CLI and desktop app (Code tab) share `~/.claude.json`

```bash
claude mcp add-json -s user duo \
  '{"type":"stdio","command":"/Users/YOU/.local/bin/duo-mcp","args":["for-claude"],"timeout":1800000}'
```

`timeout` is Claude Code's per-server tool-call limit in ms. Set it above duo-mcp's own `timeout_sec`.
New sessions pick the server up; running sessions don't. In headless `claude -p`, allow the tools with
`--allowedTools mcp__duo__ask_codex mcp__duo__review_with_codex mcp__duo__continue_codex`.

The Claude *chat* app (`claude_desktop_config.json`) is not a coding agent and has no repository, so it
isn't a target here.

### Codex: CLI and Codex desktop app share `~/.codex/config.toml`

```toml
[mcp_servers.duo]
command = "/Users/YOU/.local/bin/duo-mcp"
args = ["for-codex"]
tool_timeout_sec = 1800     # Codex's default is far too short for a peer investigation
```

(`codex mcp add duo -- /Users/YOU/.local/bin/duo-mcp for-codex` writes the first three lines. Add
`tool_timeout_sec` by hand.) The tools are annotated read-only, so Codex calls them without an approval
prompt, including under `codex exec`.

Codex starts MCP servers with a filtered environment. Keychain/OAuth logins work. If you authenticate
Claude with `ANTHROPIC_API_KEY`, add `env_vars = ["ANTHROPIC_API_KEY"]` to the block above.

## Configuration (optional)

`~/.config/duo-mcp/config.toml` (or `$DUO_MCP_CONFIG`). Every key is optional. Empty values mean the
CLI's own default.

```toml
timeout_sec = 900
max_output_chars = 20000
# log_file = "~/.local/state/duo-mcp/duo-mcp.log"   # or set DUO_MCP_DEBUG=1

[claude]                 # used when Claude is the peer (i.e. called from Codex)
model = "opus"           # alias or full name, see `duo-mcp doctor`
effort = "high"          # low | medium | high | xhigh | max
# allowed_models = ["opus", "sonnet"]
# allowed_tools = [...]  # replaces the default read-only allowlist (see config.py)
# command = "/path/to/claude"

[codex]                  # used when Codex is the peer (i.e. called from Claude)
model = "gpt-5.6-terra"  # see `duo-mcp doctor` (read from Codex's own model cache)
effort = "high"
# allowed_models = []
# disable_features = ["apps", "multi_agent", "image_generation", "goals"]
# command = "/path/to/codex"
```

Per-call `model` / `effort` override these. Logs hold metadata only (peer, cwd, model, effort, duration,
sizes). They never hold prompts, answers, file contents or environment variables.

## Agent instructions

Paste [`snippets/CLAUDE.md`](snippets/CLAUDE.md) into `~/.claude/CLAUDE.md` (or a project `CLAUDE.md`),
and [`snippets/AGENTS.md`](snippets/AGENTS.md) into `~/.codex/AGENTS.md` (or a project `AGENTS.md`).

## Example

See [`examples/README.md`](examples/README.md) for real end-to-end runs in both directions.

## Development

```bash
uv run pytest                                   # unit tests (fake peer CLIs, no API calls)
uv run scripts/mcp_call.py for-claude           # list tools over real MCP stdio
uv run scripts/mcp_call.py for-claude ask_codex '{"question": "...", "cwd": "/abs/repo"}'
```
