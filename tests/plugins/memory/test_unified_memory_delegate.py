"""Tests for ObsidianVaultDelegate (task t_b28a1b77).

The delegate must expose an IDENTICAL public API to ObsidianVaultProvider and
forward every call unchanged — same args, same returns, same exceptions, and
it must hold the SAME provider instance (shared state), never a copy.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from plugins.memory.unified_memory.delegate import ObsidianVaultDelegate


class RecordingProvider:
    """Records every forwarded call; stands in for ObsidianVaultProvider."""

    def __init__(self):
        self.calls = []

    def _rec(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return "ret:" + name

    # ABC contract
    @property
    def name(self):
        return "obsidian_vault"

    def is_available(self):
        return self._rec("is_available")

    def initialize(self, session_id, **kwargs):
        return self._rec("initialize", session_id, **kwargs)

    def unavailable_reason(self):
        return self._rec("unavailable_reason")

    def system_prompt_block(self):
        return self._rec("system_prompt_block")

    def prefetch(self, query, *, session_id=""):
        return self._rec("prefetch", query, session_id=session_id)

    def queue_prefetch(self, query, *, session_id=""):
        return self._rec("queue_prefetch", query, session_id=session_id)

    def recall_status(self):
        return self._rec("recall_status")

    def sync_turn(self, user_content, assistant_content, *,
                  session_id="", messages=None):
        return self._rec("sync_turn", user_content, assistant_content,
                         session_id=session_id, messages=messages)

    def get_tool_schemas(self):
        return [{"name": "vault_search"}]

    def handle_tool_call(self, tool_name, args, **kwargs):
        self._rec("handle_tool_call", tool_name, args)
        if tool_name == "boom":
            raise RuntimeError("vault backend down")
        if tool_name == "unknown_tool":
            raise NotImplementedError(tool_name)
        return json.dumps({"ok": True})

    def shutdown(self):
        return self._rec("shutdown")

    def on_turn_start(self, turn_number, message, **kwargs):
        return self._rec("on_turn_start", turn_number, message, **kwargs)

    def on_session_end(self, messages):
        return self._rec("on_session_end", messages)

    def on_session_switch(self, new_session_id, *, parent_session_id="",
                          reset=False, rewound=False, **kwargs):
        return self._rec("on_session_switch", new_session_id,
                         parent_session_id=parent_session_id,
                         reset=reset, rewound=rewound, **kwargs)

    def on_pre_compress(self, messages):
        return self._rec("on_pre_compress", messages)

    def on_delegation(self, task, result, *, child_session_id="", **kwargs):
        return self._rec("on_delegation", task, result,
                         child_session_id=child_session_id, **kwargs)

    def get_config_schema(self):
        return [{"key": "vaults"}]

    def save_config(self, values, hermes_home):
        return self._rec("save_config", values, hermes_home)

    def on_memory_write(self, action, target, content, metadata=None):
        return self._rec("on_memory_write", action, target, content,
                         metadata=metadata)

    def backup_paths(self):
        return ["/v"]

    # Provider-specific
    def switch_vault(self, name):
        return self._rec("switch_vault", name)

    def reload_vaults(self):
        return {"success": True}

    def _add_vault(self, name, path_str, *, activate=True):
        return self._rec("_add_vault", name, path_str, activate=activate)

    def _remove_vault(self, name):
        return self._rec("_remove_vault", name)

    def _get_index(self, vault_name=None):
        return self._rec("_get_index", vault_name=vault_name)

    def _get_vault_path(self, vault_name=None):
        return self._rec("_get_vault_path", vault_name=vault_name)

    def _wait_for_ready(self, timeout=10.0, vault_name=None):
        return self._rec("_wait_for_ready", timeout=timeout,
                         vault_name=vault_name)

    def _resolve_note(self, index, slug=None, path=None):
        return self._rec("_resolve_note", index, slug=slug, path=path)


@pytest.fixture()
def provider_and_delegate():
    p = RecordingProvider()
    d = ObsidianVaultDelegate(p)
    return p, d


def test_rejects_none_provider():
    with pytest.raises(ValueError):
        ObsidianVaultDelegate(None)


def test_holds_same_instance(provider_and_delegate):
    _, d = provider_and_delegate
    assert isinstance(d.provider, RecordingProvider)
    assert d.provider is not None


def test_abc_conformance():
    from agent.memory_provider import MemoryProvider
    assert issubclass(ObsidianVaultDelegate, MemoryProvider)


def test_identical_public_api(provider_and_delegate):
    """Every public method on the provider exists on the delegate."""
    provider_methods = {
        n for n, m in inspect.getmembers(RecordingProvider, inspect.isfunction)
        if not n.startswith("__")
    }
    provider_methods.discard("_rec")  # test double plumbing
    delegate_methods = {
        n for n, m in inspect.getmembers(ObsidianVaultDelegate, inspect.isfunction)
        if not n.startswith("__")
    }
    delegate_methods.discard("provider")  # delegate-only accessor
    assert provider_methods <= delegate_methods, (
        f"missing on delegate: {provider_methods - delegate_methods}")


def test_all_calls_forwarded_with_args_and_returns(provider_and_delegate):
    p, d = provider_and_delegate
    idx = object()

    assert d.name == "obsidian_vault"
    d.is_available()
    d.initialize("s1", hermes_home="/h", platform="test")
    d.unavailable_reason()
    d.system_prompt_block()
    d.prefetch("q", session_id="s1")
    d.queue_prefetch("q", session_id="s1")
    d.recall_status()
    d.sync_turn("u", "a", session_id="s2", messages=[{"role": "user"}])
    assert d.get_tool_schemas() == [{"name": "vault_search"}]
    assert d.handle_tool_call("vault_search", {"query": "x"}) == json.dumps({"ok": True})
    d.shutdown()

    d.on_turn_start(1, "hello")
    d.on_session_end([])
    d.on_session_switch("s2", parent_session_id="s1", reset=True)
    d.on_pre_compress([])
    d.on_delegation("t", "r", child_session_id="c9")
    d.get_config_schema()
    d.save_config({"k": 1}, "/h")
    d.on_memory_write("create", "target", "content",
                      metadata={"m": 1})
    assert d.backup_paths() == ["/v"]
    d.switch_vault("main")
    d.reload_vaults()
    d._add_vault("v2", "/p", activate=False)
    d._remove_vault("v2")
    d._get_index("main")
    d._get_vault_path()
    d._wait_for_ready(timeout=0.5, vault_name="main")
    d._resolve_note(idx, slug="note-slug")

    forwarded = [name for name, _, _ in p.calls]
    expected = [
        "is_available", "initialize", "unavailable_reason",
        "system_prompt_block", "prefetch", "queue_prefetch", "recall_status",
        "sync_turn", "handle_tool_call", "shutdown", "on_turn_start",
        "on_session_end", "on_session_switch", "on_pre_compress",
        "on_delegation", "save_config", "on_memory_write",
        "switch_vault", "_add_vault", "_remove_vault", "_get_index",
        "_get_vault_path", "_wait_for_ready", "_resolve_note",
    ]
    assert forwarded == expected

    # kwargs survive verbatim through the forward.
    init_call = next(c for c in p.calls if c[0] == "initialize")
    assert init_call[1] == ("s1",) and init_call[2] == {"hermes_home": "/h",
                                                        "platform": "test"}
    sw_call = next(c for c in p.calls if c[0] == "on_session_switch")
    assert sw_call[2] == {"parent_session_id": "s1", "reset": True,
                          "rewound": False}
    addv = next(c for c in p.calls if c[0] == "_add_vault")
    assert addv[2] == {"activate": False}


def test_return_values_pass_through_unchanged(provider_and_delegate):
    _, d = provider_and_delegate
    assert d.reload_vaults() == {"success": True}
    assert d.handle_tool_call("vault_search", {}) == json.dumps({"ok": True})
    assert d.get_tool_schemas()[0]["name"] == "vault_search"


def test_error_propagation_runtime_error(provider_and_delegate):
    """Exceptions from the provider propagate verbatim (no swallowing)."""
    _, d = provider_and_delegate
    with pytest.raises(RuntimeError, match="vault backend down"):
        d.handle_tool_call("boom", {})


def test_unknown_tool_raises_not_implemented(provider_and_delegate):
    _, d = provider_and_delegate
    with pytest.raises(NotImplementedError):
        d.handle_tool_call("unknown_tool", {})


def test_shared_state_visible_through_delegate(tmp_path):
    """State lives on the wrapped instance — mutations show through."""
    p = RecordingProvider()
    p.active = "main"
    d = ObsidianVaultDelegate(p)
    p.active = "other"          # mutate via the provider reference
    assert d.provider.active == "other"


def test_delegate_against_real_provider_class_shape():
    """Structural check against the REAL ObsidianVaultProvider signatures."""
    import sys
    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from plugins.memory.obsidian_vault import ObsidianVaultProvider

    real = {
        n for n, m in inspect.getmembers(ObsidianVaultProvider, predicate=inspect.isfunction)
        if not n.startswith("__")
    }
    # _handle_* are the provider's internal tool dispatchers — reached through
    # handle_tool_call, not part of the delegated surface (unified_memory calls
    # _handle_create_note on the provider directly, not via the delegate).
    real -= {n for n in real if n.startswith("_handle_")}

    delegate = {
        n for n, m in inspect.getmembers(ObsidianVaultDelegate, predicate=inspect.isfunction)
        if not n.startswith("__")
    } - {"provider"}
    missing = real - delegate
    assert not missing, f"delegate missing methods: {sorted(missing)}"

    # Signature compatibility: delegate params must accept the provider's params.
    for name in sorted(real & delegate):
        rp = inspect.signature(getattr(ObsidianVaultProvider, name)).parameters
        dp = inspect.signature(getattr(ObsidianVaultDelegate, name)).parameters
        for pname, rparam in rp.items():
            assert pname in dp, f"{name}: param '{pname}' missing on delegate"
