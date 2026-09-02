"""Task t_75c2f752 — UnifiedMemoryProvider identity-scoped remember/search/forget.

Provider-level tests: every AgentMemory call made through the provider
(sync_turn, prefetch, unified write/search/compensation, delegation observe)
carries the resolved identity + scope down to the client layer. Uses a
recording fake in place of _AgentMemoryClient so we assert on the exact
kwargs the provider threads through.
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

import pytest

import plugins.memory.unified_memory as um
from plugins.memory.unified_memory import UnifiedMemoryProvider
from plugins.memory.unified_memory.scope import DEFAULT_SCOPE


class RecordingClient:
    """Stands in for _AgentMemoryClient; records call kwargs."""

    def __init__(self):
        self.base_url = "http://fake:3111"
        self.remember_calls = []
        self.search_calls = []
        self.forget_calls = []
        self.fail_remember = False

    def health(self):
        return True

    def remember(self, content, *, title="", type_="fact", concepts=None,
                 metadata=None, scope=None):
        if self.fail_remember:
            return None
        self.remember_calls.append(
            {"content": content, "title": title, "type": type_,
             "concepts": concepts, "metadata": metadata, "scope": scope})
        return f"mem_{len(self.remember_calls)}"

    def smart_search(self, query, limit=10, scope=None):
        self.search_calls.append({"query": query, "limit": limit, "scope": scope})
        if "am-hit" in query or query == "both":
            return [{"obsId": "mem_x", "score": 0.8, "title": "AM obs",
                     "timestamp": "2026-08-25T10:00:00Z", "content": "c"}]
        return []

    def forget_by_id(self, memory_id, *, scope=None):
        self.forget_calls.append({"memory_id": memory_id, "scope": scope})
        return True


class FakeVault:
    """Minimal vault double (same surface the provider touches)."""

    def __init__(self):
        self.notes = {}

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def shutdown(self):
        pass

    def system_prompt_block(self):
        return "[vault]"

    def prefetch(self, query, *, session_id=""):
        return ""

    def queue_prefetch(self, query, *, session_id=""):
        pass

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, tool_name, args, **kwargs):
        raise NotImplementedError(tool_name)

    def on_session_end(self, messages):
        pass

    def on_session_switch(self, *args, **kwargs):
        pass

    def on_pre_compress(self, messages):
        return ""

    def on_memory_write(self, *args, **kwargs):
        pass

    def backup_paths(self):
        return []

    def _get_index(self, vault_name=None):
        return object()

    def _handle_create_note(self, args, index=None):
        self.notes[args["title"]] = args
        slug = args["title"].lower().replace(" ", "-")
        return json.dumps({"slug": slug, "title": args["title"]})


@pytest.fixture()
def scoped_env(monkeypatch):
    """Deterministic identity: vex/hermes/skappa."""
    monkeypatch.setenv("HERMES_PROFILE", "vex_agent")
    monkeypatch.delenv("UNIFIED_MEMORY_AGENT_ID", raising=False)
    monkeypatch.delenv("AGENT_ID", raising=False)
    monkeypatch.delenv("UNIFIED_MEMORY_TEAM_ID", raising=False)
    monkeypatch.delenv("TEAM_ID", raising=False)
    monkeypatch.delenv("UNIFIED_MEMORY_USER_ID", raising=False)
    monkeypatch.delenv("USER_ID", raising=False)


@pytest.fixture()
def provider(scoped_env, monkeypatch):
    fv = FakeVault()
    rc = RecordingClient()

    class _OVStub:
        @staticmethod
        def ObsidianVaultProvider(config=None):
            return fv

        @staticmethod
        def _load_plugin_config():
            return {}

    monkeypatch.setitem(sys.modules, "plugins.memory.obsidian_vault", _OVStub)
    # `from plugins.memory import obsidian_vault` reads the parent package
    # attribute, which sys.modules alone does not affect once the real module
    # has been imported by another test in the same session.
    import plugins.memory as _pm
    monkeypatch.setattr(_pm, "obsidian_vault", _OVStub, raising=False)
    p = UnifiedMemoryProvider(config={})
    p._am = rc  # setter re-targets the circuit breaker too
    p.initialize("sess-scope", hermes_home="", platform="test",
                 agent_context="primary")
    yield p, rc
    p.shutdown()


def test_sync_turn_passes_default_scope(provider):
    p, rc = provider
    p.sync_turn("u", "a")
    assert len(rc.remember_calls) == 1
    assert rc.remember_calls[0]["scope"] == DEFAULT_SCOPE == "isolated"


def test_delegation_observe_passes_default_scope(provider):
    p, rc = provider
    p.on_delegation("task", "result")
    assert len(rc.remember_calls) == 1
    assert rc.remember_calls[0]["scope"] == "isolated"


def test_unified_search_threads_scope_to_client(provider):
    p, rc = provider
    res = p._tool_unified_search({"query": "am-hit"})
    assert res["count"] == 1
    assert rc.search_calls[0]["scope"] == "isolated"


def test_prefetch_threads_scope_to_client(provider):
    p, rc = provider
    ctx = p.prefetch("am-hit")
    assert "Working memory" in ctx
    assert rc.search_calls[0]["scope"] == "isolated"


def test_unified_write_and_compensation_thread_scope(provider):
    p, rc = provider
    out = json.loads(p.handle_tool_call("memory_unified_write",
                                        {"title": "T1", "content": "c1"}))
    assert out["status"] == "completed"
    assert rc.remember_calls[0]["scope"] == "isolated"
    # Vault fails -> AM record compensated via forget_by_id, scope carried.
    p._vault_provider._handle_create_note = lambda *a, **k: json.dumps(
        {"error": "disk full"})
    out2 = json.loads(p.handle_tool_call("memory_unified_write",
                                         {"title": "T2", "content": "c2"}))
    assert out2["status"] in ("partial", "failed")
    assert out2["agentmemory"]["status"] == "rolled_back"
    assert rc.forget_calls and rc.forget_calls[0]["scope"] == "isolated"


def test_shared_config_marks_matching_category_shared(provider):
    """ScopeManager integration point: shared category resolves scope=shared."""
    from plugins.memory.unified_memory.scope import ScopeManager
    sm = ScopeManager({"shared_categories": ["decisions"]})
    assert sm.tag_scope("decisions") == "shared"
    assert sm.tag_scope("anything-else") == "isolated"


def test_identity_resolved_per_call_not_import_time(provider):
    """Flipping HERMES_PROFILE mid-session changes what the next call sends."""
    import os
    ident_before = um.resolve_identity()
    assert ident_before.agent_id == "vex"
    os.environ["HERMES_PROFILE"] = "neo_agent"
    try:
        assert um.resolve_identity().agent_id == "neo"
    finally:
        os.environ["HERMES_PROFILE"] = "vex_agent"


# ---------------------------------------------------------------------------
# Wire-level: full stack through _AgentMemoryClient against a live HTTP server.
# ---------------------------------------------------------------------------

class _Capture(BaseHTTPRequestHandler):
    last = {}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        _Capture.last = {"path": self.path, "headers": dict(self.headers),
                         "body": body}
        payload = json.dumps({"id": "mem_1", "results": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *a):
        pass


def test_wire_provider_remember_carries_identity_headers(scoped_env):
    server = HTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = um._AgentMemoryClient(f"http://127.0.0.1:{server.server_address[1]}")
        from plugins.memory.unified_memory.delegate import AgentMemoryFallback
        fb = AgentMemoryFallback(client)
        fb.remember("hello", title="t")  # same path the provider calls
        r = _Capture.last
        assert r["headers"]["X-Agent-Id"] == "vex"
        assert r["headers"]["X-Team-Id"] == "hermes"
        assert r["headers"]["X-User-Id"] == "skappa"
        b = r["body"]
        assert (b["agentId"], b["teamId"], b["userId"]) == ("vex", "hermes", "skappa")
        assert b["scope"] == "isolated"
    finally:
        server.shutdown()
