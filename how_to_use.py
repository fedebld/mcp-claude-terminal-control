#!/usr/bin/env python3
"""Live smoke test / usage example for the claude-terminal-control MCP facade.

Connects to the running HTTP endpoint with a FastMCP client (the same handshake any
MCP-native agent uses), opens a piloted claude session, asks ONE plan-mode prompt
(read-only, cheap), prints the clean answer, and closes.

Usage:
    pip install fastmcp==3.3.1
    MCP_URL=http://100.94.187.21:8770/mcp python how_to_use.py
"""
import os
import asyncio
from fastmcp import Client

URL = os.environ.get("MCP_URL", "http://100.94.187.21:8770/mcp")


async def main() -> None:
    async with Client(URL) as c:
        tools = [t.name for t in await c.list_tools()]
        print("tools:", tools)
        resources = [str(r.uri) for r in await c.list_resources()]
        print("resources:", resources)

        opened = (await c.call_tool("claude_open", {})).data
        print("open:", {k: opened.get(k) for k in ("session_id", "ready", "target")})
        sid = opened["session_id"]
        try:
            # Plan-mode style: ask it to PROPOSE, not execute → no permission round-trips,
            # read-only, minimal cost. pace=False so we don't wait 2–9 min in the test.
            ans = (await c.call_tool("claude_ask", {
                "session_id": sid,
                "prompt": "Rispondi esattamente con la parola PILOT-OK e nient'altro. Non usare strumenti.",
                "pace": False,
            })).data
            print("ask status:", ans.get("status"), "| paced_s:", ans.get("paced_s"))
            print("answer:\n", ans.get("answer"))
        finally:
            print("close:", (await c.call_tool("claude_close", {"session_id": sid})).data)


if __name__ == "__main__":
    asyncio.run(main())
