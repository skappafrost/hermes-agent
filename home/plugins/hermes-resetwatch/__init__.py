"""Usage page: agent-side entry point.

Unified Hermes plugin, three halves:

- ``desktop/plugin.js``  : the Desktop page (route ``/resetwatch``), real-time call log and charts.
- ``dashboard/plugin_api.py`` : backend router mounted at ``/api/plugins/resetwatch/``.
- ``usage_store.py`` / ``usage_bill.py`` : the plugin-private per-call SQLite and the vilao bill feed.

The agent side registers observer hooks: every model call Hermes makes is written to the store and
broadcast to connected desktop clients, so the log is event-driven rather than polled. Hooks are
fail-open by contract -- an observer must never break a turn.
"""
from __future__ import annotations

import logging
import sys
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Must equal plugin.yaml's name: the desktop plugin addresses /api/plugins/<id>/ through ctx.rest and
# the dashboard refuses to mount a dashboard manifest whose name is not a known agent plugin.
PLUGIN_ID = "hermes-resetwatch"

# The bill client and the store it writes through, bound by register(). Resolved at plugin load rather
# than inside a hook because usage_bill imports httpx, and the first model call of a turn is the wrong
# place to pay for an import -- the same reason _broadcast only touches an already-loaded module.
_bill = None


def _estimate_usd(payload: dict[str, Any]) -> Optional[float]:
    """Price the call with Hermes' own price table, mirroring ``agent/turn_usage``.

    Uses only an already-imported ``agent.usage_pricing``. A hook runs on the turn's critical path,
    and importing the agent runtime from inside one is a stall risk rather than an error (it is what
    wedged a standalone probe of this exact path), so the cost is simply omitted when the module is
    not loaded yet. No api key is passed either, keeping pricing offline.
    """
    usage = payload.get("usage")
    if not usage:
        return None
    pricing = sys.modules.get("agent.usage_pricing")
    if pricing is None:
        logger.debug("usage page: pricing module not loaded; row stored without an estimate")
        return None
    try:
        canonical = pricing.normalize_usage(usage, provider=payload.get("provider"),
                                            api_mode=payload.get("api_mode"))
        result = pricing.estimate_usage_cost(payload.get("model") or "", canonical,
                                             provider=payload.get("provider"),
                                             base_url=payload.get("base_url"))
        if result.amount_usd is None:
            return None
        return float(result.amount_usd)
    except Exception as exc:
        logger.debug("usage page: cost estimate failed for %s: %s", payload.get("model"), exc)
        return None


def _timing(payload: dict[str, Any]) -> dict[str, Any]:
    duration = payload.get("api_duration")
    started, ended = payload.get("started_at"), payload.get("ended_at")
    out: dict[str, Any] = {
        "duration_ms": round(float(duration) * 1000) if duration else None,
        "ttft_ms": None,
    }
    if started and ended:
        out["ttft_ms"] = round((float(ended) - float(started)) * 1000)
    if started and payload.get("first_chunk_at"):
        out["ttft_ms"] = round((float(payload["first_chunk_at"]) - float(started)) * 1000)
    return out


def _broadcast(row: dict[str, Any]) -> None:
    """Push the new row to connected desktop clients, without importing anything from a hook.

    Only an already-loaded ``hermes_cli.plugin_events`` is used: the dashboard/serve host imports it
    during startup, so in production the module is present. Importing it here instead would pull
    server machinery onto the turn's critical path, where a stall blocks the model call rather than
    just losing one live row -- the page still refetches on its 5-minute cadence.
    """
    events = sys.modules.get("hermes_cli.plugin_events")
    if events is None:
        logger.debug("usage page: plugin event host not loaded; the page will refresh on its timer")
        return
    try:
        events.broadcast_plugin_event(PLUGIN_ID, "call", row)
    except Exception as exc:
        logger.debug("usage page: call event broadcast failed: %s", exc)


def _record(payload: dict[str, Any], *, task: Optional[str] = None) -> None:
    """Write one call, tell every connected desktop client, and keep the bill feed moving."""
    from . import usage_store

    payload = dict(payload or {})
    if task:
        payload["task"] = task
    payload.update(_timing(payload))
    row = usage_store.record_local(
        ts=float(payload.get("ended_at") or time.time()),
        payload=payload,
        est_cost_usd=_estimate_usd(payload),
    )
    if row is None:
        return
    _broadcast(row)
    _pulse_bill(usage_store)


def _pulse_bill(usage_store) -> None:
    """Ask the bill feed for the price of the row that just landed.

    Throttled, store-wide claimed and off-thread inside :func:`usage_bill.auto_pulse`; wrapped here too
    because an observer must still never break a turn. The worst case is one call that stays unpriced
    until the next pulse -- which is what the page looked like all week whenever nobody opened it.
    """
    if _bill is None:
        return
    try:
        _bill.auto_pulse(usage_store)
    except Exception as exc:
        logger.debug("usage page: bill pulse skipped: %s", exc)


# Hook callbacks receive keyword arguments, not an event dict -- the dispatcher warns
# "callback raised: missing 1 required positional argument" if they are declared otherwise.
def on_post_api_request(**event: Any) -> None:
    try:
        _record(event)
    except Exception as exc:
        logger.warning("usage page: failed to record an API call: %s", exc, exc_info=True)


def on_post_auxiliary_call(**event: Any) -> None:
    """Side-LLM work (compression, titles, embeddings) is labelled so the UI can separate it."""
    try:
        _record(event, task=event.get("aux_task") or "auxiliary")
    except Exception as exc:
        logger.warning("usage page: failed to record an auxiliary call: %s", exc, exc_info=True)


def on_api_request_error(**event: Any) -> None:
    """A failed call is a row with no tokens, not a hole in the log."""
    try:
        payload = dict(event or {})
        payload.setdefault("finish_reason", "error")
        _record(payload)
    except Exception as exc:
        logger.warning("usage page: failed to record an API error: %s", exc, exc_info=True)


def register(ctx) -> None:
    global _bill
    try:
        from . import usage_bill

        _bill = usage_bill
    except Exception as exc:
        # A Hermes without httpx on this path cannot reach the billing endpoint at all, so the rows
        # simply stay as they were: recorded locally, priced whenever a reader asks for a pull.
        logger.warning("usage page: bill client unavailable, no background price refresh: %s", exc)
    for name, fn in (
        ("post_api_request", on_post_api_request),
        ("post_auxiliary_call", on_post_auxiliary_call),
        ("api_request_error", on_api_request_error),
    ):
        try:
            ctx.register_hook(name, fn)
        except Exception as exc:
            logger.warning("usage page: could not register %s on this Hermes: %s", name, exc)
