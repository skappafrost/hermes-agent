"""Report tool-layer ``web_search`` / ``web_extract`` calls into the APInex pool meter.

Replaces the metering that used to sit inside ``tools/web_tools.py`` and
``tools/web_tools_extract.py``. It runs from the ``post_tool_call`` hook, which core
fires on every path — success, plugin block, exception — with a core-measured
``duration_ms`` and the real correlation ids.

The write is handed to a daemon thread rather than done inline: ``post_tool_call`` is a
timeout-bounded hook, and ``tracker._connect`` will block up to 30s on a contended
SQLite file. A blocked write would abandon the hook worker and then suppress this
callback for 60s, silently dropping a minute of rows.
"""
from __future__ import annotations

import json
import queue
import threading
from typing import Any, Dict, Optional

from . import meter as _meter

_ENDPOINTS = {"web_search": "search", "web_extract": "contents", "web_research": "research"}
_ROWS: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=2000)
_worker_lock = threading.Lock()
_worker: Optional[threading.Thread] = None


def _configured_route(tool_name: str) -> str:
    """The backend the tool resolved to, using the tool's own ladder.

    Deliberately not ``agent.web_search_registry.get_active_search_provider()`` — that is
    a different ladder. With two vendor keys set and no ``web.*`` config it answers
    ``firecrawl`` while the tool actually serves ``tavily``, which would mislabel rows.
    These are private core helpers; a rename makes this return ``"?"``, not a bad row.
    """
    try:
        from tools import web_tools
    except Exception:
        return "?"
    try:
        if tool_name == "web_extract":
            return str(web_tools._get_extract_backend() or "?")
        return str(web_tools._get_search_backend() or "?")
    except Exception:
        return "?"


def _payload(result: Any) -> Dict[str, Any]:
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return {}
    try:
        parsed = json.loads(result)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _rescued(body: Dict[str, Any]) -> bool:
    """True when the keyless free-tier ring answered instead of the configured vendor."""
    data = body.get("data")
    if isinstance(data, dict) and data.get("rescued_from"):
        return True
    rows = body.get("results")
    if isinstance(rows, list):
        for row in rows:
            meta = row.get("metadata") if isinstance(row, dict) else None
            if isinstance(meta, dict) and meta.get("rescued_from"):
                return True
    return False


def _summarize(tool_name: str, args: Dict[str, Any]) -> str:
    if tool_name == "web_search":
        return _meter.summarize_search(str(args.get("query") or ""), int(args.get("limit") or 5))
    urls = args.get("urls") or args.get("url") or []
    if isinstance(urls, str):
        urls = [urls]
    return _meter.summarize_urls([str(u) for u in urls])


def build_row(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tool_name = str(payload.get("tool_name") or "")
    endpoint = _ENDPOINTS.get(tool_name)
    if endpoint is None:
        return None
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    body = _payload(payload.get("result"))
    route = "keyless" if _rescued(body) else _configured_route(tool_name)
    # Self-metered vendors already wrote a row for this attempt from inside their own
    # provider, so logging here would double-count the call on the dashboard.
    if route != "keyless" and route in _meter.self_metered():
        return None
    success = bool(body.get("success")) if "success" in body else payload.get("status") == "ok"
    if tool_name == "web_extract":
        rows = body.get("results") or []
        count = sum(1 for r in rows if isinstance(r, dict) and not r.get("error")) or None
        size = _meter.size_of(rows) if rows else None
    else:
        hits = ((body.get("data") or {}).get("web") or []) if isinstance(body.get("data"), dict) else []
        count = len(hits) or None
        size = _meter.size_of(hits) if hits else None
    return {
        "route": route,
        "endpoint": endpoint,
        "ok": success,
        "latency_ms": float(payload.get("duration_ms") or 0),
        "status": None if success else -1,
        "key_fp": tool_name,
        "req_summary": _summarize(tool_name, args),
        "resp_bytes": size,
        "result_count": count,
    }


def _drain() -> None:
    while True:
        row = _ROWS.get()
        try:
            _meter.log(**row)
        except Exception:
            pass
        finally:
            _ROWS.task_done()


def post_tool_call(**payload: Any) -> None:
    """Hook callback. ``**kwargs`` opts into the full observer payload."""
    global _worker
    try:
        row = build_row(payload)
    except Exception:
        return
    if row is None:
        return
    if _worker is None:
        with _worker_lock:
            if _worker is None:
                _worker = threading.Thread(target=_drain, name="apinex-pool-meter", daemon=True)
                _worker.start()
    try:
        _ROWS.put_nowait(row)
    except queue.Full:
        pass
