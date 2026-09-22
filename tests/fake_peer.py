#!/usr/bin/env python3
"""Stand-in for `claude` / `codex` in tests. Behaviour is chosen with FAKE_MODE."""
import json
import os
import sys
import time

mode = os.environ.get("FAKE_MODE", "ok")
argv = sys.argv[1:]
if argv[:1] == ["--help"]:
    print("  --effort <level>   Effort level (low, medium, high, xhigh, max)")
    sys.exit(0)
prompt = sys.stdin.read()
record = os.environ.get("FAKE_RECORD")
if record:
    with open(record, "w") as f:
        json.dump({"argv": argv, "prompt": prompt, "peer_env": os.environ.get("DUO_MCP_PEER"),
                   "pid": os.getpid(), "cwd": os.getcwd()}, f)
if mode == "sleep":
    time.sleep(60)
if mode == "fail":
    print("boom: something broke", file=sys.stderr)
    sys.exit(3)
text = "x" * 50000 if mode == "big" else "fake answer"
if "exec" in argv:  # codex
    out = argv[argv.index("-o") + 1]
    print(json.dumps({"type": "thread.started", "thread_id": "0199aaaa-bbbb-cccc-dddd-eeeeffff0000"}))
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
    open(out, "w").write(text)
else:  # claude
    print(json.dumps({"type": "result", "is_error": False, "result": text,
                      "session_id": "11111111-2222-3333-4444-555555555555",
                      "modelUsage": {"claude-fake-1": {}}, "permission_denials": []}))
