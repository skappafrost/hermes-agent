"""Provider Usage: backend half.

FastAPI router mounted at ``/api/plugins/provider-usage/`` by the Hermes
dashboard server. Serves one aggregate JSON payload for the desktop status
bar plus the live call feed; provider API calls happen here, on the user's
machine.

Vilao AI is the only provider with a remote meter here. Everything else this
account calls is a free tier (NVIDIA NIM, OpenCode Zen, Kilo), and those
publish no usage endpoint and send no ``x-ratelimit-*`` headers, so there was
nothing to read -- the ten fetchers that used to probe them always resolved to
"no credential" and were removed rather than kept as dead weight. What those
free providers *can* be measured by is Hermes' own call log, which the Usage
page tabulates per provider and per minute; the popover says so and links
there.

Providers appear only when their credentials are usable; unconfigured
providers are omitted (never rendered as errors). Credential handling is
read-only throughout: no writes, no token refresh, no browser cookies, no
credential CLIs.

Credential sources (read-only):

- Vilao AI: ``VILAO_PAT`` (the console Personal Access Token, not the ``sk-``
  inference key in ``HERMES_CUSTOM_VILAO_AI_API_KEY``). Vilao issues one
  full-scope token per account, so this fetcher only ever reads
  ``/account/balance``; no top-up, key or token endpoint is called.

Endpoints hit (GET, machine credentials attached):

- https://vilao.ai/api/v2/account/balance
- https://open.er-api.com/v6/latest/USD (public, keyless — the rate dollar and yuan rows are shown at)

All HTTPS calls use httpx in-process; no subprocesses are spawned.

Display currency is dong. Every money row leaves this module converted to VND and rounded to whole
dong, with the provider's own figure preserved in the row's ``quoted`` field, because the account that
pays for the calls bills in dong and a status bar that mixes dollars, yuan and dong answers no
question the user is actually asking.

``/recent`` reads the newest calls out of the Usage page's SQLite store at the install root -- the one
file that covers all five profiles and both feeds -- opening it read-only so a status-bar poll can
never take a write lock the ingest path is holding.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter

router = APIRouter()

TTL = 300.0
PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _load(modname: str, filename: str):
    """Load a sibling module by path.

    The dashboard mounts this file inside the plugin host process, where there is no parent package for
    a relative import to resolve against -- the same trap that makes the Usage page load its own
    helpers this way.
    """
    spec = importlib.util.spec_from_file_location(modname, PLUGIN_ROOT / filename)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fx = _load("provider_usage_fx", "fx.py")

_lock = threading.Lock()
_cache = {"at": 0.0, "payload": None}


class _Skip(Exception):
    """Provider has no usable credentials -> omitted from the payload."""

PROVIDER_META = {
    "vilao-ai": {"name": "Vilao AI", "tag": "Credit"},
}

# Display and fetch order for the panel.
FETCH_ORDER = ("vilao-ai",)


def _hermes_env_value(name):
    """Read-only fallback: the profile's own .env (never os.environ)."""
    try:
        from hermes_constants import get_hermes_home
        env_path = get_hermes_home() / ".env"
    except Exception:
        env_path = Path.home() / ".hermes" / ".env"
    try:
        for line in env_path.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return None


def _secret(*names):
    """First resolvable credential among ``names`` through Hermes' secret
    scope, falling back to the profile's .env. Read-only, no os.environ."""
    for name in names:
        try:
            from agent.secret_scope import get_secret
            value = get_secret(name)
            if value:
                return value
        except Exception:
            pass
        value = _hermes_env_value(name)
        if value:
            return value
    return None


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _decode_json_body(resp):
    """Parse a provider body, refusing non-JSON payloads.

    Some gateways answer HTTP 200 with the literal text "Not Found", and a
    plain resp.json() turns that into a bare JSONDecodeError. Report a clear,
    shape-only error instead.

    The response never leaves this function: _collect() turns exceptions into
    {"error": str(exc)[:160]} and the panel renders that, so a provider that
    reflects a rejected credential back in its body must not reach the UI.
    """
    text = (resp.text or "").lstrip()
    if not text.startswith(("{", "[")):
        raise ValueError(
            "non-JSON response (HTTP %d, %d bytes)"
            % (resp.status_code, len(text))
        )
    return json.loads(text) or {}


def _http_json(url, headers, *, timeout=12.0):
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(url, headers=headers)
        if resp.status_code in (401, 403):
            # 403 covers both a rejected credential and a missing plan
            # entitlement (e.g. OpenCode Go without a subscription).
            raise PermissionError("HTTP %d: credential rejected or entitlement missing" % resp.status_code)
        if resp.status_code == 404:
            raise FileNotFoundError("HTTP 404: endpoint not offered for this account")
        resp.raise_for_status()
        return _decode_json_body(resp)


# --------------------------------------------------------------------------- #
# Fetchers: providers resolved by Hermes' read-only account-usage helpers
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# The Usage page's call store: one SQLite file at the install root, holding every profile's calls from
# both feeds. Read-only here -- a status-bar poll must never take a lock the bill ingest is competing
# for, and on Windows a held handle also blocks the file from being replaced.
# --------------------------------------------------------------------------- #


def _open_store(db_path):
    if not db_path:
        return None
    path = Path(db_path)
    if not path.exists():
        return None
    try:
        import sqlite3

        return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1.0)
    except Exception:
        return None


# The Usage page stores each row's local calendar day in an indexed `day` column. Reading it costs a
# fraction of a millisecond; the equivalent date(ts,'unixepoch','localtime') predicate has to convert
# every one of ~42k rows, which measured 26 ms per poll -- and this module polls every 5 s. A store
# written by an older build has no such column, so the shape is probed and the expression form is the
# fallback. The probe expires: this plugin must not be able to sit on a "no column" answer from before
# the Usage page upgraded the file, which would leave it reading slowly with nothing to show for it.
# Connections are deliberately not pooled: a bare connect measured 0.22 ms, and a long-lived handle is
# what blocks SQLite file replacement on Windows.
_day_column: dict[str, tuple[float, bool]] = {}
DAY_PROBE_TTL_S = 300.0


def _has_day(conn, db_path: str) -> bool:
    key = str(db_path).lower()
    hit = _day_column.get(key)
    if hit is not None and time.time() - hit[0] < DAY_PROBE_TTL_S:
        return hit[1]
    try:
        have = any(r[1] == "day" for r in conn.execute("PRAGMA table_info(calls)"))
    except Exception:
        have = False
    _day_column[key] = (time.time(), have)
    return have


def _day_predicate(conn, db_path: str, column: str = "day") -> tuple[str, str]:
    """Return (column_or_expression, predicate for "is this local day")."""
    if _has_day(conn, db_path):
        return column, "%s = ?" % column
    expr = "date(ts, 'unixepoch', 'localtime')"
    return expr, "%s = ?" % expr


def _today_from_usage_store(db_path, today: str) -> Optional[float]:
    """Sum today's billed dong from the Usage page's store, or None when it cannot answer.

    None (not 0.0) is the "don't know" signal, so the caller can fall back: a store that exists but
    holds no bill rows yet is not evidence that today was free.
    """
    conn = _open_store(db_path)
    if conn is None:
        return None
    try:
        _expr, pred = _day_predicate(conn, db_path)
        rows = conn.execute(
            "SELECT COUNT(*) AS billed,"
            " SUM(CASE WHEN %s THEN COALESCE(bill_cost_vnd, 0) ELSE 0 END) AS dong"
            " FROM calls WHERE bill_cost_vnd IS NOT NULL" % pred,
            (today,)).fetchone()
    except Exception:
        return None
    finally:
        conn.close()
    if not rows or not rows[0]:
        return None
    return float(rows[1] or 0.0)


def _vilao_today_spend(used_balance: Optional[float]) -> float:
    """Dong spent today — two lower bounds, and the larger one wins.

    The store of billed per-call rows (the Usage page's SQLite at the install root, a sibling plugin's
    file, so optional: missing or unreadable contributes nothing) only knows what has been synced, and the
    ``used_balance`` delta since the first poll of today only covers the window since the app woke up.
    Paging ``/llm/usage`` on demand would undercount badly — 100 records spanned barely 3 hours of this
    account — so neither path does that.
    """
    today = time.strftime("%Y-%m-%d")
    try:
        # The Usage page's store and this plugin's day anchor both live under the install ROOT, because
        # the store is shared by all five profiles and `used_balance` is an account-level number.
        from hermes_constants import get_default_hermes_root

        home = get_default_hermes_root()
    except Exception:
        return 0.0
    billed = _today_from_usage_store(home / "plugin-data" / "resetwatch" / "calls.sqlite", today) or 0.0
    path = home / "plugin-data" / "provider-usage" / "vilao-day.json"
    if used_balance is None:
        return float(billed)
    anchor = None
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
        if stored.get("date") == today and isinstance(stored.get("start_used"), (int, float)):
            anchor = float(stored["start_used"])
    except (OSError, ValueError):
        anchor = None
    if anchor is None:
        anchor = used_balance
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"date": today, "start_used": used_balance}), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
    return max(float(billed), max(0.0, used_balance - anchor))


def fetch_vilao():
    """Vilao AI wallet: what is left, what went in, and what today has taken.

    The account ledger is denominated in VND: ``balance + used_balance`` equals the sum of paid
    top-ups, verified against ``/wallet/transactions``. There is no rate-limit-remaining endpoint and
    the daily/monthly budget resets come back unset, so this provider reports money rows only —
    never windows.

    Bar semantics on purpose: ``total`` is the lifetime top-up figure, so the Remaining mode reads
    ``<còn lại>/<đã nạp>`` and Used reads today's spend (falling back to the balance on a day that has
    not spent yet). ``today_spent`` travels on the same row and is what the popover's Hôm nay mode puts
    in front of the balance.

    ``used_balance`` only ever grows (a top-up moves ``balance``, not it), which is what makes the
    day delta below exact rather than an estimate.
    """
    pat = _secret("VILAO_PAT")
    if not pat:
        raise _Skip("vilao-ai")
    payload = _http_json(
        "https://vilao.ai/api/v2/account/balance",
        {"Authorization": "Bearer " + pat},
    )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Vilao returned no balance object")
    left = _num(data.get("balance"))
    if left is None:
        raise RuntimeError("Vilao returned no balance field")
    used = _num(data.get("used_balance"))
    spent_today = _vilao_today_spend(used)
    # Whole dong: the API carries sub-dong fractions the account can never hold, and a row that leaves
    # here already in VND is not converted again, so this is where the rounding has to happen.
    row = {"label": "Balance", "left": round(left), "cur": "VND"}
    if spent_today > 0:
        row["today_spent"] = round(spent_today)
    # `total` is the lifetime top-up figure, derived rather than fetched: the ledger holds the invariant
    # balance + used_balance == SUM(amount_paid) (measured twice, 140.000đ on 2026-10-06 and 190.000đ on
    # 2026-10-08), so the Remaining mode can print <còn lại>/<đã nạp> without a second round-trip to
    # /wallet/transactions every 5 seconds.
    if used is not None:
        row["total"] = round(left + used)
    money = [row]
    withdrawable = _num(data.get("withdrawable_balance"))
    if withdrawable:
        money.append({"label": "Withdrawable", "left": round(withdrawable), "cur": "VND"})
    return {"id": "vilao-ai", "status": "ok", "money": money}


FETCHERS = {"vilao-ai": fetch_vilao}

# --------------------------------------------------------------------------- #
# Burn-rate history: every fresh collection records (time, value) samples, and
# windows / money rows gain a projected time to 100% / exhaustion when a
# positive trend is visible (at least 30 min of history within the last 6 h).
# In-memory only; a restart just pauses projections until samples reaccumulate.
# --------------------------------------------------------------------------- #

_history = {}  # (provider id, row key) -> [[t, value], ...]
_history_lock = threading.Lock()
_HISTORY_WINDOW = 6 * 3600
_HISTORY_MIN_SPAN = 1800
_HISTORY_MAX_AGE = 24 * 3600


def _record(key, value, now):
    with _history_lock:
        samples = _history.setdefault(key, [])
        if samples and value < samples[-1][1] - 0.001:
            samples.clear()  # the window reset: start a fresh cycle
        if not samples or now - samples[-1][0] >= 60:
            samples.append([now, value])
        while samples and now - samples[0][0] > _HISTORY_MAX_AGE:
            del samples[0]
        del samples[:-400]


def _project(key, current, target, now):
    """Seconds until ``target`` at the observed rate, or None when unknown."""
    with _history_lock:
        samples = list(_history.get(key) or ())
    window = [s for s in samples if now - s[0] <= _HISTORY_WINDOW]
    if len(window) < 2:
        return None
    t0, v0 = window[0]
    t1, v1 = window[-1]
    if t1 - t0 < _HISTORY_MIN_SPAN:
        return None
    rate = (v1 - v0) / (t1 - t0)
    if rate <= 0:
        return None
    remaining = target - current
    if remaining <= 0:
        return 0
    eta = remaining / rate
    if eta <= 0 or eta > 60 * 86400:
        return None
    return round(eta)


def _decorate(pid, p, now):
    for w in p.get("windows") or []:
        v = w.get("percent")
        if not isinstance(v, (int, float)):
            continue
        key = (pid, str(w.get("label") or "?"))
        _record(key, float(v), now)
        eta = _project(key, float(v), 100.0, now)
        if eta is not None:
            w["eta_seconds"] = eta
    for m in p.get("money") or []:
        total = m.get("total")
        left = m.get("left")
        if not isinstance(total, (int, float)) or not isinstance(left, (int, float)) or total <= 0:
            continue
        used = float(total) - float(left)
        key = (pid, "money:" + str(m.get("label") or "?"))
        _record(key, used, now)
        eta = _project(key, used, float(total), now)
        if eta is not None:
            m["depletes_in_seconds"] = eta


def _to_dong(p, table):
    """Rewrite one provider's money rows into dong, keeping what the provider actually quoted.

    Done after :func:`_decorate`, so the depletion projection is a rate of burn measured in the
    provider's own currency and the exchange rate never enters it. Ratios are unit-free, so the meter,
    the used/remaining mode and the warning threshold all behave the same on converted numbers.

    A row whose currency the table does not know is left alone: printing a wrong conversion is worse
    than printing a dollar sign, and the popover says what it is.
    """
    for m in p.get("money") or []:
        cur = str(m.get("cur") or "USD").upper()
        if cur == "VND":
            continue
        left = fx.to_vnd(m.get("left"), cur, table)
        if left is None:
            continue
        m["quoted"] = {"left": m.get("left"), "total": m.get("total"), "cur": cur}
        m["left"] = round(left)
        if isinstance(m.get("total"), (int, float)):
            m["total"] = round(fx.to_vnd(m["total"], cur, table))
        if isinstance(m.get("today_spent"), (int, float)):
            m["today_spent"] = round(fx.to_vnd(m["today_spent"], cur, table))
        m["cur"] = "VND"


def _collect():
    results = {}
    for pid in FETCH_ORDER:
        try:
            results[pid] = FETCHERS[pid]()
        except _Skip:
            continue
        except Exception as exc:  # a dead provider must never blank the bar
            results[pid] = {"id": pid, "status": "error", "error": str(exc)[:160]}

    providers = []
    now = time.time()
    # One rate for the whole payload: providers priced in dollars and yuan are shown in dong below, and
    # two rows converted at two different moments would not add up to the total the bar prints.
    table = fx.rates() if fx else None
    for pid in FETCH_ORDER:
        p = results.get(pid)
        if p is None:
            continue
        meta = PROVIDER_META[pid]
        p.setdefault("name", meta["name"])
        p.setdefault("tag", meta["tag"])
        if p.get("status") == "ok":
            _decorate(pid, p, now)
            if table:
                _to_dong(p, table)
        providers.append(p)
    return {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "providers": providers,
            "fx": table}


def payload(fresh=False):
    """Cached aggregate payload; ``fresh=True`` forces a provider round-trip."""
    with _lock:
        now = time.time()
        if not fresh and _cache["payload"] is not None and now - _cache["at"] < TTL:
            return _cache["payload"]
        result = _collect()
        _cache["at"] = now
        _cache["payload"] = result
        return result


@router.get("/usage")
def get_usage(fresh: bool = False):
    """Aggregate usage payload; per-provider failures are isolated in-band."""
    return payload(fresh=fresh)


RECENT_MAX = 20

# Both feeds land in the one table, so the newest rows mix Hermes' hook rows (a dollar estimate) and
# vilao's bill rows (real dong). The dong column therefore says which of the two a number came from.
_RECENT_SQL = (
    "SELECT ts, model, provider, profile, tok_in, tok_out, tok_cache_read, tok_total,"
    " duration_ms, finish_reason, bill_cost_vnd, est_cost_usd, match_state"
    " FROM calls ORDER BY ts DESC LIMIT ?"
)
def _today_sql(day_pred: str) -> str:
    return ("SELECT COUNT(*), SUM(COALESCE(tok_total, 0)), SUM(COALESCE(tok_cache_read, 0)),"
            " ROUND(SUM(COALESCE(bill_cost_vnd, 0))) FROM calls WHERE " + day_pred)


def _dong_days(conn, db, span: int = 14):
    """Real dong per local day for the last ``span`` days, or None when the store cannot answer.

    The Usage page keeps a per-day rollup, and reading it is a few hundred rows; a store from before
    that table has the ``day`` column and is grouped from the call rows. Neither path may fall back to
    ``date(ts,'unixepoch','localtime')``: this module polls every five seconds and the expression form
    converts all 42k rows every time (measured 26 ms). A store too old for either shape returns None and
    the popover draws no trend, rather than guessing one from the rows it happens to have.
    """
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - span * 86400))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except Exception:
        return None
    try:
        if "day_stats" in tables:
            return [[r[0], round(float(r[1] or 0))] for r in conn.execute(
                "SELECT day, SUM(dong) FROM day_stats WHERE day >= ? AND source = 'bill'"
                " GROUP BY day ORDER BY day", (since,)).fetchall()]
    except Exception:
        return None
    if not _has_day(conn, str(db)):
        return None
    try:
        return [[r[0], round(float(r[1] or 0))] for r in conn.execute(
            "SELECT day, SUM(COALESCE(bill_cost_vnd, 0)) FROM calls"
            " WHERE day >= ? AND bill_cost_vnd IS NOT NULL GROUP BY day ORDER BY day",
            (since,)).fetchall()]
    except Exception:
        return None


def _store_db():
    """The shared call store at the install root, or None when the root cannot be resolved."""
    try:
        from hermes_constants import get_default_hermes_root

        return get_default_hermes_root() / "plugin-data" / "resetwatch" / "calls.sqlite"
    except Exception:
        return None


def _recent_row(r, rate):
    ts, model, provider, profile = r[0], r[1], r[2], r[3]
    tok_in, tok_out, cache, total = (int(r[4] or 0), int(r[5] or 0), int(r[6] or 0), int(r[7] or 0))
    billed = r[10] is not None
    dong = round(float(r[10])) if billed else (
        round(float(r[11]) * rate) if r[11] is not None and rate else None)
    return {
        "ts": float(ts or 0),
        "model": model or "?",
        "provider": provider or "?",
        "profile": profile or "",
        "tok_in": tok_in,
        "tok_out": tok_out,
        "tok_cache_read": cache,
        "tok_total": total or (tok_in + tok_out + cache),
        "duration_ms": int(r[8] or 0),
        "error": str(r[9] or "") == "error",
        "dong": dong,
        "billed": billed,
    }


def recent(limit: int = 5) -> dict:
    """The newest calls across all five profiles, plus today's totals.

    Never raises: the bar still has a balance to print when the store is missing, locked or from before
    the Usage page existed. But an empty list and a failed read are different facts, and showing them
    alike is how a broken store spends the week looking like a quiet one -- so the reason goes out in
    ``error`` and the popover says it.
    """
    limit = max(1, min(RECENT_MAX, RECENT_LIMIT if limit is None else int(limit)))
    out = {"rows": [], "today": None, "days": None, "store": None, "error": None}
    db = _store_db()
    if db is None:
        out["error"] = "hermes home không xác định được"
        return out
    conn = _open_store(db)
    if conn is None:
        out["store"] = str(db)
        out["error"] = "không mở được store (thiếu file hoặc đang bị khoá)"
        return out
    out["store"] = str(db)
    table = fx.rates() if fx else None
    rate = (table or {}).get("usd_vnd")
    try:
        out["rows"] = [_recent_row(r, rate) for r in conn.execute(_RECENT_SQL, (limit,)).fetchall()]
        _expr, pred = _day_predicate(conn, db)
        n, tokens, cache, dong = conn.execute(_today_sql(pred), (time.strftime("%Y-%m-%d"),)).fetchone()
        out["today"] = {"calls": int(n or 0), "tokens": int(tokens or 0),
                        "cache": int(cache or 0), "dong": int(dong or 0)}
        out["days"] = _dong_days(conn, db)
    except Exception as exc:
        # The rows already read stay useful; only the part that failed is reported as missing.
        out["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    finally:
        conn.close()
    return out


@router.get("/recent")
def get_recent(limit: int = 5):
    """Latest calls for the status-bar popover; read-only store access, no provider round-trip."""
    return recent(limit=limit)


@router.get("/health")
def get_health():
    return {"ok": True}
