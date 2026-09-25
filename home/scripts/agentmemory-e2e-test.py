"""E2E through the REAL Hermes path: mcp SDK stdio_client + StdioServerParameters
against scripts/agentmemory-mcp.cmd — exactly how the gateway launches it.
Proves: initialize, FULL toolset (>10 tools, not the 7-tool InMemoryKV fallback),
memory_save -> memory_smart_search round-trip. Test memories are cleaned up via
REST /agentmemory/forget (memoryId) afterwards by the caller if this fails midway.
"""

import asyncio
import json
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BRIDGE = r"C:\Users\Ha Trung\AppData\Local\hermes\scripts\agentmemory-mcp.cmd"
MARKER = f"e2e-agentmemory-setup-{int(time.time())}"


def is_err(res) -> bool:
    return bool(getattr(res, "isError", None) or getattr(res, "is_error", False))


def texts(res) -> list:
    return [c.text for c in (res.content or []) if hasattr(c, "text")]


async def main() -> None:
    params = StdioServerParameters(
        command=BRIDGE,
        args=[],
        env={"AGENTMEMORY_URL": "http://127.0.0.1:3111"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), 30)

            tools = (await asyncio.wait_for(session.list_tools(), 30)).tools
            print(f"tools/list: {len(tools)} tools")
            assert len(tools) > 10, f"DEGRADED SHIM (only {len(tools)}) — livez probe failed"

            t0 = time.time()
            res = await asyncio.wait_for(session.call_tool("memory_save", {
                "content": (f"Setup verification {MARKER}: agentmemory engine autostarted via Startup .vbs, "
                            "MCP bridge wired into all 5 Hermes profiles."),
                "concepts": "agentmemory,hermes,setup,e2e",
            }), 120)
            print(f"memory_save isError={is_err(res)} took={round(time.time() - t0, 1)}s")
            assert not is_err(res), texts(res)

            res = await asyncio.wait_for(session.call_tool("memory_smart_search", {
                "query": MARKER, "limit": 5,
            }), 120)
            body = " ".join(texts(res))
            print(f"smart_search hit={MARKER in body}")
            assert MARKER in body, body[:400]

            mem_id = None
            try:
                payload = json.loads(body)
                rows = payload if isinstance(payload, list) else (
                    payload.get("results") or payload.get("memories") or [])
                mem_id = (rows[0] or {}).get("id") if rows else None
            except Exception:
                pass
            if mem_id:
                res = await asyncio.wait_for(session.call_tool("memory_governance_delete", {
                    "id": mem_id, "reason": "e2e test cleanup",
                }), 60)
                print("cleanup deleted:", mem_id, "isError=", is_err(res))
            print("E2E PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        print(f"E2E FAIL: {exc}", file=sys.stderr)
        sys.exit(1)
