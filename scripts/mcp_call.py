"""Minimal MCP stdio client for manual testing.

    uv run scripts/mcp_call.py for-claude                      # list tools
    uv run scripts/mcp_call.py for-claude ask_codex '{"question": "..."}'
"""

import json
import os
import sys
import time

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def main() -> None:
    side, tool = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
    args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
    params = StdioServerParameters(command=sys.executable, args=["-m", "duo_mcp", side],
                                    env={k: v for k, v in os.environ.items() if k.startswith("DUO_MCP")})
    async with stdio_client(params) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            if not tool:
                for t in (await session.list_tools()).tools:
                    print(f"{t.name}: {sorted((t.input_schema.get('properties') or {}).keys())}")
                return
            t0 = time.monotonic()
            res = await session.call_tool(tool, args)
            print(f"--- isError={res.is_error} ({time.monotonic() - t0:.0f}s)")
            for c in res.content:
                print(getattr(c, "text", c))


anyio.run(main)
