"""Tests for the unified_memory provider (Obsidian vault + AgentMemory)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import plugins.memory.unified_memory as unified_memory_module
from plugins.memory.unified_memory import UnifiedMemoryProvider


class FakeVault:
    """Stands in for ObsidianVaultProvider — no disk, no config."""

    def __init__(self):
        self.notes = {}
        self.shutdowns = 0
        self.switched = False
        self.ended = False
        self.mem_writes = 0

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        pass

    def shutdown(self):
        self.shutdowns += 1

    def system_prompt_block(self):
        return "[Obsidian Vault fake: 2 notes]"

    def prefetch(self, query, *, session_id=""):
        if "vault-hit" in query:
            return "### Vault Note\nsnippet about vault-hit"
        return ""

    def queue_prefetch(self, query, *, session_id=""):
        pass

    def get_tool_schemas(self):
        return [{
            "name": "vault_search",
            "description": "d",
            "parameters": {"type": "object", "properties": {}},
        }]

    def handle_tool_call(self, tool_name, args, **kwargs):
        if tool_name == "vault_search":
            return json.dumps({"results": [{
                "slug": "note-1", "title": "Note 1", "tags": ["Alpha"],
                "path": "/x/note-1.md", "snippet": "body text here", "score": 0.9,
            }]})
        raise NotImplementedError(tool_name)

    def on_session_end(self, messages):
        self.ended = True

    def on_session_switch(self, *args, **kwargs):
        self.switched = True

    def on_pre_compress(self, messages):
        return "keep this"

    def on_memory_write(self, *args, **kwargs):
        self.mem_writes += 1

    def backup_paths(self):
        return ["C:/fake/vault"]

    def _get_index(self, vault_name: Optional[str] = None):
        return object()

    def _handle_create_note(self, args, index=None):
        self.notes[args["title"]] = args
        slug = args["title"].lower().replace(" ", "-")
        return json.dumps({"slug": slug, "title": args["title"]})


class FakeAgentMemory:
    def __init__(self):
        self.memories = []
        self.deleted = []
        self.fail_remember = False
        self.base_url = "http://fake:3111"

    def health(self):
        return True

    def remember(self, content, **kwargs):
        if self.fail_remember:
            return None
        mid = f"mem_{len(self.memories)}"
        self.memories.append((mid, content, kwargs))
        return mid

    def smart_search(self, query, limit=10):
        if "am-hit" in query or query == "both":
            return [{"obsId": "mem_x", "score": 0.8, "title": "AM obs",
                     "timestamp": "2026-08-25T10:00:00Z", "content": "c"}]
        return []

    def forget_by_id(self, memory_id):
        self.deleted.append(memory_id)
        return True


@pytest.fixture()
def provider(monkeypatch):
    fv, fam = FakeVault(), FakeAgentMemory()

    class _OVStub:
        @staticmethod
        def ObsidianVaultProvider(config=None):
            return fv

        @staticmethod
        def _load_plugin_config():
            return {}

    monkeypatch.setitem(sys.modules, "plugins.memory.obsidian_vault", _OVStub)
    p = UnifiedMemoryProvider(config={})
    p._am = fam
    p.initialize("sess-1", hermes_home="", platform="test", agent_context="primary")
    yield p, fv, fam
    p.shutdown()


def test_abc_conformance():
    from agent.memory_provider import MemoryProvider
    p = UnifiedMemoryProvider(config={})
    assert isinstance(p, MemoryProvider)
    assert not getattr(type(p), "__abstractmethods__", frozenset())
    assert p.name == "unified_memory"


def test_shutdown_safe_before_initialize():
    UnifiedMemoryProvider(config={}).shutdown()


def test_initialize_and_system_prompt(provider):
    p, _fv, _fam = provider
    assert p._session_id == "sess-1"
    block = p.system_prompt_block()
    assert "Obsidian Vault fake" in block
    assert "AgentMemory: online" in block


def test_prefetch_merges_both_sources(provider):
    p, _fv, _fam = provider
    ctx = p.prefetch("both vault-hit am-hit")
    assert "vault-hit" in ctx
    assert "Working memory" in ctx and "AM obs" in ctx
    status = p.recall_status()
    assert status is not None and status.count >= 2
    assert status.provider_label == "unified"


def test_recall_status_reflects_last_prefetch_only(provider):
    p, _fv, _fam = provider
    p.prefetch("both vault-hit am-hit")
    assert p.prefetch("nothing matches xyz") == ""
    assert p.recall_status() is None
    assert p.prefetch("") == ""


def test_unified_search_merges_and_sorts(provider):
    p, _fv, _fam = provider
    res = p._tool_unified_search({"query": "both"})
    assert res["count"] == 2 and not res["partial"]
    items = res["results"]
    # Higher score first; deterministic tiebreak by source name.
    assert items[0]["source"] == "obsidian" and items[0]["score"] == 0.9
    assert items[1]["source"] == "agentmemory"
    assert items[1]["uri"] == "agentmemory://memories/mem_x"
    assert items[1]["score"] == 0.8


def test_unified_search_partial_on_vault_failure(provider):
    p, fv, _fam = provider
    original = fv.handle_tool_call

    def boom(*a, **k):
        raise RuntimeError("boom")

    fv.handle_tool_call = boom
    res = p._tool_unified_search({"query": "both"})
    fv.handle_tool_call = original
    assert res["partial"] is True
    assert res["count"] == 1 and res["results"][0]["source"] == "agentmemory"


def test_unified_write_happy_path(provider):
    p, fv, _fam = provider
    out = json.loads(p.handle_tool_call("memory_unified_write", {
        "title": "Test Note", "content": "hello world", "tags": ["#Foo", "bar"],
    }))
    assert out["status"] == "completed"
    assert out["obsidian"]["status"] == "written" and out["agentmemory"]["status"] == "written"
    frontmatter = fv.notes["Test Note"]["frontmatter"]
    assert frontmatter["unified_id"]
    assert frontmatter["version"] == 1
    assert frontmatter["source"] == "unified_memory"
    assert frontmatter["content_hash"]
    # tags normalized: lowercased, '#' stripped, sorted, deduped
    assert fv.notes["Test Note"]["tags"] == ["bar", "foo"]


def test_unified_write_partial_keeps_vault_as_source_of_truth(provider):
    p, fv, fam = provider
    fam.fail_remember = True
    out = json.loads(p.handle_tool_call("memory_unified_write",
                                        {"title": "T2", "content": "c2"}))
    assert out["status"] == "partial"
    assert out["obsidian"]["status"] == "written"


def test_unified_write_compensates_when_vault_fails(provider):
    p, fv, fam = provider
    fv._handle_create_note = lambda *a, **k: json.dumps({"error": "disk full"})
    out = json.loads(p.handle_tool_call("memory_unified_write",
                                        {"title": "T3", "content": "c3"}))
    assert out["agentmemory"]["status"] == "rolled_back"
    assert fam.deleted, "compensation must delete the orphan AgentMemory record"


def test_vault_tools_delegate_verbatim(provider):
    p, _fv, _fam = provider
    result = json.loads(p.handle_tool_call("vault_search", {"query": "x"}))
    assert result["results"][0]["slug"] == "note-1"


def test_unknown_tool_raises(provider):
    p, _fv, _fam = provider
    with pytest.raises(NotImplementedError):
        p.handle_tool_call("unknown_tool", {})


def test_session_switch_updates_state(provider):
    p, fv, _fam = provider
    p.on_session_switch("sess-2", reset=True)
    assert p._session_id == "sess-2"
    assert fv.switched
    assert p.recall_status() is None  # reset clears last-prefetch state


def test_sync_turn_records_working_copy(provider):
    p, _fv, fam = provider
    p.sync_turn("user says", "assistant answers")
    assert len(fam.memories) == 1


def test_shutdown_delegates_once(provider):
    p, fv, _fam = provider
    p.shutdown()
    assert fv.shutdowns == 1


# ---------------------------------------------------------------------------
# Real discovery path: plugins/memory/__init__.py loads the module from disk.
# ---------------------------------------------------------------------------

def test_discovery_finds_provider_via_register(tmp_path):
    """The real discovery path loads the plugin file from disk and calls register(ctx)."""
    import shutil

    from plugins.memory import _load_provider_from_dir

    src = Path(unified_memory_module.__file__)
    target_dir = tmp_path / "unified_memory"
    target_dir.mkdir()
    shutil.copy2(src, target_dir / "__init__.py")
    shutil.copy2(
        src.parent / "plugin.yaml", target_dir / "plugin.yaml"
    )

    provider = _load_provider_from_dir(target_dir, register_skills=False)
    # The copy loads under a separate user namespace, so the instance's class is
    # not the same object as the bundled UnifiedMemoryProvider — verify by shape.
    assert provider is not None
    assert type(provider).__name__ == "UnifiedMemoryProvider"
    assert provider.name == "unified_memory"
    assert provider.get_tool_schemas()
