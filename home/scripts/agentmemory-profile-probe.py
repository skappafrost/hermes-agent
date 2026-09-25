"""Per-profile live verification that Hermes can discover the agentmemory MCP toolset.

Runs in a fresh subprocess per profile (driver below sets HERMES_HOME to that profile's
home), filters discovery to the single 'agentmemory' server so it spawns only that stdio
bridge, and prints how many agentmemory tools registered + a few names. The driver
asserts each profile sees the FULL toolset (>10, not the 7-tool degraded shim), proving
each profile's config.yaml -> bridge -> REST wiring is live.
"""

import json
import sys

ALLOWED = ["agentmemory"]


def run() -> int:
    from tools.mcp_tool_discovery import discover_mcp_tools

    names = discover_mcp_tools(allowed_mcp_names=ALLOWED)
    am = [n for n in names if n.startswith("mcp__agentmemory__")]
    print(json.dumps({
        "hermes_home": str(__import__("hermes_constants").get_hermes_home()),
        "agentmemory_tools": len(am),
        "sample": sorted(am)[:4],
    }))
    return 0 if len(am) > 10 else 2


if __name__ == "__main__":
    try:
        sys.exit(run())
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": repr(exc)}), file=sys.stderr)
        sys.exit(1)
