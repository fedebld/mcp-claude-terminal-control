#!/usr/bin/env python3
"""v0.2 validation: prove the Verifiable Framed Payload on rich content (a small table).

Runs two asks on the same session:
  1) integrity="frame"  → deterministic boundary extraction, no tools/approvals
  2) integrity="hash"   → file channel, facade re-hashes, byte-exact, verified=true

Usage: MCP_URL=http://127.0.0.1:8770/mcp python selftest_v2.py
"""
import os, asyncio
from fastmcp import Client

URL = os.environ.get("MCP_URL", "http://127.0.0.1:8770/mcp")
PROMPT = ("Restituisci ESATTAMENTE questa tabella markdown e nient'altro:\n"
          "| k | v |\n|---|---|\n| a | 1 |\n| b | 2 |").replace("\n", "  ")  # single line for the TUI


async def main():
    async with Client(URL) as c:
        sid = (await c.call_tool("claude_open", {})).data["session_id"]
        print("session:", sid)
        try:
            for mode in ("frame", "hash"):
                r = (await c.call_tool("claude_ask", {
                    "session_id": sid, "prompt": PROMPT, "pace": False,
                    "integrity": mode, "timeout_s": 150,
                })).data
                print(f"\n=== integrity={mode} ===")
                print("status:", r.get("status"), "| verified:", r.get("verified"),
                      "| sha256:", (r.get("sha256") or "-")[:16], "| len:", r.get("len"))
                print("answer:\n" + (r.get("answer") or r.get("dialog") or "(none)"))
        finally:
            print("\nclose:", (await c.call_tool("claude_close", {"session_id": sid})).data)


if __name__ == "__main__":
    asyncio.run(main())
