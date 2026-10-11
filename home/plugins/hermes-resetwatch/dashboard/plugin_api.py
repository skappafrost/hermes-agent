"""Usage page: backend router.

FastAPI router mounted at ``/api/plugins/resetwatch/`` by the Hermes dashboard server. Reads the
plugin-private per-call SQLite and serves the aggregates the desktop page draws.

The store and bill modules are loaded by path, not imported: with ``plugins.isolation: host`` the
dashboard mounts this file inside the plugin host process, where there is no parent package to
resolve a relative import against.

Credential handling is read-only. Bill sync issues GETs only and never touches a write scope.
"""
from __future__ import annotations

import importlib.util
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException

router = APIRouter()

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SYNC_COOLDOWN_S = 60.0
# vilao's /llm/usage costs a few seconds per page and gets slower deeper in, so a request may only
# pull a handful of pages. The page drives the loop and shows progress; the cursor lives in the store
# so a dashboard restart resumes instead of re-reading from page 1.
DEFAULT_BUDGET = 3
MAX_BUDGET = 6
CURSOR_KEY = "bill_cursor"
# A key id as vilao issues them (uuid-shaped). Anything else is refused before it is pasted into an
# endpoint path that this module authenticates with a spend-capable token.
KEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# A day filter comes from a click on the year grid, so the only honest shape is the stored one; anything
# else would silently match zero rows and read as "that day had no calls".
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A Vilao-mode view refresh pulls two bill pages, which costs a few seconds of the billing endpoint's
# time. Filters change on every keystroke, so the refresh itself is rate-limited rather than free.
PULSE_COOLDOWN_S = 45.0

# Where a paged backfill stopped. Inline pulls (no worker thread) keep the request short and let the
# page drive the walk, so the cursor is the only cross-request state this router needs.
_state: dict[str, Any] = {"at": None, "result": None, "error": None, "page": 1}
_pulse: dict[str, Any] = {"at": 0.0}


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, PLUGIN_ROOT / filename)
    if spec is None or spec.loader is None:
        raise HTTPException(status_code=500, detail="cannot load %s" % filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = _load("resetwatch_usage_store", "usage_store.py")
bill = _load("resetwatch_usage_bill", "usage_bill.py")
fx = _load("resetwatch_fx", "fx.py")
rd = _load("resetwatch_vilao_read", "vilao_read.py")



def _days(raw: Optional[str], default: int) -> int:
    try:
        return max(1, min(int(raw), 365))
    except (TypeError, ValueError):
        return default


def _status() -> dict[str, Any]:
    page = int(store.get_meta("bill_page", "1") or 1)
    return {"at": _state["at"], "result": _state["result"], "error": _state["error"],
            "page": page, "pending": page > 1}


@router.get("/health")
def get_health() -> dict[str, Any]:
    return {"ok": True, "pat_configured": bool(bill.pat()), "sync": _status(), "rows": store.stats()}


@router.get("/calls")
def get_calls(page: int = 1, per: int = 100, provider: Optional[str] = None,
              model: Optional[str] = None, since: Optional[float] = None,
              main_only: bool = False, key_id: Optional[str] = None,
              billed: bool = False, outcome: Optional[str] = None, min_dong: Optional[float] = None,
              hours: Optional[float] = None, profile: Optional[str] = None,
              source: Optional[str] = None, pulse: bool = False,
              day: Optional[str] = None,
              pages: int = 2) -> dict[str, Any]:
    """The call log, one page at a time. ``billed=1`` is the Vilao view, ``pulse=1`` refreshes it first.

    A pulse is what makes the Vilao mode live rather than a mirror of the last sync: it ingests the
    newest pages before answering. The cooldown keeps a fast-fingered filter change from paging the
    billing endpoint once per keystroke; reading the store itself is free.

    Paging is server-side and the total comes back with the rows: the store holds ~42k calls, so a
    client that only ever asks for the first N can never reach the rest, and a pager that has to guess
    whether another page exists would guess wrong on an exact multiple.
    """
    pulled = None
    if pulse:
        now = time.time()
        if now - float(_pulse["at"] or 0) >= PULSE_COOLDOWN_S:
            _pulse["at"] = now
            try:
                pulled = bill.pull_recent(store, pages=pages)
            except PermissionError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except Exception as exc:
                raise HTTPException(status_code=502, detail="vilao bill unreachable: %s" % str(exc)[:120]) from exc
        else:
            pulled = {"skipped": "cooldown"}
    window = since if since else (time.time() - float(hours) * 3600 if hours else None)
    if day:
        if not DAY_RE.match(day):
            raise HTTPException(status_code=400, detail="day must look like 2026-10-09")
        try:
            time.strptime(day, "%Y-%m-%d")
        except ValueError as exc:
            # 2026-02-30 has the right shape and no such day; answering "zero calls" would be a lie
            # about a date that never happened.
            raise HTTPException(status_code=400, detail="no such day: %s" % day) from exc
    per = max(1, min(int(per), 500))
    number = max(1, int(page))
    who = profile or None
    rows = store.recent(limit=per, offset=(number - 1) * per, provider=provider, model=model,
                        since=window, main_only=bool(main_only), key_id=key_id or None,
                        profile=who, billed_only=bool(billed), outcome=outcome or None,
                        min_dong=min_dong, source=source or None, day=day or None)
    total = store.recent_count(provider=provider, model=model, since=window,
                               main_only=bool(main_only), key_id=key_id or None, profile=who,
                               billed_only=bool(billed), outcome=outcome or None,
                               min_dong=min_dong, source=source or None, day=day or None)
    return {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "count": len(rows), "total": total,
            "page": number, "per": per, "pages": max(1, -(-total // per)), "calls": rows,
            "pulled": pulled}


@router.get("/fx")
def get_fx() -> dict[str, Any]:
    """USD and yuan spot rates, for turning Hermes' dollar estimates into the dong the account spends.

    Separate from ``/calls`` because the page wants it once per session while the log refills every
    few minutes, and the shared cache file means provider-usage's status bar and this page cannot
    disagree about the rate they print.
    """
    return {"fx": fx.rates()}


@router.get("/rate")
def get_rate(hours: float = 24.0, profile: Optional[str] = None) -> dict[str, Any]:
    """Self-measured request rate per provider.

    The free tiers this account leans on publish a request limit and return no usage API, so the only
    number that exists is our own count. The page must label it as measured, not as the provider's word.

    ``by_provider`` walks the same window per provider (one query each, plus the rolling-hour loop) and
    ``overall`` re-buckets for the requested profile; both read the call table through its ts index, which
    measured 2 ms for a day of this account's calls.
    """
    span = max(0.25, min(float(hours), 720.0))
    return {"hours": span,
            "by_provider": store.rate_by_provider(span),
            "overall": store.rate_window(span, profile or None)}


@router.get("/catalog")
def get_catalog(refresh: bool = False, q: Optional[str] = None) -> dict[str, Any]:
    """The model catalogue with the account's own spend next to each price, when ``ids`` is given.

    The join against the store is unconditional: a price list on its own answers "what does it cost",
    and the question in front of the user is "what do *I* pay for the models I actually reach for".

    The join against the model rollup is what used to make this 79 ms; with the day x model table it is a
    few hundred rows, so no cache is needed and ``refresh=1`` still means "re-read the catalogue now".
    """
    def build() -> dict[str, Any]:
        data = rd.catalog(bill._get, force=bool(refresh))
        rows = data.get("models") or []
        used = {str(r.get("model_key")): r for r in store.usage_by_model()}
        for m in rows:
            hit = used.get(str(m.get("id")))
            if hit:
                m["mine"] = {"calls": int(hit.get("calls") or 0), "tokens": int(hit.get("tokens") or 0),
                             "dong": round(float(hit.get("dong") or 0), 4),
                             # The denominator the bill actually charges: Hermes reports input+output,
                             # vilao bills input+output+cache read, so a "per 1M tokens" price built on the
                             # first shape would look inflated by exactly the cached share.
                             "tok_billed": int(hit.get("tok_billed") or 0),
                             "list_in": hit.get("list_in"), "list_out": hit.get("list_out")}
        # Price watch: a snapshot is only possible while the prices are in hand, so the read path writes
        # it. A failure here must not cost the user the table they asked for, so the watch degrades to
        # "no changes known" and says so rather than turning the tab into an error card.
        watch: dict[str, Any] = {"days": [], "count": 0}
        if rows:
            try:
                store.record_catalog(rows)
                changes = store.catalog_changes()
                by_key: dict[str, list[dict[str, Any]]] = {}
                for c in changes["rows"]:
                    by_key.setdefault(str(c["key"]), []).append(c)
                for m in rows:
                    moved = by_key.get(str(m.get("key")))
                    if moved:
                        m["chg"] = moved
                watch = {"days": changes["days"], "count": changes["count"]}
            except Exception as exc:
                watch = {"days": [], "count": 0, "error": str(exc)[:120]}
        return {"count": len(rows), "total": data.get("count"), "models": rows, "watch": watch,
                "cached_at": data.get("at"), "source": data.get("source"), "error": data.get("error")}

    try:
        payload = build()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="catalog unreachable: %s" % str(exc)[:120]) from exc
    term = (q or "").strip().lower()
    rows = payload["models"] if not term else [
        m for m in payload["models"]
        if term in str(m.get("id") or "").lower()
        or term in str(m.get("name") or "").lower() or term in str(m.get("provider") or "").lower()]
    # The search filter is applied to the built payload, not cached per term: a cache keyed on a text
    # field the user types into grows without bound and buys nothing this side of a second.
    return {**payload, "count": len(rows), "models": rows}


@router.get("/keys/detail")
def get_key_detail(key_id: str) -> dict[str, Any]:
    """Budget reset times and limits, which only the per-key route answers correctly.

    ``key_id`` is interpolated into the endpoint path, so it is checked first: an id carrying ``..`` or a
    query string would otherwise make this authenticated GET read some other route with a spend-capable
    token attached.
    """
    if not KEY_ID_RE.match(str(key_id or "")):
        raise HTTPException(status_code=400, detail="key_id is not a key id")
    try:
        return {"key": rd.key_detail(bill._get, key_id), "models": rd.key_models(bill._get, key_id)}
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="vilao key unreachable: %s" % str(exc)[:120]) from exc


@router.get("/topups")
def get_topups(pages: int = 2) -> dict[str, Any]:
    """Every top-up on record: the audit trail behind the ledger invariant the page trusts."""
    try:
        rows = rd.transactions(bill._get, pages=min(4, max(1, int(pages))))
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="vilao wallet unreachable: %s" % str(exc)[:120]) from exc
    paid = [r for r in rows if r.get("status") == "paid"]
    return {"transactions": rows, "paid_total": round(sum(float(r.get("paid") or 0) for r in paid), 2),
            "paid_count": len(paid), "account": rd.me(bill._get)}


@router.get("/keys")
def get_keys(refresh: bool = False) -> dict[str, Any]:
    """The account's vilao API keys, for the Vilao mode's per-key filter and row labels."""
    try:
        rows = rd.keys(bill._get)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="vilao keys unreachable: %s" % str(exc)[:120]) from exc
    return {"keys": rows}


@router.get("/matrix")
def get_matrix(days: str = "30") -> dict[str, Any]:
    span = _days(days, 30)
    return {"days": span, "rows": store.matrix(span)}


@router.get("/summary")
def get_summary(days: str = "30", profile: Optional[str] = None,
                source: Optional[str] = None) -> dict[str, Any]:
    """Day series + day×model matrix for the Trend and Log tabs.

    ``source`` separates the two feeds the store mixes: ``hook`` is what Hermes itself called (every
    provider, dollars estimated), ``bill`` is what vilao charged (dong, real). The Log tab reads the first,
    the bill trend reads the second, and "all" is the account total.
    """
    span = _days(days, 30)
    who = profile or None
    daily = store.daily(span, who, source or None)
    matrix = store.matrix(span, who)
    return {"days": span, "summary": _rollup(span, daily), "matrix": matrix,
            "bill": store.bill_span(),
            "profiles": store.profiles_seen(),
            "store_root": str(Path(store.store_path()).parent),
            "sync": _status(), "balance_available": bool(bill.pat())}


def _rollup(span: int, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals derived from the same day rows the page draws, so a sum can never disagree with its chart."""
    dong = sum(float(r["dong"] or 0) for r in rows)
    usd = sum(float(r["usd"] or 0) for r in rows)
    tokens = {k: round(sum(float(r[k] or 0) for r in rows))
              for k in ("tokens", "tok_in", "tok_out", "tok_cache_read", "tok_reasoning")}
    spend = [r for r in rows if float(r["dong"] or 0) > 0][-7:]
    return {
        "days": span,
        "calls": sum(int(r["calls"]) for r in rows),
        "dong": round(dong, 2),
        "usd_estimated": round(usd, 6),
        "dong_per_day": round((sum(float(r["dong"]) for r in spend) / len(spend)) if spend else 0.0, 2),
        "tokens": int(tokens["tokens"]), "token_parts": tokens,
        "days_by_date": rows,
        "counts": store.match_states(),
    }


@router.get("/log")
def get_log(days: str = "30", profile: Optional[str] = None,
            source: Optional[str] = None) -> dict[str, Any]:
    """Per-model totals from the call log -- every model Hermes touched, not only the billed ones."""
    span = _days(days, 30)
    who = profile or None
    rows = store.usage_by_model(days=span, profile=who, source=source or None)
    return {"days": span, "models": rows}


@router.get("/sessions")
def get_sessions(days: str = "30", profile: Optional[str] = None, limit: int = 60) -> dict[str, Any]:
    """Per-session rollup of the Hermes log -- what one conversation actually cost.

    Only hook rows carry a session id, so this is a log view by construction: the billing feed has no
    session to group by and is not silently folded in.
    """
    span = _days(days, 30)
    who = profile or None
    rows = store.sessions(span, who, limit)
    return {"days": span, "sessions": rows}


@router.get("/profiles")
def get_profiles() -> dict[str, Any]:
    """Which homes the shared store actually holds calls for.

    The page needs this to say what it is showing: the store covers every profile, so a home whose plugin
    is not enabled simply does not appear, and an empty number then means "not contributing", not "no use".
    A row with an empty label is the vilao billing feed, which belongs to the account and carries no home.
    """
    return {"profiles": store.profiles_seen(), "store": store.store_path()}


@router.get("/balance")
def get_balance() -> dict[str, Any]:
    """Live vilao wallet figures for the Wallet tab, read on demand, plus the local 24 h bill total.

    ``spent_24h`` comes from the store, not vilao: the account has no rolling-24-hour endpoint, and the
    per-call rows already hold every billed dong.
    """
    try:
        data = bill.account_snapshot()
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail="vilao bill unreachable: %s" % str(exc)[:120]) from exc
    return {"balance": data, "spent_24h": store.dong_since(86400.0),
            "per_key_24h": store.dong_by_key_since(86400.0)}


@router.post("/sync")
def post_sync(days: str = "30", pages: int = DEFAULT_BUDGET, pulse: bool = False,
              restart: bool = False) -> dict[str, Any]:
    """Pull up to ``pages`` bill pages now and report the cursor.

    The cursor is the whole point of this route: a deep walk spans hundreds of pages, so it has to be
    driven a few pages per request and RESUME on the next one. ``restart`` is therefore only ever sent
    on the first request of a run -- sending it every time would re-read the same newest pages forever
    and never reach the older ones. An incremental pull inside the cooldown is refused so a re-render
    cannot hammer vilao's 120 req/min ceiling.

    ``pulse`` is the scheduled refresh: it always starts at the newest page (the durable cursor belongs
    to deep walks, and resuming one would make the refresh read months-old rows and call that current)
    and stops at the first page holding nothing new.
    """
    budget = max(1, min(int(pages), MAX_BUDGET))
    cursor = 1 if (restart or pulse) else int(store.get_meta("bill_page", "1") or 1)
    if not restart and not pulse and cursor == 1:
        last = _state["at"]
        if last and time.time() - float(last) < SYNC_COOLDOWN_S:
            return {"started": False, "reason": "cooldown", "page": cursor,
                    "seconds_left": round(SYNC_COOLDOWN_S - (time.time() - float(last))), **_status()}
    span = _days(days, 30)
    try:
        result = bill.ingest(store, days=span, start_page=cursor, budget=budget,
                             fresh_stop=bool(pulse))
    except PermissionError as exc:
        store.set_meta("bill_page", "1")
        _state.update(error=str(exc), at=time.time(), page=1)
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        store.set_meta("bill_page", "1")
        _state.update(error=type(exc).__name__ + ": " + str(exc)[:160], at=time.time(), page=1)
        raise HTTPException(status_code=502, detail=str(exc)[:160]) from exc
    # A pulse always reads from the newest page and reports page 1 when it finishes, so writing its
    # cursor would rewind a deep walk that is still in progress. Only real walks move the cursor.
    if not pulse:
        store.set_meta("bill_page", str(result["next_page"]))
    _state.update(at=time.time(), result=result, error=None, page=result["next_page"])
    return {"started": True, "restart": bool(restart), "pulse": bool(pulse), "days": span,
            **result, **_status()}
