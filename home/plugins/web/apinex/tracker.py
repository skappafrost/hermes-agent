"""Request meter for the APInex key pool: every APInex HTTP call appends one
row to a shared SQLite file (WAL mode, multiprocess-safe). The meter
dashboard reads this file; the tracker never breaks tool calls (fail-open).

Schema (``requests``):
    ts, profile, key_no, key_fp, endpoint, ok, status, latency_ms,
    limit_remaining, limit_reset_s   (last two from upstream headers when present)

Backends (``route``): every row is tagged 'apinex' (pool calls, key_no 1..9)
or 'fallback' (the Exa safety net that serves calls APInex could not —
key_no 0). Requests APInex rejects outright (e.g. HTTP 402 after the paid
tier switch) keep route='apinex' with ok=0 and the status code.

A single *logical* call (one tool invocation) can span several attempt rows:
key rotation, backoff retries, and the Exa rescue. All of them share one
``call_id``, so the dashboard can count calls instead of raw attempts: a call
whose rescue succeeded is a SUCCESS (via Exa), not a failure. Legacy rows
have NULL call_id and each counts as its own call.

Long-term storage: raw rows are pruned past ``RAW_RETENTION_DAYS`` (default
30) while per-hour rollups in ``hourly`` are kept indefinitely for trends.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

RAW_RETENTION_DAYS = 30
_DB_TIMEOUT_S = 30.0


def db_path() -> Path:
    """Shared meter DB. Override with ``APINEX_METER_DB`` (tests use this)."""
    from plugins.web._common import provider_env

    override = (provider_env("APINEX_METER_DB") or "").strip()
    if override:
        return Path(override)
    home = (os.environ.get("HERMES_HOME") or "").replace("\\", "/").rstrip("/")
    parts = home.split("/") if home else []
    if len(parts) >= 2 and parts[-2] == "profiles":
        base = "/".join(parts[:-2])  # .../hermes (main home, shared by all profiles)
    elif home:
        base = home
    else:
        base = str(Path.home())
    return Path(base) / "apinex-meter" / "meter.db"


def _connect() -> Optional[sqlite3.Connection]:
    try:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path), timeout=_DB_TIMEOUT_S)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute(
            """CREATE TABLE IF NOT EXISTS requests(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                profile TEXT NOT NULL,
                key_no INTEGER NOT NULL,
                key_fp TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                ok INTEGER NOT NULL,
                status INTEGER,
                latency_ms REAL,
                limit_remaining INTEGER,
                limit_reset_s INTEGER,
                req_summary TEXT,
                resp_bytes INTEGER,
                result_count INTEGER,
                route TEXT NOT NULL DEFAULT 'apinex',
                call_id INTEGER
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts)")
        # Migrate pre-existing DBs (blind ALTER would error on fresh ones, hence the check).
        try:
            have = {r[1] for r in con.execute("PRAGMA table_info(requests)").fetchall()}
            for _col, _ddl in (("req_summary", "TEXT"), ("resp_bytes", "INTEGER"),
                               ("result_count", "INTEGER"),
                               ("route", "TEXT NOT NULL DEFAULT 'apinex'"),
                               ("call_id", "INTEGER")):
                if _col not in have:
                    con.execute(f"ALTER TABLE requests ADD COLUMN {_col} {_ddl}")
        except Exception:
            pass
        con.execute(
            """CREATE TABLE IF NOT EXISTS hourly(
                hour INTEGER NOT NULL,
                endpoint TEXT NOT NULL,
                key_no INTEGER NOT NULL,
                count INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (hour, endpoint, key_no)
            )"""
        )
        return con
    except Exception as exc:  # noqa: BLE001 — metering must never break tools
        logger.debug("APInex meter unavailable: %s", exc)
        return None


def new_call_id() -> int:
    """Unique id for one logical tool call.

    Passed to every :func:`log_request` made while serving that call so the
    dashboard can group attempts (key rotations, retries, Exa rescue).
    A random 48-bit value — collision-free across processes without any
    shared state, and attempts of one call always run in one process.
    """
    return int.from_bytes(os.urandom(6), "big")


# Ambient logical-call scope. The tool layer (or the apinex chain) opens
# ``call_scope()`` once per user-facing call; every ``log_request`` inside —
# no matter which provider serves it — inherits the same call_id, so the
# dashboard can group the APInex attempt + SearchX/UnSearch/Exa rescue into
# ONE logical call with its total wall-clock latency. A contextvar (not a
# plain global) because the dispatcher runs concurrent tool calls on threads
# and asyncio tasks. The value is a MUTABLE dict, not a scalar: the tool
# layer's scope must survive ``asyncio.to_thread`` (which copies the context
# into the worker thread — scalar sets there would be invisible to the
# parent, while mutations of the shared dict are seen through every copy).
_scope_var: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "apinex_meter_scope", default=None
)


def current_call_id() -> Optional[int]:
    scope = _scope_var.get()
    return scope["call_id"] if scope else None


def attempt_count() -> int:
    """Log requests made inside the currently open call scope (0 = none)."""
    scope = _scope_var.get()
    return scope["attempts"] if scope else 0


@contextlib.contextmanager
def call_scope(call_id: Optional[int] = None) -> Iterator[int]:
    """Bind one logical call id for every metered attempt inside the block."""
    scope = {"call_id": call_id if call_id is not None else new_call_id(), "attempts": 0}
    tok = _scope_var.set(scope)
    try:
        yield scope["call_id"]
    finally:
        _scope_var.reset(tok)


@contextlib.contextmanager
def ensure_call_scope() -> Iterator[int]:
    """Reuse the ambient call scope when one is open, else own a fresh one.

    Lets every metered entry point (the tool layer AND the apinex provider
    chain when called directly, e.g. from tests or scripts) wrap without
    double-splitting a call: the innermost opener is always the outer scope.
    """
    scope = _scope_var.get()
    if scope is not None:
        yield scope["call_id"]
        return
    with call_scope() as cid:
        yield cid


def parse_limit_headers(headers) -> tuple[Optional[int], Optional[int]]:
    """Best-effort ``(remaining, reset_epoch)`` from upstream rate-limit headers."""
    remaining: Optional[int] = None
    reset: Optional[int] = None
    try:
        items = dict(headers or {})
    except Exception:  # noqa: BLE001
        return None, None
    lowered = {str(k).lower(): v for k, v in items.items()}
    for k, v in lowered.items():
        if "ratelimit" in k and "remain" in k:
            try:
                remaining = int(str(v).split(",")[0].strip())
            except (ValueError, TypeError):
                pass
        elif k in ("x-ratelimit-reset", "ratelimit-reset", "x-rate-limit-reset"):
            try:
                reset = int(str(v).split(",")[0].strip())
            except (ValueError, TypeError):
                pass
    return remaining, reset


def log_request(
    *,
    profile: Optional[str] = None,
    key_no: int,
    key_fp: str,
    endpoint: str,
    ok: bool,
    status: Optional[int],
    latency_ms: float,
    limit_remaining: Optional[int] = None,
    limit_reset_s: Optional[int] = None,
    req_summary: Optional[str] = None,
    resp_bytes: Optional[int] = None,
    result_count: Optional[int] = None,
    route: str = "apinex",
    call_id: Optional[int] = None,
) -> None:
    """Append one meter row + bump the hourly rollup. Never raises.

    ``call_id`` (from :func:`new_call_id`) groups every attempt of one
    logical tool call — key rotations, retries, and fallback rescues — so
    the dashboard can measure call-level success instead of attempts.
    Omit it to inherit the ambient :func:`call_scope` id (the whole
    APInex → SearchX → Exa chain then rides one logical call); omit
    *profile* to auto-detect the running Hermes profile.
    """
    con = _connect()
    if con is None:
        return
    if call_id is None:
        call_id = current_call_id()
    if not profile:
        try:
            from . import keypool as _pool

            profile = _pool.profile_name()
        except Exception:  # noqa: BLE001
            profile = "?"
    try:
        scope = _scope_var.get()
        if scope is not None:
            scope["attempts"] += 1
    except Exception:  # noqa: BLE001 — context var bump is cosmetic bookkeeping
        pass
    try:
        now = time.time()
        with con:
            con.execute(
                "INSERT INTO requests(ts, profile, key_no, key_fp, endpoint, ok, status,"
                " latency_ms, limit_remaining, limit_reset_s,"
                " req_summary, resp_bytes, result_count, route, call_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, profile, key_no, key_fp, endpoint, 1 if ok else 0,
                 status, latency_ms, limit_remaining, limit_reset_s,
                 (req_summary or "")[:400] if req_summary else None,
                 resp_bytes, result_count, route, call_id),
            )
            hour = int(now // 3600) * 3600
            con.execute(
                "INSERT INTO hourly(hour, endpoint, key_no, count, errors)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(hour, endpoint, key_no) DO UPDATE SET"
                " count = count + 1, errors = errors + excluded.errors",
                (hour, endpoint, key_no, 1, 0 if ok else 1),
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("APInex meter write failed: %s", exc)
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass


def prune_raw(retention_days: int = RAW_RETENTION_DAYS) -> int:
    """Delete raw rows older than ``retention_days`` (rollups stay). Returns rows removed."""
    con = _connect()
    if con is None:
        return 0
    try:
        cutoff = time.time() - retention_days * 86400
        with con:
            cur = con.execute("DELETE FROM requests WHERE ts < ?", (cutoff,))
            return cur.rowcount or 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("APInex meter prune failed: %s", exc)
        return 0
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
