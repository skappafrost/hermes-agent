"""Vilao billing client: the real-dong side of the Usage page.

Only GETs. The account's Personal Access Token is full-scope (vilao issues one token per account
with no scoping, including ``wallet:write``), so this module never posts anything and reads the
secret through the same path provider-usage uses.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

BASE = "https://vilao.ai/api/v2"
# The console host, spelled out because the token this module attaches is full-scope. Anything absolute
# that is not https on one of these hosts is refused before a header is built.
ALLOWED_HOSTS = frozenset({"vilao.ai", "www.vilao.ai"})
PAGE_SIZE = 100
MAX_PAGES = 200  # ~20k rows; the account had 7048 at integration time
# Deep offsets on the usage endpoint answer in tens of seconds; a year walk must not die on
# one slow page (it did, at ~90s in, on 2026-10-08).
PAGE_TIMEOUT = httpx.Timeout(90.0, connect=10.0)


def pat() -> Optional[str]:
    """The console PAT, scoped to the profile that is asking."""
    try:
        from agent.secret_scope import get_secret

        value = get_secret("VILAO_PAT")
        if value:
            return str(value)
    except Exception:
        pass
    try:
        from hermes_constants import get_hermes_home

        for line in (get_hermes_home() / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("VILAO_PAT="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _ts(created_at: Optional[str]) -> float:
    """ISO-8601 (Z or offset) to epoch seconds; unparseable stamps land on 'now' rather than 1970."""
    if not created_at:
        return time.time()
    text = str(created_at).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return time.time()


def to_row(item: dict[str, Any]) -> dict[str, Any]:
    """One ``/llm/usage`` entry into the store's bill shape.

    ``consumer_key_id`` is carried as ``bill_key_id``: the account has six keys and the usage feed is
    the only place that says which one a call ran on, so dropping it would make per-key filtering
    impossible after the page is closed.
    """
    tokens = int(item.get("total_tokens") or 0)
    return {
        "ts": _ts(item.get("created_at")),
        "model": item.get("actual_model") or item.get("model_id"),
        "provider": "vilao-ai",
        "session_id": None,
        "platform": None,
        "task": None,
        "api_mode": item.get("provider_type"),
        "finish_reason": "error" if item.get("success") is False else "stop",
        "duration_ms": int(item.get("latency_ms") or 0) or None,
        "ttft_ms": int(item.get("time_to_first_token_ms") or 0) or None,
        "bill_cost_vnd": float(item.get("total_cost") or 0.0),
        "bill_key_id": str(item.get("consumer_key_id") or "") or None,
        "bill_request_id": str(item.get("id") or item.get("request_id") or ""),
        "tok_total": tokens,
        "usage": {
            "input_tokens": int(item.get("input_tokens") or 0),
            "output_tokens": int(item.get("output_tokens") or 0),
            "cache_read_tokens": int(item.get("cache_read_input_tokens") or 0),
            "reasoning_tokens": int(item.get("reasoning_tokens") or 0),
        },
    }


def _key_row(k: dict[str, Any]) -> dict[str, Any]:
    """One ``/llm/keys`` entry. ``id`` is what ``/llm/usage`` calls ``consumer_key_id``."""
    return {
        "id": str(k.get("id") or ""),
        "name": k.get("name"), "key_prefix": k.get("key_prefix"),
        "rate_limit_rpm": k.get("rate_limit_rpm"),
        "total_requests": k.get("total_requests"),
        "total_spent": k.get("total_spent"),
        "subscriptions": len(k.get("subscriptions") or []),
        "active": bool(k.get("active")), "favorite": bool(k.get("favorite")),
        "created_at": k.get("created_at"), "revoked_at": k.get("revoked_at"),
    }


def _get(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    """One authenticated GET, returning the envelope's ``data``.

    ``path`` may be absolute: the model catalogue and the published prices live on the v1 marketplace host
    rather than under ``/api/v2``, and this is the only place that knows how to authenticate a call.

    The token is full-scope, so the destination is validated rather than trusted: an absolute URL only
    counts if its host is the console's own and the scheme is https. Without that check, one caller
    passing a string it built from a request parameter would hand a spend-capable credential to a third
    party, and the plugin would look exactly the same in the logs.
    """
    url = str(path) if str(path).startswith("http") else BASE + str(path)
    host = urlsplit(url).netloc.lower()
    if not url.startswith("https://") or host.split(":", 1)[0] not in ALLOWED_HOSTS:
        raise ValueError("refusing to send the PAT to a host other than the console: %r" % host)
    token = pat()
    if not token:
        raise PermissionError("VILAO_PAT is not configured for this profile")
    resp = httpx.get(url, params=params or {}, timeout=20.0,
                     headers={"Authorization": "Bearer " + token})
    if resp.status_code in (401, 403):
        raise PermissionError("vilao rejected the PAT (HTTP %d)" % resp.status_code)
    resp.raise_for_status()
    return (resp.json() or {}).get("data")


_keys_cache: dict[str, Any] = {"at": 0.0, "rows": None}
KEYS_TTL_S = 300.0


def list_keys(refresh: bool = False) -> list[dict[str, Any]]:
    """The account's API keys, cached: they change when a key is created, not every minute, and the
    page asks for them on each filter render to label the per-key rows."""
    age = time.time() - float(_keys_cache["at"] or 0)
    if not refresh and _keys_cache["rows"] is not None and age < KEYS_TTL_S:
        return list(_keys_cache["rows"])
    data = _get("/llm/keys") or []
    rows = [_key_row(k) for k in data if isinstance(k, dict)]
    _keys_cache.update(at=time.time(), rows=rows)
    return rows


def pull_recent(store, pages: int = 2, hours: float = 48.0) -> dict[str, Any]:
    """Ingest the newest ``pages`` bill pages so a Vilao-mode view is current, then answer from the store.

    The usage endpoint ignores every filter we tried (same 41k ``total_count`` and the same rows for
    ``consumer_key_id``, ``days``, ``success``, ``model_id``), so filtering has to happen here, over a
    window this pull can actually reach. Newest-first means two pages cover the last few minutes of a
    busy account; the walk is idempotent per bill id, so a re-pull costs the reads and nothing else.
    """
    read = stored = 0
    for _page, rows in iter_pages(max(1, int(hours / 24)), start_page=1,
                                  budget=max(1, min(int(pages), 6))):
        read += len(rows)
        stored += store.record_bill_rows(rows)
    matched = store.match_rows() if read else 0
    return {"read": read, "stored": stored, "matched": matched, "pages": pages}


# A pulse reads the newest two pages -- about a hundred bill rows, which is more than this account
# produces in the cooldown window -- so an idle hour costs no request at all.
AUTO_PULSE_COOLDOWN_S = 90.0
# vilao writes the bill row a moment after the response finishes. Reading immediately is the one way
# this job could still leave the call that triggered it unpriced.
AUTO_PULSE_SETTLE_S = 12.0
AUTO_PULSE_PAGES = 2
AUTO_PULSE_HOURS = 48.0
PULSE_CLAIM_KEY = "bill_pulse_at"

_pulse_gate: dict[str, Any] = {"next": 0.0}
_pulse_lock = threading.Lock()


def auto_pulse(store) -> bool:
    """Price the calls that just happened, whether or not anyone has the Usage page open.

    The call hook is the trigger because it is the only thing guaranteed to run where calls happen: this
    install has five gateways, the desktop app can be closed for hours, and every dong figure in the
    plugin is made from bill rows -- so a row parked at ``local_only`` until somebody opens a tab is the
    page quietly failing to answer its own headline question.

    Two gates keep that off the turn's critical path, and neither does IO: an in-process timestamp that
    holds the window, and a non-blocking lock so a slow pull cannot stack threads. Everything that
    touches the store -- the cross-process claim and the pull -- happens on the daemon thread, after the
    sleep that lets vilao finish writing the row it is about to read.
    """
    now = time.time()
    if now < float(_pulse_gate["next"] or 0) or not _pulse_lock.acquire(blocking=False):
        return False
    _pulse_gate["next"] = now + AUTO_PULSE_COOLDOWN_S

    def scheduled():
        try:
            time.sleep(AUTO_PULSE_SETTLE_S)
            # The claim is store-wide: five gateways and the dashboard host share one window rather than
            # each pulling the same bill and queueing five writers onto the same rows.
            if not store.claim_slot(PULSE_CLAIM_KEY, AUTO_PULSE_COOLDOWN_S):
                return False
            logger.debug("usage page: background bill pulse %s",
                         pull_recent(store, pages=AUTO_PULSE_PAGES, hours=AUTO_PULSE_HOURS))
            return True
        except Exception as exc:
            # Fail-open, and the claim already moved the stamp forward: a stalled network costs one
            # cooldown window rather than a retry storm behind every following call.
            logger.warning("usage page: background bill pulse failed: %s: %s",
                           type(exc).__name__, str(exc)[:160])
            return False
        finally:
            _pulse_lock.release()

    try:
        threading.Thread(target=scheduled, daemon=True, name="resetwatch-bill-pulse").start()
    except Exception as exc:
        _pulse_lock.release()
        logger.warning("usage page: could not start a bill pulse: %s", exc)
        return False
    return True


def account_snapshot() -> dict[str, Any]:
    """Everything the Wallet tab needs, in one round trip: wallet, pending top-ups, per-key totals.

    Read-only by construction -- three GETs, and the account's token is full-scope so no write
    endpoint is ever named here.
    """
    token = pat()
    if not token:
        raise PermissionError("VILAO_PAT is not configured for this profile")
    out: dict[str, Any] = {}
    with httpx.Client(timeout=20.0, headers={"Authorization": "Bearer " + token}) as client:
        for name, path, params in (
            ("wallet", "/account/balance", None),
            ("pending_topups", "/wallet/transactions", {"status": "pending", "page_size": 5}),
            ("keys", "/llm/keys", None),
        ):
            resp = client.get(BASE + path, params=params)
            if resp.status_code in (401, 403):
                raise PermissionError("vilao rejected the PAT (HTTP %d)" % resp.status_code)
            resp.raise_for_status()
            body = resp.json() or {}
            data = body.get("data")
            if name == "keys" and isinstance(data, list):
                out["keys"] = [_key_row(k) for k in data if isinstance(k, dict)]
            elif name == "pending_topups" and isinstance(data, list):
                out["pending_topups"] = [{
                    "transaction_id": t.get("id"), "amount_expected": t.get("amount_expected"),
                    "currency": t.get("currency"), "created_at": t.get("created_at"),
                    "expires_at": t.get("expires_at"),
                } for t in data if isinstance(t, dict)]
            else:
                out["wallet"] = data or {}
    return out


def iter_pages(days: int, start_page: int = 1, budget: int = 8):
    """Yield up to ``budget`` bill pages, starting at ``start_page``, stopping at the horizon.

    ``start_page`` makes a bounded pull resumable: the route asks for a few pages per request, so no
    single HTTP call can be held open by a long backfill. Two guards on the walk, because ``days`` is
    not trustworthy on this endpoint (a 7-day pull kept returning rows from months back): the horizon
    is enforced from row timestamps, and the budget bounds the work per request.

    Deep offsets answer slowly and a read occasionally exceeds the timeout, so a page is retried once
    before the walk gives up: measured, a 415-page year walk died at page ~95 on a single stall, which
    threw away minutes of already-fetched history.
    """
    token = pat()
    if not token:
        raise PermissionError("VILAO_PAT is not configured for this profile")
    cutoff = time.time() - max(1, int(days)) * 86400
    with httpx.Client(timeout=PAGE_TIMEOUT, headers={"Authorization": "Bearer " + token}) as client:
        for page in range(max(1, int(start_page)), max(1, int(start_page)) + max(1, int(budget))):
            items = _page(client, page, days)
            if not items:
                return
            rows = [to_row(item) for item in items if isinstance(item, dict)]
            fresh = [row for row in rows if float(row["ts"]) >= cutoff]
            if fresh:
                yield page, fresh
            if len(rows) < PAGE_SIZE or len(fresh) < len(rows):
                return


def _page(client, page: int, days: int) -> list:
    """One usage page, retried once on a transport failure."""
    last: Optional[Exception] = None
    for attempt in range(2):
        try:
            resp = client.get(f"{BASE}/llm/usage",
                              params={"page": page, "page_size": PAGE_SIZE, "days": days})
            if resp.status_code in (401, 403):
                raise PermissionError("vilao rejected the PAT (HTTP %d)" % resp.status_code)
            resp.raise_for_status()
            return (resp.json() or {}).get("data") or []
        except (httpx.TimeoutException, httpx.NetworkError, ValueError) as exc:
            # ValueError: a stalled response often arrives truncated, so the JSON parse fails too.
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise last


def ingest(store, days: int = 30, *, full: bool = False, start_page: int = 1,
           budget: int = 8, fresh_stop: bool = False) -> dict[str, Any]:
    """Pull up to ``budget`` bill pages into ``store`` and pair what arrived.

    The store is passed in rather than imported: this module is loaded both as part of the plugin
    package (agent side) and standalone by path (the dashboard plugin host), and only the first has a
    parent package to import from.

    Returns the next cursor and whether the walk reached the horizon, so the caller can drive a long
    backfill a few pages per request. Idempotent per row (``bill_request_id`` is unique), so an
    interrupted pull simply resumes and a re-run costs only the new pages.

    ``fresh_stop`` ends the walk at the first page holding nothing new. Rows arrive newest-first, so
    that page is the frontier, and a poll that kept walking would re-read the whole horizon every
    five minutes.
    """
    horizon = 3650 if full else max(1, int(days))
    read = stored = matched = 0
    pages_seen = 0
    last_page = start_page
    done = False
    for page, rows in iter_pages(horizon, start_page=start_page, budget=budget):
        pages_seen += 1
        last_page = page
        read += len(rows)
        new = store.record_bill_rows(rows)
        stored += new
        if fresh_stop and new == 0:
            done = True
            break
    # Pair once per request, not per page: pairing re-reads every unclaimed row, so doing it inside
    # the page loop costs pages x rows and stalls a full backfill.
    matched = store.match_rows()
    done = done or pages_seen < budget
    if done:
        last_page = 1
    return {"read": read, "stored": stored, "matched": matched, "done": done,
            "next_page": last_page, "pages": pages_seen}
