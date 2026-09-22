"""Configuration: ~/.config/duo-mcp/config.toml (override with DUO_MCP_CONFIG)."""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("duo-mcp")

# Read-only investigation commands the Claude peer may run without asking.
# Everything else is denied (permission mode "dontAsk"). Test runners are
# included because running tests is often the fastest way to get evidence.
DEFAULT_CLAUDE_ALLOWED_TOOLS = [
    "Read", "Grep", "Glob",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(git blame:*)", "Bash(git grep:*)", "Bash(git ls-files:*)", "Bash(git rev-parse:*)",
    "Bash(git branch:*)", "Bash(git merge-base:*)",
    "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)", "Bash(rg:*)",
    "Bash(grep:*)", "Bash(pwd)", "Bash(file:*)", "Bash(stat:*)", "Bash(tree:*)",
    "Bash(pytest:*)", "Bash(python -m pytest:*)", "Bash(python3 -m pytest:*)", "Bash(uv run pytest:*)",
    "Bash(npm test:*)", "Bash(npm run test:*)", "Bash(pnpm test:*)", "Bash(yarn test:*)",
    "Bash(bun test:*)", "Bash(go test:*)", "Bash(go vet:*)", "Bash(cargo test:*)",
    "Bash(cargo check:*)", "Bash(make test:*)",
]

# Codex features that make no sense for a bounded read-only consultant:
# external app connectors (some can write), sub-agent delegation, images, goals.
DEFAULT_CODEX_DISABLE_FEATURES = ["apps", "multi_agent", "image_generation", "goals"]


@dataclass
class PeerConfig:
    command: str = ""          # empty = auto-detect
    model: str = ""            # empty = the CLI's own default
    effort: str = ""           # empty = the CLI's own default
    allowed_models: list[str] = field(default_factory=list)  # empty = any
    extra_args: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)     # claude only
    disable_features: list[str] = field(default_factory=list)  # codex only


@dataclass
class Config:
    timeout_sec: int = 900
    max_output_chars: int = 20000
    log_file: str = ""
    claude: PeerConfig = field(default_factory=lambda: PeerConfig(allowed_tools=list(DEFAULT_CLAUDE_ALLOWED_TOOLS)))
    codex: PeerConfig = field(default_factory=lambda: PeerConfig(disable_features=list(DEFAULT_CODEX_DISABLE_FEATURES)))


def config_path() -> Path:
    env = os.environ.get("DUO_MCP_CONFIG")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "duo-mcp" / "config.toml"


def _peer(raw: dict, default: PeerConfig, name: str) -> PeerConfig:
    known = set(PeerConfig.__dataclass_fields__)
    for key in raw:
        if key not in known:
            log.warning("config: unknown key [%s].%s ignored", name, key)
    for key, value in raw.items():
        if key in known:
            setattr(default, key, value)
    return default


def load_config() -> Config:
    cfg = Config()
    path = config_path()
    if not path.exists():
        return cfg
    with path.open("rb") as f:
        raw = tomllib.load(f)
    cfg.timeout_sec = int(raw.get("timeout_sec", cfg.timeout_sec))
    cfg.max_output_chars = int(raw.get("max_output_chars", cfg.max_output_chars))
    cfg.log_file = str(raw.get("log_file", cfg.log_file))
    cfg.claude = _peer(raw.get("claude", {}), cfg.claude, "claude")
    cfg.codex = _peer(raw.get("codex", {}), cfg.codex, "codex")
    return cfg
