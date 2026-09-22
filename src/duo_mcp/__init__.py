"""duo-mcp: let Claude Code and Codex consult each other as independent peers."""

from __future__ import annotations

import argparse
import atexit
import logging
import os
import re
import signal
import subprocess
import sys
from pathlib import Path


def setup_logging(log_file: str) -> None:
    # stderr (clients capture it) gets warnings only. Debug detail goes to a file when enabled.
    # Only metadata is logged (peer, cwd, model, effort, durations, sizes): never prompts or answers.
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    handlers[0].setLevel(logging.WARNING)
    if os.environ.get("DUO_MCP_DEBUG") and not log_file:
        log_file = "~/.local/state/duo-mcp/duo-mcp.log"
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path)
        fh.setLevel(logging.INFO)
        handlers.append(fh)
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s pid=%(process)d %(levelname)s %(message)s")
    for noisy in ("mcp", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def doctor() -> int:
    from .config import config_path, load_config
    from .peers import PeerError, claude_efforts, codex_models, find_binary

    cfg = load_config()
    path = config_path()
    print(f"config: {path} ({'found' if path.exists() else 'not found, using defaults'})")
    print(f"timeout_sec={cfg.timeout_sec} max_output_chars={cfg.max_output_chars}")
    for peer, pc in (("claude", cfg.claude), ("codex", cfg.codex)):
        print(f"\n[{peer}] model={pc.model or '(CLI default)'} effort={pc.effort or '(CLI default)'}")
        try:
            binary = find_binary(peer, pc)
            ver = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
            print(f"  binary: {binary} ({ver})")
        except PeerError as e:
            print(f"  ERROR: {e}")
            continue
        if peer == "claude":
            help_text = subprocess.run([binary, "--help"], capture_output=True, text=True, timeout=20).stdout
            m = re.search(r"--model <model>\s+(.*?)\n  -", help_text, re.S)
            print(f"  models: {' '.join(m.group(1).split()) if m else '(see claude --help)'}")
            print(f"  efforts: {', '.join(claude_efforts(binary)) or '(could not parse claude --help)'}")
        else:
            models = codex_models()
            if not models:
                print("  models: (no ~/.codex/models_cache.json; run codex once)")
            for slug, efforts in models.items():
                print(f"  model {slug}: efforts {', '.join(efforts)}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="duo-mcp", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("for-claude", help="MCP stdio server for Claude Code: exposes Codex tools")
    sub.add_parser("for-codex", help="MCP stdio server for Codex: exposes Claude tools")
    sub.add_parser("doctor", help="show detected CLIs, config, and valid models/efforts")
    args = parser.parse_args()

    if args.cmd == "doctor":
        sys.exit(doctor())

    from .config import load_config
    from .peers import kill_all
    from .server import build_server

    cfg = load_config()
    setup_logging(cfg.log_file)
    atexit.register(kill_all)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # runs atexit -> kills peer process groups
    peer = "codex" if args.cmd == "for-claude" else "claude"
    logging.getLogger("duo-mcp").info("server start: %s (peer=%s) cwd=%s", args.cmd, peer, os.getcwd())
    build_server(peer, cfg).run("stdio")
