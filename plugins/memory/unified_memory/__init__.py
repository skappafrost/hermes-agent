"""Unified memory provider — Obsidian Vault (long-term) + AgentMemory (working).

Fronts two heterogeneous stores behind a single MemoryProvider:

- Obsidian Vault: durable, markdown-based long-term knowledge (delegates to the
  in-tree ``obsidian_vault`` provider instance).
- AgentMemory: fast agent-oriented working memory via its local REST API
  (default ``http://127.0.0.1:3111``, see AGENTMEMORY_BASE_URL).

Reads fan out to both sources and merge results deterministically (field
precedence per the unified-memory design docs; dedupe on canonical id; stable
ordering). Writes go to AgentMemory first, then the vault — if the vault write
fails after AgentMemory succeeded, the AgentMemory record is compensated away
so no orphan is left behind.

Activated like any exclusive provider:

    memory:
      provider: unified_memory

Design references: kanban board ``unified_memory`` tasks t_ccfd7875 (ADR),
t_9ab19a70 (paths + conflict rules), t_aa1715a0 (merging strategy).
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider, RecallStatus

from plugins.memory.unified_memory.identity import resolve_identity
from plugins.memory.unified_memory.merger import ResultMerger
from plugins.memory.unified_memory.scope import DEFAULT_SCOPE
from plugins.memory.unified_memory.contract import (
    MergeConfig,
    MergeStrategy,
    OrderKey,
    ProviderResult,
)

logger = logging.getLogger(__name__)

DEFAULT_AGENTMEMORY_URL = "http://127.0.0.1:3111"
# prefetch() runs on the turn thread with an 8s manager-side timeout; keep the
# remote leg well under it and cache the rest.
AGENTMEMORY_TIMEOUT_S = 3.0
# Per-source prefetch timeout: vault leg + AgentMemory leg each get this
# budget (config key plugins.unified_memory.prefetch_timeout_s). Kept well
# under the 8s manager-side prefetch timeout.
PREFETCH_TIMEOUT_S = 5.0
_PREFETCH_LIMIT = 5


def _json_serialize(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _normalize_tags(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    seen = set()
    for tag in raw:
        if not isinstance(tag, str):
            continue
        clean = tag.strip().lstrip("#").lower()
        if clean:
            seen.add(clean)
    return sorted(seen)


class _AgentMemoryClient:
    """Thin synchronous REST client for the local AgentMemory service.

    Every request carries the resolved identity (agentId/teamId/userId) as
    headers plus a ``scope`` body field. Identity is resolved at call time
    (never import time) so tests can set env vars per-call; when identity is
    unresolvable the request proceeds without identity headers — same fallback
    behavior as before this upgrade.
    """

    def __init__(self, base_url: str, timeout: float = AGENTMEMORY_TIMEOUT_S,
                 scope: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Default scope preserves pre-identity behavior (see scope.DEFAULT_SCOPE).
        self.scope = scope or DEFAULT_SCOPE

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> Optional[dict]:
        url = f"{self.base_url}/agentmemory/{path.lstrip('/')}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        try:
            ident = resolve_identity()
        except ValueError:
            ident = None  # unresolvable: send without identity, keep old fallback path
        if ident is not None:
            headers.update({
                "X-Agent-Id": ident.agent_id,
                "X-Team-Id": ident.team_id,
                "X-User-Id": ident.user_id,
            })
            # Identity/scope payload only on bodied requests (GETs stay clean).
            if body is not None:
                body = dict(body)
                body.setdefault("agentId", ident.agent_id)
                body.setdefault("teamId", ident.team_id)
                body.setdefault("userId", ident.user_id)
                body.setdefault("scope", self.scope)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read().decode("utf-8")
                return json.loads(payload) if payload else {}
        except urllib.error.HTTPError as e:
            logger.debug("agentmemory %s %s -> HTTP %s", method, path, e.code)
            return None
        except Exception as e:
            logger.debug("agentmemory %s %s failed: %s", method, path, e)
            return None

    def health(self) -> bool:
        resp = self._request("GET", "health")
        return bool(resp and resp.get("status") == "healthy")

    def remember(self, content: str, *, title: str = "", type_: str = "fact",
                 concepts: Optional[List[str]] = None, metadata: Optional[dict] = None,
                 scope: Optional[str] = None) -> Optional[str]:
        body: Dict[str, Any] = {"content": content, "type": type_}
        if title:
            body["title"] = title
        if concepts:
            body["concepts"] = concepts
        if metadata:
            body["metadata"] = metadata
        if scope:
            body["scope"] = scope
        resp = self._request("POST", "remember", body)
        if not resp:
            return None
        memory = resp.get("memory") or {}
        return (memory.get("id") or resp.get("id") or resp.get("obsId")
                or resp.get("memoryId") or resp.get("memory_id"))

    def smart_search(self, query: str, limit: int = 10,
                     scope: Optional[str] = None) -> List[dict]:
        body = {"query": query, "limit": limit}
        if scope:
            body["scope"] = scope
        resp = self._request("POST", "smart-search", body)
        if not resp:
            return []
        return resp.get("results") or []

    def forget_by_id(self, memory_id: str, *, scope: Optional[str] = None) -> bool:
        # Current AM: /forget takes {"memoryId"}; older builds exposed
        # governance/bulk-delete or {"id"}. Try current first, then legacy.
        extra = {"scope": scope} if scope else {}
        resp = self._request("POST", "forget", {"memoryId": memory_id, **extra})
        if resp is None:
            resp = self._request("POST", "governance/bulk-delete",
                                 {"memoryIds": [memory_id],
                                  "reason": "unified_memory compensation", **extra})
        if resp is None:
            resp = self._request("POST", "forget", {"id": memory_id, **extra})
        return resp is not None


class UnifiedMemoryProvider(MemoryProvider):
    """Aggregates the Obsidian vault and AgentMemory behind one provider."""

    def __init__(self, config: Optional[dict] = None) -> None:
        self._config = config or {}
        self._lock = threading.RLock()
        self._vault_provider: Optional[Any] = None
        client = _AgentMemoryClient(self._config.get("agentmemory_url") or DEFAULT_AGENTMEMORY_URL)
        from plugins.memory.unified_memory.delegate import AgentMemoryFallback
        # Circuit breaker in front of the raw client: consecutive failures
        # degrade to Obsidian-only mode (logged), re-probe recovers.
        self._amfb = AgentMemoryFallback(client)
        self.agentmemory_fallback = self._amfb  # public for status/monitoring
        # ScopeManager decides isolated vs shared per observation category
        # (config key plugins.unified_memory.shared_categories).
        from plugins.memory.unified_memory.scope import ScopeManager
        self._scope_mgr = ScopeManager(config.get("scope") or config)
        self._initialized = False
        self._session_id = ""
        self._hermes_home = ""
        self._platform = ""
        self._agent_context = "primary"
        # Last-prefetch bookkeeping for recall_status().
        self._last_status: Optional[RecallStatus] = None
        self._last_context_len = 0
        # Per-source outcome of the last prefetch (ok/timeout/error each).
        self._last_prefetch_sources: Dict[str, dict] = {}
        # Per-source prefetch timeout (seconds); each source gets its own
        # budget so a hanging source never delays the healthy one.
        self._prefetch_timeout_s = float(
            (config or {}).get("prefetch_timeout_s", PREFETCH_TIMEOUT_S))
        # Idempotency store: unified_id -> operation record (write path).
        self._ops: Dict[str, dict] = {}

    # -- Identity -------------------------------------------------------------

    @property
    def _am(self):
        """Raw AgentMemory client; writing through also re-targets the
        circuit breaker so test doubles swap both in one assignment."""
        return self._amfb._client

    @_am.setter
    def _am(self, client) -> None:
        self._amfb._client = client

    def _scoped_remember(self, content: str, *, category: str = "",
                         **kw) -> Optional[str]:
        """remember() with the observation's scope resolved from its category.

        Scope comes from ScopeManager (isolated by default; shared only for
        configured shared_categories). Identity (agentId/teamId/userId) is
        attached inside _AgentMemoryClient._request at call time.
        """
        scope = self._scope_mgr.tag_scope(category) if category else DEFAULT_SCOPE
        return self._amfb.remember(content, scope=scope, **kw)


    @property
    def name(self) -> str:
        return "unified_memory"

    def unavailable_reason(self) -> str:
        try:
            from plugins.memory.obsidian_vault import ObsidianVaultProvider, _load_plugin_config
            config = self._config.get("obsidian_vault") or _load_plugin_config()
            probe = ObsidianVaultProvider(config=config)
            if not probe.is_available():
                return ("no Obsidian vaults configured (set plugins.obsidian_vault.vaults "
                        "in config.yaml); AgentMemory alone is not sufficient for unified mode")
        except Exception:
            pass
        return ""

    def is_available(self) -> bool:
        """Cheap, synchronous, network-free readiness check."""
        try:
            from hermes_constants import get_hermes_home
            from plugins.memory.obsidian_vault import ObsidianVaultProvider, _load_plugin_config
            config = self._config.get("obsidian_vault") or _load_plugin_config()
            probe = ObsidianVaultProvider(config=config)
            return probe.is_available()
        except Exception:
            return False

    # -- Lifecycle ------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        self._hermes_home = kwargs.get("hermes_home", "") or ""
        self._platform = kwargs.get("platform", "") or ""
        self._agent_context = kwargs.get("agent_context", "primary")
        self._session_id = session_id
        try:
            from plugins.memory import obsidian_vault as _ov
            from plugins.memory.unified_memory.delegate import ObsidianVaultDelegate
            config = self._config.get("obsidian_vault") or _ov._load_plugin_config()
            # Wrap the raw provider in the delegate (same shared instance):
            # 1:1 pass-through plus AgentMemory fallback state visibility.
            delegate = ObsidianVaultDelegate(_ov.ObsidianVaultProvider(config=config),
                                             agentmemory=self._amfb)
            delegate.initialize(session_id, **kwargs)
            self._vault_provider = delegate
        except Exception as e:
            logger.warning("unified_memory: obsidian delegate failed to initialize: %s", e)
            self._vault_provider = None
        self._initialized = True
        logger.info(
            "unified_memory: initialized (session=%s platform=%s context=%s agentmemory=%s)",
            session_id, self._platform, self._agent_context, self._am.base_url,
        )

    def shutdown(self) -> None:
        # Safe when initialize() never ran.
        with self._lock:
            delegate, self._vault_provider = self._vault_provider, None
            self._initialized = False
            self._last_status = None
        if delegate is not None:
            try:
                delegate.shutdown()
            except Exception as e:
                logger.warning("unified_memory: vault delegate shutdown error: %s", e)

    # -- System prompt ----------------------------------------------------------

    def system_prompt_block(self) -> str:
        block = ""
        if self._vault_provider is not None:
            try:
                block = self._vault_provider.system_prompt_block()
            except Exception as e:
                logger.debug("unified_memory: vault system_prompt_block failed: %s", e)
        am_up = self._amfb.mode != "obsidian_only" if self._initialized else False
        parts = [p for p in (block.strip(), f"[AgentMemory: {'online' if am_up else 'offline'}]" if block or am_up else "") if p]
        return "\n".join(parts)

    # -- Recall -----------------------------------------------------------------

    def _fetch_agentmemory(self, query: str) -> List[dict]:
        # Use the circuit-breaker wrapper so test fakes (legacy smart_search
        # signature) and degraded mode are handled uniformly.
        return self._amfb.smart_search(query, limit=_PREFETCH_LIMIT,
                                       scope=DEFAULT_SCOPE)

    def _fetch_agentmemory_status(self) -> dict:
        """Return status of the most recent AgentMemory call for prefetch.

        Must be called immediately after _fetch_agentmemory; it inspects the
        circuit-breaker wrapper's last_error to distinguish a graceful empty
        result (ok) from an captured exception (error) or degraded mode.
        """
        if self._amfb.mode == "obsidian_only":
            return {"status": "degraded", "mode": "obsidian_only"}
        if self._amfb.last_error is not None:
            return {"status": "error", "error": str(self._amfb.last_error)}
        return {"status": "ok"}

    def _fetch_vault(self, query: str, session_id: str) -> str:
        if self._vault_provider is None:
            return ""
        return self._vault_provider.prefetch(query, session_id=session_id)

    def _parse_vault_block(self, vault_block: str) -> List[ProviderResult]:
        """Split a vault prefetch block into discrete merge candidates.

        Sections separated by blank lines are treated as individual items so
        the ResultMerger can interleave them with AgentMemory observations.
        """
        if not vault_block.strip():
            return []
        items: List[ProviderResult] = []
        for section in vault_block.split("\n\n"):
            section = section.strip()
            if not section:
                continue
            items.append(ProviderResult(
                provider_id="obsidian",
                payload=section,
                timestamp=datetime.now(timezone.utc),
            ))
        return items

    def _format_am_results(self, am_results: List[dict]) -> List[ProviderResult]:
        """Convert AgentMemory result dicts into merge candidates."""
        items: List[ProviderResult] = []
        for r in am_results[:_PREFETCH_LIMIT]:
            title = (r.get("title") or "").strip() or "(untitled observation)"
            ts_raw = r.get("timestamp") or ""
            ts: Optional[datetime] = None
            if isinstance(ts_raw, str) and ts_raw:
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    ts = datetime.now(timezone.utc)
            elif isinstance(ts_raw, datetime):
                ts = ts_raw
            items.append(ProviderResult(
                provider_id="agentmemory",
                payload={"title": title, "timestamp": ts_raw},
                timestamp=ts,
                confidence=float(r.get("score") or 0.0),
            ))
        return items

    def _render_merged_items(self, items: List[Any]) -> str:
        """Render merged items back into a prompt-ready string block."""
        if not items:
            return ""
        # Preserve legacy block shape: vault sections first, then a labelled
        # working-memory list for any AgentMemory-derived items.
        vault_lines: List[str] = []
        am_lines: List[str] = []
        for item in items:
            if isinstance(item, ProviderResult):
                payload = item.payload
                if isinstance(payload, str):
                    vault_lines.append(payload)
                elif isinstance(payload, dict) and "title" in payload:
                    title = payload["title"]
                    ts = (payload.get("timestamp") or "").split("T")[0]
                    am_lines.append(f"- {title}" + (f" ({ts})" if ts else ""))
            elif isinstance(item, str):
                vault_lines.append(item)
            elif isinstance(item, dict) and "title" in item:
                title = item["title"]
                ts = (item.get("timestamp") or "").split("T")[0]
                am_lines.append(f"- {title}" + (f" ({ts})" if ts else ""))
        parts: List[str] = []
        if vault_lines:
            parts.append("\n\n".join(vault_lines))
        if am_lines:
            parts.append("[Working memory]\n" + "\n".join(am_lines))
        return "\n\n".join(parts)

    def _merge_prefetch_context(self, vault_block: str,
                                am_results: List[dict]) -> str:
        """Merge vault + AgentMemory results via ResultMerger."""
        merger = ResultMerger(default_limit=_PREFETCH_LIMIT)
        partials = self._parse_vault_block(vault_block) + self._format_am_results(am_results)
        config = MergeConfig(
            strategy=MergeStrategy.UNION,
            order_key=OrderKey.PROVIDER_PRIORITY,
            provider_priority=["obsidian", "agentmemory"],
            max_results=_PREFETCH_LIMIT,
        )
        merged = merger.merge(partials, config)
        return self._render_merged_items(merged.items)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Merged recall from both stores. Fast paths only; failures degrade quietly.

        Both sources are fetched concurrently (ThreadPoolExecutor, 2 workers)
        with an independent per-source timeout: one slow/failed source never
        blocks or kills the other. When both sources succeed, results are
        merged deterministically via ResultMerger. When AgentMemory times out
        or errors, the provider degrades gracefully to a single-source
        vault-only block (logged at warning level). Per-source outcome
        (ok/timeout/error) lands in self._last_prefetch_sources.
        """
        if not query or not query.strip():
            self._remember_recall(0, "")
            return ""

        timeout = self._prefetch_timeout_s
        am_results: List[dict] = []
        vault_block = ""
        sources: Dict[str, dict] = {}
        # Manual lifecycle (not ``with``): __exit__ joins workers, which would
        # wait out a hung source and defeat the whole point of the timeout.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        fut_am = pool.submit(self._fetch_agentmemory, query)
        fut_vault = pool.submit(self._fetch_vault, query, session_id)
        try:
            try:
                am_results = fut_am.result(timeout=timeout)
                sources["agentmemory"] = self._fetch_agentmemory_status()
            except concurrent.futures.TimeoutError:
                sources["agentmemory"] = {"status": "timeout",
                                          "timeout_s": timeout}
                logger.debug("unified_memory: agentmemory prefetch timed "
                             "out after %.1fs", timeout)
            except Exception as e:
                sources["agentmemory"] = {"status": "error", "error": str(e)}
                logger.debug("unified_memory: agentmemory search failed: %s", e)
            try:
                vault_block = fut_vault.result(timeout=timeout)
                sources["vault"] = {"status": "ok"}
            except concurrent.futures.TimeoutError:
                sources["vault"] = {"status": "timeout", "timeout_s": timeout}
                logger.debug("unified_memory: vault prefetch timed out after %.1fs",
                             timeout)
            except Exception as e:
                sources["vault"] = {"status": "error", "error": str(e)}
                logger.debug("unified_memory: vault prefetch failed: %s", e)
        finally:
            # Abandoned timed-out threads die on their own; never join them.
            pool.shutdown(wait=False, cancel_futures=True)

        self._last_prefetch_sources = sources

        # Decide merge vs. graceful degradation.
        am_ok = sources.get("agentmemory", {}).get("status") == "ok"
        vault_ok = sources.get("vault", {}).get("status") == "ok"

        if am_ok and vault_ok:
            context = self._merge_prefetch_context(vault_block, am_results)
        elif vault_ok:
            # AgentMemory degraded: single-source vault-only block.
            logger.warning(
                "unified_memory: AgentMemory prefetch %s; degrading to "
                "Obsidian-only block for query=%r",
                sources["agentmemory"].get("status", "unknown"),
                query,
            )
            context = vault_block
        elif am_ok:
            # Vault unavailable: AgentMemory-only block.
            context = self._render_merged_items(
                self._format_am_results(am_results)
            )
        else:
            context = ""

        count = len(am_results) + (1 if vault_block else 0)
        self._remember_recall(count, context)
        return context

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._vault_provider is not None:
            try:
                self._vault_provider.queue_prefetch(query, session_id=session_id)
            except Exception as e:
                logger.debug("unified_memory: vault queue_prefetch failed: %s", e)

    def recall_status(self) -> Optional[RecallStatus]:
        with self._lock:
            return self._last_status

    def _remember_recall(self, count: int, context: str) -> None:
        with self._lock:
            self._last_context_len = len(context)
            self._last_status = (
                RecallStatus(provider_label="unified", count=count)
                if count > 0 else None
            )

    # -- Persistence --------------------------------------------------------------

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "",
                  messages: Optional[List[Dict[str, Any]]] = None) -> None:
        if self._agent_context != "primary":
            return
        # Working copy into AgentMemory; the vault stays curated (no raw turns).
        try:
            snippet = f"{user_content}\n---\n{assistant_content}"
            if len(snippet) > 4000:
                snippet = snippet[:4000] + "…"
            # sync_turn observations are categorized as raw turn transcripts.
            # They default to the isolated scope per agent_id via ScopeManager.
            memory_id = self._scoped_remember(
                snippet,
                title=f"turn {session_id or self._session_id}",
                type_="observation",
                category="turn",
            )
            if memory_id:
                logger.debug("unified_memory: sync_turn persisted id=%s", memory_id)
            # Retrieval self-check: confirm the same identity can find the
            # observation just written. Never block the turn on this; failures
            # are logged and swallowed so the turn loop is unaffected.
            if memory_id:
                try:
                    hits = self._amfb.smart_search(
                        snippet[:160], limit=3, scope=self._scope_mgr.tag_scope("turn")
                    )
                    if not any((r.get("id") or r.get("obsId")) == memory_id for r in hits):
                        logger.debug("unified_memory: sync_turn retrieval self-check "
                                     "did not surface id=%s immediately", memory_id)
                except Exception as e:
                    logger.debug("unified_memory: sync_turn retrieval self-check failed: %s", e)
        except Exception as e:
            logger.debug("unified_memory: sync_turn remember failed: %s", e)

    # -- Tools ---------------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        schemas: List[Dict[str, Any]] = []
        if self._vault_provider is not None:
            try:
                schemas.extend(self._vault_provider.get_tool_schemas())
            except Exception as e:
                logger.warning("unified_memory: vault tool schemas unavailable: %s", e)
        schemas.append({
            "name": "memory_unified_write",
            "description": (
                "Write one memory to BOTH stores (Obsidian vault note + AgentMemory). "
                "Use for durable facts worth keeping in the long-term knowledge base."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Note/memory title."},
                    "content": {"type": "string", "description": "Markdown body content."},
                    "tags": {"type": "array", "items": {"type": "string"},
                             "description": "Optional tags."},
                    "category": {"type": "string", "description": "Optional note category."},
                },
                "required": ["title", "content"],
            },
        })
        schemas.append({
            "name": "memory_unified_search",
            "description": (
                "Search BOTH memory stores at once (Obsidian vault + AgentMemory working "
                "memory) and return merged, deduplicated results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "limit": {"type": "integer", "description": "Max merged results (default 10, max 50)."},
                },
                "required": ["query"],
            },
        })
        return schemas

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        # Vault tools are delegated verbatim to the obsidian_vault provider.
        if tool_name.startswith("vault_"):
            if self._vault_provider is None:
                return json.dumps({"error": "Obsidian vault backend unavailable."})
            try:
                return self._vault_provider.handle_tool_call(tool_name, args, **kwargs)
            except NotImplementedError:
                raise
            except Exception as e:
                logger.warning("unified_memory: vault tool %s failed: %s", tool_name, e)
                return json.dumps({"error": f"vault tool failed: {e}"})
        if tool_name == "memory_unified_write":
            return json.dumps(self._tool_unified_write(args), default=_json_serialize)
        if tool_name == "memory_unified_search":
            return json.dumps(self._tool_unified_search(args), default=_json_serialize)
        raise NotImplementedError(f"unified_memory does not handle tool {tool_name}")

    # -- Unified write path (2PC-lite: AgentMemory first, then vault, compensate) --

    def _tool_unified_write(self, args: Dict[str, Any]) -> Dict[str, Any]:
        title = str(args.get("title", "")).strip()
        content = str(args.get("content", "")).strip()
        if not title or not content:
            return {"error": "Both 'title' and 'content' are required."}
        tags = _normalize_tags(args.get("tags"))
        category = args.get("category")
        unified_id = str(uuid.uuid4())
        op: dict = {
            "idempotency_key": unified_id,
            "status": "preparing",
            "obsidian": {"status": "pending", "slug": None, "error": None},
            "agentmemory": {"status": "pending", "memory_id": None, "error": None},
            "created_at": _now_iso(),
        }
        with self._lock:
            self._ops[unified_id] = op

        # Phase 1: AgentMemory (fast store, fail-fast). None when degraded →
        # Obsidian-only mode: vault write still proceeds and is the SoT.
        memory_id = self._scoped_remember(
            content,
            title=title,
            category=category if isinstance(category, str) else "",
            concepts=tags or None,
            metadata={"unified_id": unified_id, "category": category},
        )
        if memory_id:
            op["agentmemory"] = {"status": "written", "memory_id": memory_id, "error": None}
        elif self._amfb.mode == "obsidian_only":
            op["agentmemory"] = {"status": "skipped", "memory_id": None,
                                 "error": f"AgentMemory unavailable ({self._amfb.reason}) "
                                          "- Obsidian-only fallback active"}
        else:
            op["agentmemory"] = {"status": "failed", "memory_id": None, "error": "agentmemory write failed"}

        # Phase 2: vault note (durable source of truth).
        slug = self._write_vault_note(title, content, tags, category, unified_id)
        if slug:
            op["obsidian"] = {"status": "written", "slug": slug, "error": None}
        else:
            op["obsidian"] = {"status": "failed", "slug": None, "error": "vault write failed"}

        # Compensation: vault succeeded but AgentMemory failed → retry once; still
        # failing → keep the durable vault record (SoT) and report partial.
        # AgentMemory succeeded but vault failed → compensate-delete the AM record.
        if op["obsidian"]["status"] == "written" and op["agentmemory"]["status"] != "written":
            retry = self._scoped_remember(content, title=title,
                                          category=category if isinstance(category, str) else "",
                                          concepts=tags or None,
                                          metadata={"unified_id": unified_id, "category": category})
            if retry:
                op["agentmemory"] = {"status": "written", "memory_id": retry, "error": None}
        if op["agentmemory"]["status"] == "written" and op["obsidian"]["status"] != "written":
            memory_id = op["agentmemory"]["memory_id"]
            scope = self._scope_mgr.tag_scope(category) if isinstance(category, str) and category else DEFAULT_SCOPE
            if memory_id and self._amfb.forget_by_id(memory_id, scope=scope):
                op["agentmemory"] = {"status": "rolled_back", "memory_id": None, "error": None}

        if op["obsidian"]["status"] == "written" and op["agentmemory"]["status"] == "written":
            op["status"] = "completed"
        elif op["obsidian"]["status"] == "written" or op["agentmemory"]["status"] == "written":
            op["status"] = "partial"
        else:
            op["status"] = "failed"

        op["completed_at"] = _now_iso()
        result = {k: v for k, v in op.items() if k != "idempotency_key"}
        result["unified_id"] = unified_id
        return result

    def _write_vault_note(self, title: str, content: str, tags: List[str],
                          category: Any, unified_id: str) -> Optional[str]:
        if self._vault_provider is None:
            return None
        frontmatter = {
            "unified_id": unified_id,
            "content_hash": _content_hash(content),
            "updated_at": _now_iso(),
            "version": 1,
            "source": "unified_memory",
        }
        try:
            handler = getattr(self._vault_provider, "_handle_create_note", None)
            index = self._vault_provider._get_index()
            if handler is None or index is None:
                return None
            result_json = handler(
                {"title": title, "body": content, "frontmatter": frontmatter,
                 "tags": tags, "category": category},
                index=index,
            )
            data = json.loads(result_json)
            if data.get("error"):
                logger.warning("unified_memory: vault create_note error: %s", data["error"])
                return None
            return data.get("slug") or data.get("title")
        except Exception as e:
            logger.warning("unified_memory: vault note write failed: %s", e)
            return None

    # -- Unified read path -------------------------------------------------------

    def _tool_unified_search(self, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            return {"error": "'query' is required."}
        limit = min(max(int(args.get("limit", 10)), 1), 50)

        items: List[Dict[str, Any]] = []
        partial = False

        am_results: List[dict] = []
        try:
            am_results = self._amfb.smart_search(query, limit=limit,
                                                 scope=DEFAULT_SCOPE)
            if self._amfb.mode == "obsidian_only":
                partial = True  # degraded: results are vault-only
        except Exception as e:
            logger.debug("unified_memory: agentmemory search failed: %s", e)
            partial = True

        vault_items: List[Dict[str, Any]] = []
        if self._vault_provider is not None:
            try:
                raw = self._vault_provider.handle_tool_call(
                    "vault_search", {"query": query, "limit": limit})
                data = json.loads(raw)
                if data.get("error"):
                    partial = True
                else:
                    vault_items = [
                        {
                            "id": r.get("slug"),
                            "source": "obsidian",
                            "title": r.get("title") or "",
                            "snippet": r.get("snippet") or "",
                            "uri": r.get("path"),
                            "tags": _normalize_tags(r.get("tags")),
                            "score": float(r.get("score") or 0.0),
                        }
                        for r in (data.get("results") or [])
                    ]
            except Exception as e:
                logger.debug("unified_memory: vault search failed: %s", e)
                partial = True

        items.extend(vault_items)
        items.extend(self._normalize_am(am_results))
        items.sort(key=lambda x: (-x["score"], x["source"], x["id"]))
        items = items[:limit]
        return {
            "query": query,
            "count": len(items),
            "partial": partial,
            "results": items,
        }

    @staticmethod
    def _normalize_am(results: List[dict]) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        for r in results:
            obs_id = r.get("obsId") or r.get("id") or ""
            score = r.get("score")
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 0.0
            items.append({
                "id": obs_id,
                "source": "agentmemory",
                "title": r.get("title") or "(untitled)",
                "snippet": r.get("content") or "",
                "uri": f"agentmemory://memories/{obs_id}" if obs_id else None,
                "tags": _normalize_tags(r.get("concepts")),
                "score": score,
            })
        return items

    # -- Session hooks -----------------------------------------------------------

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._vault_provider is not None:
            try:
                self._vault_provider.on_session_end(messages)
            except Exception as e:
                logger.debug("unified_memory: vault on_session_end failed: %s", e)

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "",
                          reset: bool = False, rewound: bool = False, **kwargs) -> None:
        self._session_id = new_session_id
        if reset:
            with self._lock:
                self._last_status = None
        if self._vault_provider is not None:
            try:
                self._vault_provider.on_session_switch(
                    new_session_id, parent_session_id=parent_session_id,
                    reset=reset, rewound=rewound, **kwargs)
            except Exception as e:
                logger.debug("unified_memory: vault on_session_switch failed: %s", e)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        if self._vault_provider is not None:
            try:
                return self._vault_provider.on_pre_compress(messages)
            except Exception as e:
                logger.debug("unified_memory: vault on_pre_compress failed: %s", e)
        return ""

    def on_delegation(self, task: str, result: str, *,
                      child_session_id: str = "", **kwargs) -> None:
        if self._agent_context != "primary":
            return
        try:
            self._scoped_remember(f"task: {task}\nresult: {result[:2000]}",
                                  title="delegation outcome", type_="observation")
        except Exception as e:
            logger.debug("unified_memory: delegation observe failed: %s", e)

    def on_memory_write(self, action: str, target: str, content: str,
                        metadata: Optional[Dict[str, Any]] = None) -> None:
        # Mirror built-in memory-tool writes: vault keeps the curated copy.
        if self._vault_provider is not None:
            try:
                self._vault_provider.on_memory_write(action, target, content, metadata)
            except Exception as e:
                logger.debug("unified_memory: vault on_memory_write failed: %s", e)

    def backup_paths(self) -> List[str]:
        if self._vault_provider is not None:
            try:
                return self._vault_provider.backup_paths()
            except Exception as e:
                logger.debug("unified_memory: backup_paths failed: %s", e)
        return []

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "agentmemory_url",
                "label": "AgentMemory base URL",
                "kind": "text",
                "description": "Base URL of the local AgentMemory REST service.",
                "default": DEFAULT_AGENTMEMORY_URL,
                "placeholder": DEFAULT_AGENTMEMORY_URL,
                "scope": "global",
                "inline": True,
                "group": "Backend",
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> bool:
        try:
            from hermes_cli.config import load_config, save_config
            all_config = load_config()
            plugins_cfg = all_config.setdefault("plugins", {})
            um_cfg = plugins_cfg.setdefault("unified_memory", {})
            for key in ("agentmemory_url",):
                if key in values:
                    um_cfg[key] = values[key]
            save_config(all_config)
            return True
        except Exception as e:
            logger.error("unified_memory: save_config failed: %s", e)
            return False


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register the unified_memory provider (exclusive activation path)."""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.config import cfg_get
        all_config = load_config_readonly()
        config = cfg_get(all_config, "plugins", "unified_memory", default={}) or {}
    except Exception:
        config = {}
    provider = UnifiedMemoryProvider(config=config)
    ctx.register_memory_provider(provider)
