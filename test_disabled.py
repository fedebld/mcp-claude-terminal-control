#!/usr/bin/env python3
"""Confirm frame mode is disabled (returns status='disabled' before any model call)."""
import os, asyncio
from fastmcp import Client

URL = os.environ.get("MCP_URL", "http://127.0.0.1:8770/mcp")


async def main():
    async with Client(URL) as c:
        sid = (await c.call_tool("claude_open", {})).data["session_id"]
        try:
            r = (await c.call_tool("claude_ask", {
                "session_id": sid, "prompt": "x", "integrity": "frame"})).data
            print("frame ->", r.get("status"), "|", r.get("reason"))
        finally:
            await c.call_tool("claude_close", {"session_id": sid})


if __name__ == "__main__":
    asyncio.run(main())
