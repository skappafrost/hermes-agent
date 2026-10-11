"""Verify the agentmemory MemoryProvider: pure format + Hermes discovery + live prefetch.

Run with HERMES_HOME set to a profile home (default profile here). Seeds one memory through
the live engine, then loads the provider via Hermes' real discovery path and asserts prefetch()
recalls it as plain, '<memory-context'-free markdown.
"""
import asyncio
import os
import sys
import time

REPO = r"C:\Users\Ha Trung\AppData\Local\hermes\hermes-agent"
sys.path.insert(0, REPO)
os.environ["PYTHONPATH"] = REPO

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

BRIDGE = r"C:\Users\Ha Trung\AppData\Local\hermes\scripts\agentmemory-mcp.cmd"
MARKER = f"provider-verify-{int(time.time())}"
HOME = os.environ.get("HERMES_HOME", r"C:\Users\Ha Trung\AppData\Local\hermes")


async def seed():
    txt = (f"Provider verify {MARKER}: the Hermes agentmemory MemoryProvider pushes recalled "
           "memory into each turn via /agentmemory/search. Unique token ZEBRACORTEX.")
    async with stdio_client(StdioServerParameters(command=BRIDGE, args=[],
                     env={"AGENTMEMORY_URL": "http://127.0.0.1:3111"})) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            await asyncio.wait_for(s.call_tool("memory_save",
                         {"content": txt, "concepts": "agentmemory,provider,verify"}), 120)
    print("seeded memory with marker", MARKER)


def check_format():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "amem_provider", os.path.join(HOME, "plugins", "agentmemory", "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.AgentMemoryProvider._format({"results": [
        {"title": "Deploy rule", "observation": {"title": "Deploy rule", "facts": ["no force push", "squash merge"]}},
        {"title": "Pref note", "content": "single line note"},
    ]})
    assert "## AgentMemory" in out, out
    assert "Deploy rule: no force push; squash merge" in out, out
    assert "Pref note: single line note" in out, out
    assert "<memory-context>" not in out, "must NOT pre-wrap"
    assert mod.AgentMemoryProvider().get_tool_schemas() == []
    assert mod.AgentMemoryProvider().system_prompt_block() == ""
    print("format()/ABC OK\n" + out)


def check_discovery_and_prefetch():
    from plugins.memory import load_memory_provider
    p = load_memory_provider("agentmemory")
    assert p is not None, "provider not discovered under HERMES_HOME/plugins"
    assert p.name == "agentmemory"
    assert p.is_available() is True
    p.initialize("verify-session", hermes_home=HOME, platform="cli",
                 agent_context="primary", agent_identity="default")
    text = p.prefetch("ZEBRACORTEX unique token provider verify", session_id="verify-session")
    print("prefetch returned", len(text), "chars")
    print(text[:400] if text else "(EMPTY)")
    assert "ZEBRACORTEX" in text, "recall did not surface the seeded memory"
    assert "<memory-context>" not in text, "provider must not pre-wrap (Hermes wraps it)"
    p.shutdown()


if __name__ == "__main__":
    asyncio.run(seed())
    check_format()
    check_discovery_and_prefetch()
    print("ALL PROVIDER CHECKS PASSED")
