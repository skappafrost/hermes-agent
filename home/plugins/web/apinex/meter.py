"""Shared metering shim for web providers that serve as fallbacks.

The pool dashboard (``apinex-meter``) reads one SQLite DB. Every web flow —
whichever vendor ultimately serves it — should land there, so the board can
answer "which agent searched what, how many KB came back, through which
route". This module gives any provider a one-call way to do that without
importing the apinex plugin directly: it lazy-loads the tracker (stdlib +
its own DB path only) and degrades to a no-op when unavailable, so metering
can never break a live tool call.

``route`` values are the vendor names themselves (``apinex``, ``searchx``,
``unsearch``, ``exa``, ``keyless``), so a single column covers the whole
chain and the dashboard needs no special-casing per provider.
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

logger = logging.getLogger(__name__)


def tracker() -> Optional[Any]:
    """The pool tracker module, or ``None`` when metering is unavailable."""
    try:
        from . import tracker as _meter

        return _meter
    except Exception:  # noqa: BLE001 — the meter is optional by design
        return None


def key_fingerprint(key: str) -> str:
    """Short stable label for a vendor key (never the secret itself)."""
    return (key or "")[-8:] or "?"


@contextmanager
def call_scope() -> Iterator[Optional[int]]:
    """Group every attempt of one logical call; no-op when the meter is off."""
    _meter = tracker()
    if _meter is None:
        yield None
        return
    with _meter.call_scope() as cid:
        yield cid


def log(
    *,
    route: str,
    endpoint: str,
    ok: bool,
    latency_ms: float,
    status: Optional[int] = None,
    key_fp: str = "",
    req_summary: Optional[str] = None,
    resp_bytes: Optional[int] = None,
    result_count: Optional[int] = None,
    limit_remaining: Optional[int] = None,
) -> None:
    """Append one dashboard row. Silent no-op when the meter is unavailable."""
    _meter = tracker()
    if _meter is None:
        return
    try:
        _meter.log_request(
            key_no=0, key_fp=key_fp or route, endpoint=endpoint, ok=ok,
            status=status, latency_ms=latency_ms, limit_remaining=limit_remaining,
            req_summary=req_summary, resp_bytes=resp_bytes,
            result_count=result_count, route=route,
        )
    except Exception as exc:  # noqa: BLE001 — metering must never break a call
        logger.debug("web meter (%s) unavailable: %s", route, exc)


def size_of(payload: Any) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return 0


def summarize_search(query: str, limit: int) -> str:
    return f"q={str(query or '')[:200]} n={limit}"


def summarize_urls(urls: List[str]) -> str:
    first = str(urls[0])[:150] if urls else "-"
    return f"{len(urls)} url(s): {first}"


def remaining_from_headers(headers: Any) -> Optional[int]:
    try:
        items = dict(headers or {})
    except Exception:  # noqa: BLE001
        return None
    for key in ("x-ratelimit-remaining", "X-RateLimit-Remaining"):
        raw = items.get(key)
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
    return None


_SELF_METERED = {"apinex", "searchx", "unsearch"}


def self_metered() -> set:
    """Vendor names whose plugin logs its own meter rows (skip tool-layer dupes).

    Kept as an explicit list — adding a self-metered tier means listing it
    here once. Reflection over the registry was brittle (class objects and
    name strings coexist) and the failure mode is a harmless missing row.
    """
    return set(_SELF_METERED)


def profile_name() -> str:
    """Current Hermes profile for meter attribution (same rules as keypool)."""
    try:
        from . import keypool as _pool

        return _pool.profile_name()
    except Exception:  # noqa: BLE001
        return "?"
