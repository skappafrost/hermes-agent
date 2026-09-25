"""agentmemory — Hermes MemoryProvider over an agentmemory server.

Two directions, both stdlib-only (urllib) so the plugin declares no pip_dependencies and
`hermes update` can never auto-disable it:

* PUSH (prefetch): each turn we recall relevant memories via POST /agentmemory/search and
  return plain markdown; Hermes fences it in <memory-context> onto the current user message
  (agent/memory_manager.py::build_memory_context_block) — never the system prompt, so prompt
  caching and role alternation hold.
* CAPTURE (sync_turn): each completed turn is posted as a session-linked observation via
  POST /agentmemory/observe (hookType ``prompt_submit``), on the manager's serialized background
  worker, so it never delays the reply. Observations — not POST /remember — are what the engine's
  LLM pipeline reads: AGENTMEMORY_AUTO_COMPRESS distills each one into title/facts/narrative and
  CONSOLIDATION_ENABLED promotes them into semantic memories, so feeding /remember instead stores
  an undistilled transcript and leaves the session (and reflect/lessons/crystals) empty. Failures
  buffer and retry; a final flush runs in on_session_end/shutdown/atexit so a crash loses nothing
  already queued.

Per-profile isolation on a SHARED engine: agentId (= the active profile) is sent on session/start,
and mem::observe inherits it from that session row. mem::search filters by the agentId column
regardless of AGENTMEMORY_AGENT_SCOPE, so push+capture never cross profiles. The 54 MCP-bridge
tools drop agentId, so those still read pool-wide — accepted (see plan). Optionally scope further
by project (= <profile>/<cwd-slug>) when cwd is known.
"""

from __future__ import annotations

import atexit
import datetime as _dt
import json
import logging
import re
import threading
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from agent.memory_provider import (
    MemoryProvider,
    RecallStatus,
    is_trivial_prompt,
    spawn_context_thread,
)
from agent.secret_scope import get_secret

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://127.0.0.1:3111"
_HTTP_TIMEOUT_S = 5.0          # stay well under the manager's 8s external-prefetch cap
_FLUSH_JOIN_S = 4.0           # shutdown() must return within the manager's 5s drain window
_RECALL_LIMIT = 8
_MAX_INJECT_CHARS = 4000
_MAX_USER_CHARS = 4000          # per-side caps; the compression LLM reads what we send
_MAX_ASSISTANT_CHARS = 2000
_MAX_FACT_CHARS = 300           # a distilled fact longer than this is a raw dump, not a fact
_MAX_PENDING = 50             # bounded retry buffer (drop oldest past this)
_FLUSH = object()             # sentinel appended to the retry queue to unblock a waiter

# Built on first use from agent.prompt_builder.CONTROL_FRAME_OPENERS (see _carries_user_words).
_CONTROL_FRAME_RE: Optional[re.Pattern] = None


def _config_block() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        block = load_config_readonly().get("memory", {}).get("agentmemory", {})
    except Exception:
        block = None
    return dict(block) if isinstance(block, dict) else {}


def _carries_user_words(text: str) -> bool:
    """False for empty text and for Hermes' own control frames delivered as role:user rows.

    Mid-loop injections (``/steer``, async-delegation batch reports, runtime/system notes, cron
    payloads) occupy the user slot because that is the only alternation-safe position, so without
    this test they get captured as if the user had said them. ``CONTROL_FRAME_OPENERS``
    (agent/prompt_builder.py) is the canonical list, as regex alternatives after the opening ``[``.
    Import failure must not cost memory coverage: treat everything as real user words.
    """
    if not text.strip():
        return False
    global _CONTROL_FRAME_RE
    if _CONTROL_FRAME_RE is None:
        try:
            from agent.prompt_builder import CONTROL_FRAME_OPENERS
            _CONTROL_FRAME_RE = re.compile(r"^\[?(?:" + "|".join(CONTROL_FRAME_OPENERS) + ")")
        except Exception:
            return True
    return _CONTROL_FRAME_RE.match(text.lstrip()) is None


class AgentMemoryProvider(MemoryProvider):
    def __init__(self) -> None:
        self._base_url = _DEFAULT_BASE_URL
        self._secret = ""
        self._project: Optional[str] = None
        self._agent_id: Optional[str] = None
        self._cwd: Optional[str] = None
        self._use_project = False
        self._capture = True
        self._write_enabled = True
        self._agent_context = "primary"
        self._session_id = ""
        self._lock = threading.Lock()
        self._drain_lock = threading.Lock()   # serializes peek+post+pop of the retry buffer
        self._cache: Dict[Tuple[str, str], str] = {}
        self._pending: list = []          # retry queue of capture bodies
        self._session_open = False        # session/start landed (observations inherit agentId from it)
        self._last_count = 0
        self._atexit_registered = False

    # -- mandatory ABC surface ------------------------------------------------
    @property
    def name(self) -> str:
        return "agentmemory"

    def is_available(self) -> bool:
        return bool(self._resolve_base_url())

    def unavailable_reason(self) -> str:
        return "Set memory.agentmemory.base_url or AGENTMEMORY_URL (agentmemory REST)."

    def get_tool_schemas(self):
        return []

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = session_id or ""
        self._base_url = self._resolve_base_url()
        self._secret = (get_secret("AGENTMEMORY_SECRET", "") or "").strip()
        block = _config_block()
        self._use_project = bool(block.get("project_partition", True))
        self._capture = bool(block.get("auto_capture", True))
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        # cron/subagent/flush must not produce durable writes.
        self._write_enabled = self._agent_context not in {"cron", "subagent", "flush"}
        profile = str(kwargs.get("agent_identity") or "").strip() or "default"
        self._agent_id = profile
        cwd = kwargs.get("cwd") or self._resolve_cwd()
        self._cwd = cwd or None
        self._project = self._compute_project(profile, self._cwd)
        with self._lock:
            self._session_open = False
            self._pending.clear()
        if not self._atexit_registered:
            atexit.register(self._flush_best_effort)
            self._atexit_registered = True

    # -- push recall ----------------------------------------------------------
    def system_prompt_block(self) -> str:
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._agent_context != "primary" or is_trivial_prompt(query):
            return
        key = (session_id or self._session_id, query.strip())
        with self._lock:
            if key in self._cache:
                return
        spawn_context_thread(self._warm, name="agentmemory-prefetch",
                             args=(key[0], query.strip())).start()

    def _warm(self, sid: str, query: str) -> None:
        text = self._recall(query)
        with self._lock:
            self._cache[(sid, query)] = text

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if is_trivial_prompt(query):
            return ""
        sid = session_id or self._session_id
        key = (sid, query.strip())
        with self._lock:
            cached = self._cache.pop(key, None)
        text = cached if cached is not None else self._recall(query)
        self._last_count = text.count("\n- ") if text else 0
        return text

    def recall_status(self) -> Optional[RecallStatus]:
        if not self._last_count:
            return None
        return RecallStatus(provider_label="AgentMemory", count=self._last_count)

    # -- capture --------------------------------------------------------------
    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None,
                  turn_author=None) -> None:
        # Runs on the manager's serialized background worker; blocking here is fine (supermemory
        # pattern) and keeps ordering turn N before N+1.
        if not self._write_enabled or not self._capture:
            return
        user = _as_text(user_content)[:_MAX_USER_CHARS]
        if not _carries_user_words(user):
            return
        body = self._observe_body(user, _as_text(assistant_content)[:_MAX_ASSISTANT_CHARS])
        if body is None:
            return
        self._enqueue_capture(body)

    def on_session_end(self, messages) -> None:
        # Real session boundary only (never per-turn), so this is where the agentmemory session row
        # gets closed — that is what fires end-of-session consolidation/reflect.
        self._flush_best_effort()
        self._end_session()

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False,
                          rewound: bool = False, **kwargs) -> None:
        if not new_session_id or new_session_id == self._session_id:
            return
        self._flush_best_effort()
        if not rewound:
            self._end_session()
        self._session_id = new_session_id
        with self._lock:
            self._session_open = False
            self._pending.clear()

    # -- capture plumbing -----------------------------------------------------
    def _observe_body(self, user: str, assistant: str) -> Optional[Dict[str, Any]]:
        """One turn as a ``prompt_submit`` observation. api::observe rejects a body missing
        sessionId/project/cwd/timestamp, and mem::observe inherits agentId from the session row —
        so both come from here rather than being assumed."""
        if not self._session_id or not self._cwd:
            return None
        data: Dict[str, Any] = {"prompt": user}
        if assistant.strip():
            data["assistant"] = assistant.strip()
        return {
            "hookType": "prompt_submit",
            "sessionId": self._session_id,
            "project": self._project or self._agent_id or "default",
            "cwd": self._cwd,
            "timestamp": _utc_now(),
            "data": data,
        }

    def _session_start_body(self) -> Dict[str, Any]:
        body = {"sessionId": self._session_id, "project": self._project or self._agent_id or "default",
                "cwd": self._cwd or ""}
        if self._agent_id:
            body["agentId"] = self._agent_id
        return body

    def _ensure_session(self) -> bool:
        """Open the session row before the first observation of this session.

        Deliberately on the capture worker, not inline in initialize(): an observation posted before
        its session row exists inherits no agentId and then never resurfaces, so ordering matters
        more than latency here.
        """
        with self._lock:
            if self._session_open:
                return True
        if not self._session_id or not self._cwd:
            return False
        if self._post("/agentmemory/session/start", self._session_start_body()) is None:
            return False
        with self._lock:
            self._session_open = True
        return True

    def _end_session(self) -> None:
        # Leaves _session_open alone: a late queued turn must still land on THIS session id.
        # Re-running session/start would rewrite the row and zero its observationCount.
        with self._lock:
            opened, sid = self._session_open, self._session_id
        if opened and sid:
            self._post("/agentmemory/session/end", {"sessionId": sid})

    def _enqueue_capture(self, body: Dict[str, Any]) -> None:
        with self._lock:
            self._pending.append(body)
            if len(self._pending) > _MAX_PENDING:
                del self._pending[0: len(self._pending) - _MAX_PENDING]
        spawn_context_thread(self._drain_once, name="agentmemory-capture").start()

    def _send_head(self, timeout: float = _HTTP_TIMEOUT_S) -> bool:
        """Post the oldest queued body, popping it on success. The peek+post+pop runs under one
        lock: two drain threads reading the same head would otherwise write the turn twice (and
        the engine has no dedup for prompt_submit payloads)."""
        with self._drain_lock:
            if not self._ensure_session():
                return False
            with self._lock:
                body = self._pending[0] if self._pending else None
            if body is None:
                return False
            if self._post("/agentmemory/observe", body, timeout=timeout) is None:
                return False
            with self._lock:
                if self._pending and self._pending[0] is body:
                    self._pending.pop(0)
            return True

    def _drain_once(self) -> None:
        # One attempt per queued turn; failures stay buffered and retry on the next sync_turn
        # or an explicit flush.
        self._send_head()

    def _flush_best_effort(self) -> None:
        # Synchronous, bounded drain of the retry buffer (session boundary / shutdown / atexit).
        # Never touches the session row when nothing is buffered — that would re-open a session
        # this provider already closed.
        deadline = _monotonic() + _FLUSH_JOIN_S
        while _monotonic() < deadline:
            with self._lock:
                if not self._pending:
                    return
            if not self._send_head(timeout=2.0):
                return  # engine down: stop, keep the buffer; atexit retries on a later process

    # -- HTTP -----------------------------------------------------------------
    def _resolve_base_url(self) -> str:
        block = _config_block()
        raw = block.get("base_url") or get_secret("AGENTMEMORY_URL", "") or _DEFAULT_BASE_URL
        return str(raw).strip().rstrip("/") or _DEFAULT_BASE_URL

    def _post(self, path: str, body: Dict[str, Any], timeout: float = _HTTP_TIMEOUT_S) -> Any:
        url = f"{self._base_url}{path}"
        data = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._secret:
            headers["Authorization"] = f"Bearer {self._secret}"
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            logger.debug("agentmemory POST %s failed", path)
            return None
        except Exception:
            logger.debug("agentmemory POST %s unexpected error", path, exc_info=True)
            return None

    def _recall(self, query: str) -> str:
        body: Dict[str, Any] = {"query": query, "format": "full", "limit": _RECALL_LIMIT}
        if self._agent_id:
            body["agentId"] = self._agent_id
        if self._use_project and self._project:
            body["project"] = self._project
        payload = self._post("/agentmemory/search", body)
        if not isinstance(payload, dict):
            return ""
        return self._format(payload)

    # -- shaping / helpers ----------------------------------------------------
    @staticmethod
    def _format(payload: Dict[str, Any]) -> str:
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            return ""
        lines = []
        for r in results:
            if not isinstance(r, dict):
                continue
            # mem::search returns every row — distilled observation or promoted memory — wrapped as
            # {"observation": {...}, "score", "sessionId"}, so title/detail live inside the wrapper.
            obs = r.get("observation") if isinstance(r.get("observation"), dict) else r
            title = str(obs.get("title") or "").strip()
            detail = AgentMemoryProvider._detail(obs)
            if not title and not detail:
                continue
            lines.append(f"- {title}: {detail}" if title and detail else f"- {title or detail}")
        if not lines:
            return ""
        return ("## AgentMemory (relevant memories)\n" + "\n".join(lines))[:_MAX_INJECT_CHARS]

    @staticmethod
    def _detail(obs: Dict[str, Any]) -> str:
        facts = obs.get("facts")
        if isinstance(facts, list) and facts:
            kept = [str(f).strip().replace("\n", " ")[:_MAX_FACT_CHARS] for f in facts if str(f).strip()]
            if kept:
                return "; ".join(kept[:3])
        for k in ("narrative", "content", "summary"):
            v = obs.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip().replace("\n", " ")[:_MAX_FACT_CHARS * 2]
        return ""

    @staticmethod
    def _resolve_cwd() -> Optional[str]:
        try:
            from agent.runtime_cwd import resolve_agent_cwd
            c = resolve_agent_cwd()
            return str(c) if c else None
        except Exception:
            return None

    @staticmethod
    def _compute_project(profile: str, cwd: Optional[str]) -> Optional[str]:
        if not cwd:
            return profile
        try:
            import subprocess
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd,
                                 capture_output=True, text=True, timeout=2).stdout.strip()
            slug = top.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if top else ""
            if not slug:
                import os
                slug = os.path.basename(os.path.normpath(cwd))
            return f"{profile}/{slug[:64]}" if slug else profile
        except Exception:
            return profile

    def shutdown(self) -> None:
        self._flush_best_effort()
        with self._lock:
            self._cache.clear()


def _monotonic() -> float:
    import time
    return time.monotonic()


def _as_text(content: Any) -> str:
    """Message content as plain text (OpenAI string or content-part list)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")).strip() for p in content if isinstance(p, dict)).strip()
    return ""


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def register(ctx) -> None:
    """Memory-provider entrypoint discovered by plugins/memory/__init__.py."""
    ctx.register_memory_provider(AgentMemoryProvider())
