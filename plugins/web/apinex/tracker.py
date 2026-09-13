"""Request meter for the APInex key pool: every APInex HTTP call appends one
row to a shared SQLite file (WAL mode, multiprocess-safe). The meter
dashboard reads this file; the tracker never breaks tool calls (fail-open).

Schema (``requests``):
    ts, profile, key_no, key_fp, endpoint, ok, status, latency_ms,
    limit_remaining, limit_reset_s   (last two from upstream headers when present)

Long-term storage: raw rows are pruned past ``RAW_RETENTION_DAYS`` (default
30) while per-hour rollups in ``hourly`` are kept indefinitely for trends.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

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
                limit_reset_s INTEGER
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts)")
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
    profile: str,
    key_no: int,
    key_fp: str,
    endpoint: str,
    ok: bool,
    status: Optional[int],
    latency_ms: float,
    limit_remaining: Optional[int] = None,
    limit_reset_s: Optional[int] = None,
) -> None:
    """Append one meter row + bump the hourly rollup. Never raises."""
    con = _connect()
    if con is None:
        return
    try:
        now = time.time()
        with con:
            con.execute(
                "INSERT INTO requests(ts, profile, key_no, key_fp, endpoint, ok, status,"
                " latency_ms, limit_remaining, limit_reset_s)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now, profile, key_no, key_fp, endpoint, 1 if ok else 0,
                 status, latency_ms, limit_remaining, limit_reset_s),
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
