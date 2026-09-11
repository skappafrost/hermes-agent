"""ObsidianVaultDelegate — typed pass-through wrapper around ObsidianVaultProvider.

Holds the SAME provider instance (never a copy): all state (_vaults,
_active_vault, ...) lives on the wrapped object, so one shared instance per
process stays the single source of truth. Every call is forwarded unchanged;
return values and exceptions propagate verbatim (JSON-string tool results,
NotImplementedError on unknown tools, readiness gating inside handlers).
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus

logger = logging.getLogger(__name__)


class AgentMemoryFallback:
    """Circuit-breaker wrapper around an AgentMemory client.

    Tracks consecutive call failures; after ``max_failures`` the wrapper enters
    ``obsidian_only`` mode: further calls short-circuit (returning the client's
    "not written / no results" value) so vault operations continue unaffected,
    and the remote service is re-probed at most once per ``probe_interval_s``.
    A healthy probe restores ``unified`` mode. Never raises.
    """

    def __init__(self, client: Any, max_failures: int = 2,
                 probe_interval_s: float = 60.0) -> None:
        self._client = client
        self.max_failures = max(1, int(max_failures))
        self.probe_interval_s = float(probe_interval_s)
        self.mode = "unified"          # "unified" | "obsidian_only"
        self.reason = ""
        self._failures = 0
        self._degraded_at = 0.0
        # Exposed for callers that need per-call health signal (e.g. prefetch).
        self.last_error: Optional[Exception] = None

    # -- State machine ---------------------------------------------------------

    def _enter_obsidian_only(self, reason: str) -> None:
        self.mode = "obsidian_only"
        self.reason = reason
        self._degraded_at = time.monotonic()
        logger.warning(
            "unified_memory: AgentMemory unavailable (%s) after %d consecutive "
            "failure(s) — switching to Obsidian-only mode; all vault operations "
            "continue via Obsidian storage.", reason, self._failures)

    def _recover(self) -> None:
        self.mode = "unified"
        self.reason = ""
        self._failures = 0
        logger.info("unified_memory: AgentMemory healthy again — unified mode restored.")

    def _maybe_reprobe(self) -> None:
        if self.mode != "obsidian_only":
            return
        if time.monotonic() - self._degraded_at < self.probe_interval_s:
            return
        try:
            healthy = bool(self._client.health())
        except Exception as e:
            logger.debug("unified_memory: AgentMemory re-probe failed: %s", e)
            healthy = False
        if healthy:
            self._recover()
        else:
            self._degraded_at = time.monotonic()

    def _record(self, ok: bool, op: str, err: Any = None) -> bool:
        """Update failure counters; True iff caller may proceed with the result."""
        if ok:
            if self._failures:
                self._failures = 0
            self.last_error = None
            return True
        self._failures += 1
        self.last_error = err
        logger.debug("unified_memory: AgentMemory %s failed (%s); failures=%d",
                     op, err, self._failures)
        if self._failures >= self.max_failures and self.mode == "unified":
            self._enter_obsidian_only(f"service down ({op}: {err})")
        return False

    # -- Wrapped client operations (same shapes as _AgentMemoryClient) ----------

    def remember(self, content: str, **kw) -> Optional[str]:
        self._maybe_reprobe()
        if self.mode == "obsidian_only":
            return None
        try:
            r = self._client.remember(content, **kw)
        except Exception as e:
            self._record(False, "remember", e)
            return None
        return r if self._record(r is not None, "remember", "write rejected") else None

    def smart_search(self, query: str, limit: int = 10,
                     scope: Optional[str] = None) -> List[dict]:
        self._maybe_reprobe()
        self.last_error = None
        if self.mode == "obsidian_only":
            return []
        try:
            try:  # ponytail: legacy client doubles lack the scope kwarg
                r = self._client.smart_search(query, limit=limit, scope=scope)
            except TypeError:
                r = self._client.smart_search(query, limit=limit)
        except Exception as e:
            self._record(False, "smart_search", e)
            return []
        return r if self._record(True, "smart_search") else []

    def forget_by_id(self, memory_id: str, *, scope: Optional[str] = None) -> bool:
        self._maybe_reprobe()
        if self.mode == "obsidian_only":
            return False
        try:
            try:  # ponytail: legacy client doubles lack the scope kwarg
                r = bool(self._client.forget_by_id(memory_id, scope=scope))
            except TypeError:
                r = bool(self._client.forget_by_id(memory_id))
        except Exception as e:
            self._record(False, "forget_by_id", e)
            return False
        return r


class ObsidianVaultDelegate(MemoryProvider):
    """MemoryProvider delegating 1:1 to an ObsidianVaultProvider instance."""

    def __init__(self, provider: Any, agentmemory: Any = None) -> None:
        if provider is None:
            raise ValueError("ObsidianVaultDelegate requires a provider instance.")
        # ponytail: typed as Any (no import cycle / no Protocol) — add a
        # typing.Protocol mirroring the provider surface once a second
        # implementation exists to justify it.
        self._provider = provider
        # Optional circuit-breaker wrapper around an AgentMemory client
        # (AgentMemoryFallback). When absent, AgentMemory integration is
        # disabled and the delegate is pure Obsidian pass-through.
        self._agentmemory = agentmemory

    @property
    def provider(self) -> Any:
        """The wrapped provider instance (same object passed to __init__)."""
        return self._provider

    # -- AgentMemory fallback state ---------------------------------------------

    @property
    def agentmemory_mode(self) -> str:
        """"unified" | "obsidian_only" | "disabled" (no AgentMemory wired)."""
        if self._agentmemory is None:
            return "disabled"
        return getattr(self._agentmemory, "mode", "unified")

    def call_agentmemory(self, op: str, *args, **kwargs) -> Any:
        """Route one AgentMemory operation through the fallback wrapper.

        Returns the client result, or None/[] when degraded (Obsidian-only
        mode). Never raises — vault operations are unaffected by AM outages.
        """
        if self._agentmemory is None:
            return None
        try:
            fn = getattr(self._agentmemory, op)
            return fn(*args, **kwargs)
        except Exception as e:
            logger.warning("unified_memory: AgentMemory %s error: %s "
                           "(vault operations continue via Obsidian storage)", op, e)
            return None

    # -- MemoryProvider ABC contract -----------------------------------------

    @property
    def name(self) -> str:
        return self._provider.name

    def is_available(self) -> bool:
        return self._provider.is_available()

    def initialize(self, session_id: str, **kwargs) -> None:
        self._provider.initialize(session_id, **kwargs)

    def unavailable_reason(self) -> str:
        return self._provider.unavailable_reason()

    def system_prompt_block(self) -> str:
        return self._provider.system_prompt_block()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return self._provider.prefetch(query, session_id=session_id)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self._provider.queue_prefetch(query, session_id=session_id)

    def recall_status(self) -> Optional[RecallStatus]:
        return self._provider.recall_status()

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "",
                  messages: Optional[List[Dict[str, Any]]] = None) -> None:
        self._provider.sync_turn(user_content, assistant_content,
                                 session_id=session_id, messages=messages)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return self._provider.get_tool_schemas()

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        return self._provider.handle_tool_call(tool_name, args, **kwargs)

    def shutdown(self) -> None:
        self._provider.shutdown()

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._provider.on_turn_start(turn_number, message, **kwargs)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        self._provider.on_session_end(messages)

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False,
                          **kwargs) -> None:
        self._provider.on_session_switch(
            new_session_id, parent_session_id=parent_session_id,
            reset=reset, rewound=rewound, **kwargs)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        return self._provider.on_pre_compress(messages)

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        self._provider.on_delegation(task, result,
                                     child_session_id=child_session_id, **kwargs)

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return self._provider.get_config_schema()

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> bool:
        return self._provider.save_config(values, hermes_home)

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        self._provider.on_memory_write(action, target, content, metadata)

    def backup_paths(self) -> List[str]:
        return self._provider.backup_paths()

    # -- Provider-specific vault management -----------------------------------

    def switch_vault(self, name: str) -> bool:
        return self._provider.switch_vault(name)

    def reload_vaults(self) -> Dict[str, Any]:
        return self._provider.reload_vaults()

    # -- Private-but-reachable helpers (tool layer reaches through) ------------

    def _add_vault(self, name: str, path_str: str, *,
                   activate: bool = True) -> Dict[str, Any]:
        return self._provider._add_vault(name, path_str, activate=activate)

    def _remove_vault(self, name: str) -> Dict[str, Any]:
        return self._provider._remove_vault(name)

    def _get_index(self, vault_name: Optional[str] = None) -> Optional[Any]:
        return self._provider._get_index(vault_name)

    def _handle_create_note(self, args: Dict[str, Any], index: Any = None) -> str:
        return self._provider._handle_create_note(args, index=index)

    def _get_vault_path(self, vault_name: Optional[str] = None) -> Optional[Path]:
        return self._provider._get_vault_path(vault_name)

    def _wait_for_ready(self, timeout: float = 10.0,
                        vault_name: Optional[str] = None) -> bool:
        return self._provider._wait_for_ready(timeout=timeout, vault_name=vault_name)

    def _resolve_note(self, index: Any, slug: Optional[str] = None,
                      path: Optional[str] = None) -> Optional[Any]:
        return self._provider._resolve_note(index, slug=slug, path=path)
