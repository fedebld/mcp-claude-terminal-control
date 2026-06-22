#!/usr/bin/env python3
"""Quick check: frame mode on structured content must fail-closed (status nondeterministic),
never a guessed answer, and call the operator. Usage: MCP_URL=… python test_frame.py"""
import os, asyncio
from fastmcp import Client

URL = os.environ.get("MCP_URL", "http://127.0.0.1:8770/mcp")
PROMPT = "Restituisci ESATTAMENTE questa tabella markdown: | k | v |  |---|---|  | a | 1 |"


async def main():
    async with Client(URL) as c:
        sid = (await c.call_tool("claude_open", {})).data["session_id"]
        try:
            r = (await c.call_tool("claude_ask", {
                "session_id": sid, "prompt": PROMPT, "pace": False,
                "integrity": "frame", "timeout_s": 90})).data
            print("status:", r.get("status"), "| verified:", r.get("verified"))
            print("reason:", r.get("reason"))
            print("answer:", r.get("answer"))  # must be absent/none — never a guess
        finally:
            await c.call_tool("claude_close", {"session_id": sid})


if __name__ == "__main__":
    asyncio.run(main())
