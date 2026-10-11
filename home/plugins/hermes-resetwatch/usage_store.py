"""Per-call usage store for the Usage page (plugin-private SQLite).

One file per profile at ``<profile home>/plugin-data/resetwatch/calls.sqlite``. This never opens
Hermes' ``state.db``: rows are written by the observer hook thread, the bill-sync thread and read by
dashboard request threads, so each thread keeps its own connection and WAL arbitrates.

Two ingestion paths land in one table:

- ``record_local``  — a call Hermes itself made, priced from Hermes' own price table (USD estimate).
- ``record_bill``   — a charge vilao billed, in real dong.

They are joined by :func:`match_rows`, never by averaging the two numbers: a row that cannot be
claimed stays ``local_only`` or ``bill_only`` so the UI can say which figure is which.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Iterable, Optional

# 400, not 180: the Trend tab draws twelve months of squares, and a store that pruned at six months
# would leave half the year blank no matter how deep the bill walk went. A year of this account's
# history is ~41k rows, a few tens of MB.
RETENTION_DAYS = 400
MATCH_WINDOW_S = 90.0
MATCH_LOOKBACK = 7 * 86400
TOKEN_TOLERANCE = 0.005

_local = threading.local()
_schema_lock = threading.Lock()
# Per-process boot state, keyed by database path: DDL and the one-time data fixes run once, and the
# legacy drain runs at most every DRAIN_INTERVAL_S (see _connect for the measured cost that made this
# necessary). close_connections() deliberately does not clear these: the schema is in the file.
_booted: set = set()
_last_drain: dict = {}
DRAIN_INTERVAL_S = 60.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    profile TEXT,
    session_id TEXT,
    platform TEXT,
    task TEXT,
    provider TEXT,
    model TEXT,
    model_key TEXT,
    api_mode TEXT,
    tok_in INTEGER DEFAULT 0,
    tok_out INTEGER DEFAULT 0,
    tok_cache_read INTEGER DEFAULT 0,
    tok_cache_write INTEGER DEFAULT 0,
    tok_reasoning INTEGER DEFAULT 0,
    tok_total INTEGER DEFAULT 0,
    duration_ms INTEGER,
    ttft_ms INTEGER,
    finish_reason TEXT,
    context_length INTEGER,
    est_cost_usd REAL,
    bill_cost_vnd REAL,
    bill_key_id TEXT,
    match_state TEXT NOT NULL DEFAULT 'local_only',
    bill_request_id TEXT,
    source TEXT NOT NULL DEFAULT 'hook',
    day TEXT
);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON calls (ts);
CREATE INDEX IF NOT EXISTS idx_calls_model_ts ON calls (model_key, ts);
CREATE INDEX IF NOT EXISTS idx_calls_state ON calls (match_state);
/* Indexes over columns this script cannot add to an existing store -- idx_calls_day, idx_calls_session,
 * idx_calls_key_ts, idx_calls_billed -- are created in _connect after _ensure_columns has run. Declaring
 * one here fails on a store written by an older build, because CREATE TABLE IF NOT EXISTS no-ops and the
 * index is then prepared against a column that does not exist yet. */
/* idx_calls_key_ts is created after _ensure_columns: on a store written before bill_key_id existed,
 * an index in this script would be prepared against a column the script cannot add. */
CREATE UNIQUE INDEX IF NOT EXISTS idx_calls_bill ON calls (bill_request_id)
    WHERE bill_request_id IS NOT NULL;
/* The plugin now lives in the launch scope AND (via junction) the profile scope, and the multiplex
 * gateway hosts both managers in one process, so an observer hook can fire twice for one call.
 * Bill rows have a NULL session_id and NULLs never collide in a unique index, so this guards the
 * local rows only -- exactly where double-counting would inflate today's figures. */
CREATE UNIQUE INDEX IF NOT EXISTS idx_calls_local ON calls (ts, session_id, model)
    WHERE source = 'hook';
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
/* ---- rollups ------------------------------------------------------------------------------
 * Everything the page reads in aggregate -- the Trend calendar, the day series, the per-model
 * rollup, the per-home strip, the billing span -- used to be a GROUP BY over all 42k call rows on
 * every request (30-97 ms each), which is why a 45-second memo was bolted on top of them. These two
 * tables replace that: one row per (day, home, feed) and one per (day, home, model), rebuilt for the
 * affected days inside the same transaction that wrote the calls. Reads become a scan of a few
 * hundred small rows, so the memo, its staleness window and its invalidation hooks are gone.
 *
 * Sums are stored raw (ms_sum with ms_n, not an average) so re-aggregating across days or models is
 * exact: an average of averages would silently weight a 3-call day like a 900-call one. */
CREATE TABLE IF NOT EXISTS day_stats (
    day TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    calls INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    tok_in INTEGER NOT NULL DEFAULT 0,
    tok_out INTEGER NOT NULL DEFAULT 0,
    tok_cache INTEGER NOT NULL DEFAULT 0,
    tok_reasoning INTEGER NOT NULL DEFAULT 0,
    dong REAL NOT NULL DEFAULT 0,
    usd REAL NOT NULL DEFAULT 0,
    ms_sum REAL NOT NULL DEFAULT 0,
    ms_n INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    matched INTEGER NOT NULL DEFAULT 0,
    local_only INTEGER NOT NULL DEFAULT 0,
    bill_only INTEGER NOT NULL DEFAULT 0,
    last_ts REAL,
    PRIMARY KEY (day, profile, source)
);
CREATE TABLE IF NOT EXISTS day_model_stats (
    day TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    model_key TEXT NOT NULL,
    providers TEXT,
    calls INTEGER NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    tok_in INTEGER NOT NULL DEFAULT 0,
    tok_out INTEGER NOT NULL DEFAULT 0,
    tok_cache INTEGER NOT NULL DEFAULT 0,
    dong REAL NOT NULL DEFAULT 0,
    usd REAL NOT NULL DEFAULT 0,
    ms_sum REAL NOT NULL DEFAULT 0,
    ms_n INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    first_ts REAL,
    last_ts REAL,
    PRIMARY KEY (day, profile, source, model_key)
);
CREATE INDEX IF NOT EXISTS idx_dm_day ON day_model_stats (day);
CREATE INDEX IF NOT EXISTS idx_dm_model ON day_model_stats (model_key);
CREATE INDEX IF NOT EXISTS idx_ds_day ON day_stats (day);
/* One row per (day, offer) -- the price watch. The marketplace is the only place that says what a model
 * costs, and it says it differently every few days, so the page can answer "did this get cheaper" only
 * if the answer was written down when the catalogue was last read. Prices are stored net of promo, which
 * is what the invoice charges and what the screen shows: a promo ending is a price rise. */
CREATE TABLE IF NOT EXISTS catalog_prices (
    day TEXT NOT NULL,
    key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    provider TEXT,
    price_in REAL,
    price_out REAL,
    cache_read REAL,
    context INTEGER,
    floor REAL,
    PRIMARY KEY (day, key)
);
CREATE INDEX IF NOT EXISTS idx_cp_day ON catalog_prices (day DESC);
"""


def profile_home() -> str:
    """The profile this call belongs to, resolved at write time.

    The multiplex host binds the owning profile in a contextvar while a turn runs, and ``os.environ``
    keeps the launch profile's home -- so reading the env here would silently file every profile's
    calls under whichever profile booted the gateway.
    """
    from hermes_constants import get_hermes_home

    return str(get_hermes_home())


# Trailing separators are stripped before comparing homes, so a home written with or
# without a final slash resolves to the same label.
SEPS = "/" + (os.sep if os.sep != "/" else "")
_VILAO_PREFIXES = ("vilao-ai/", "vilao/")


def model_key(model: Optional[str]) -> str:
    """Coarse join key: vilao bills ``cx/gpt-6-astra`` while Hermes calls it ``vilao-ai/cx/gpt-6-astra``.

    Only the vilao prefix is stripped. A bill row arrives with no prefix at all, so dropping a leading
    segment in general would turn ``cx/gpt-6-astra`` into ``gpt-6-astra`` and nothing would ever pair.
    """
    name = (model or "?").strip().lower()
    for prefix in _VILAO_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


@contextmanager
def _tx(conn: sqlite3.Connection):
    """One explicit write transaction.

    Needed because every connection here runs in autocommit: without a paired BEGIN/COMMIT a
    multi-statement write (claim a pair, delete the bill) could be seen half-applied by another
    thread.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns that ``CREATE TABLE IF NOT EXISTS`` cannot add to a store written by an older build.

    Dropping and rebuilding is not an option: the bill rows in there are the only record of real dong
    spent, and re-fetching them means paging vilao's slow usage endpoint again.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(calls)")}
    if "bill_key_id" not in have:
        conn.execute("ALTER TABLE calls ADD COLUMN bill_key_id TEXT")
    if "day" not in have:
        conn.execute("ALTER TABLE calls ADD COLUMN day TEXT")


DAY_BACKFILL = "day_column_backfilled"


def _backfill_day(conn: sqlite3.Connection, force: bool = False) -> int:
    """Fill ``day`` on rows written before the column existed.

    Every day-bucketed query used to run ``date(ts,'unixepoch','localtime')`` over all 42k rows, which is
    what made the Trend route 274 ms and the status-bar poll 26 ms every 5 s: no index can serve a
    function of a column. Storing the bucket costs one UPDATE over the rows that lack it.

    ``force`` is used after a legacy import, because those rows arrive from a file written by an older
    build (no ``day`` column at all) and the once-per-store flag would otherwise leave them invisible to
    every day-based view forever.

    The expression matches the write path: Python's ``localtime`` and SQLite's ``localtime`` modifier
    resolve through the same OS zone data, so a row backfilled here and a row written tomorrow bucket
    the same way.
    """
    if not force and conn.execute("SELECT 1 FROM meta WHERE key=? AND value='1'",
                                  (DAY_BACKFILL,)).fetchone():
        # The flag records that the bulk fill ran; it must not make the repair blind. A gateway process
        # still holding the previous build writes rows whose INSERT does not carry ``day`` at all, and
        # with a one-shot flag those rows would stay invisible to every day-based view -- and keep the
        # rollup permanently short of them -- until the app restarted. The probe is an index seek on
        # idx_calls_day, so the steady state costs one lookup rather than an UPDATE over 42k rows.
        if not conn.execute("SELECT 1 FROM calls WHERE day IS NULL LIMIT 1").fetchone():
            return 0
    with _tx(conn):
        cur = conn.execute("UPDATE calls SET day = date(ts, 'unixepoch', 'localtime') WHERE day IS NULL")
        fixed = cur.rowcount or 0
    if not force:
        conn.execute("INSERT INTO meta (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE"
                     " SET value='1'", (DAY_BACKFILL,))
    return fixed


def day_of(ts: float) -> str:
    """The local calendar day a call belongs to, in the same string the SQL buckets use."""
    return time.strftime("%Y-%m-%d", time.localtime(float(ts)))


# --------------------------------------------------------------------------- #
# Rollup maintenance
# --------------------------------------------------------------------------- #
# Every write to `calls` funnels through touch_days() with the days it changed, inside the same
# transaction. Recomputing a whole day (rather than applying a delta) is deliberate: pairing moves a
# charge from a bill row to a hook row and replay-drops delete rows outright, so a delta would have to
# know the difference between an insert, a delete and a re-source to stay exact -- and getting that wrong
# corrupts the one number the user watches. A day is a few hundred rows, so recomputing it is free.

_DAY_STATS_SQL = """
INSERT INTO day_stats (day, profile, source, calls, tokens, tok_in, tok_out, tok_cache,
                       tok_reasoning, dong, usd, ms_sum, ms_n, errors, matched, local_only,
                       bill_only, last_ts)
SELECT day, COALESCE(profile, ''), source, COUNT(*),
       SUM(COALESCE(tok_total, 0)), SUM(COALESCE(tok_in, 0)), SUM(COALESCE(tok_out, 0)),
       SUM(COALESCE(tok_cache_read, 0)), SUM(COALESCE(tok_reasoning, 0)),
       SUM(COALESCE(bill_cost_vnd, 0)), SUM(COALESCE(est_cost_usd, 0)),
       SUM(COALESCE(duration_ms, 0)), COUNT(duration_ms),
       SUM(CASE WHEN finish_reason = 'error' THEN 1 ELSE 0 END),
       SUM(CASE WHEN match_state = 'matched' THEN 1 ELSE 0 END),
       SUM(CASE WHEN match_state = 'local_only' THEN 1 ELSE 0 END),
       SUM(CASE WHEN match_state = 'bill_only' THEN 1 ELSE 0 END), MAX(ts)
FROM calls WHERE day = ? GROUP BY day, profile, source"""

_DAY_MODEL_SQL = """
INSERT INTO day_model_stats (day, profile, source, model_key, providers, calls, tokens, tok_in,
                             tok_out, tok_cache, dong, usd, ms_sum, ms_n, errors, first_ts, last_ts)
SELECT day, COALESCE(profile, ''), source, COALESCE(model_key, '?'),
       GROUP_CONCAT(DISTINCT provider), COUNT(*),
       SUM(COALESCE(tok_total, 0)), SUM(COALESCE(tok_in, 0)), SUM(COALESCE(tok_out, 0)),
       SUM(COALESCE(tok_cache_read, 0)), SUM(COALESCE(bill_cost_vnd, 0)),
       SUM(COALESCE(est_cost_usd, 0)),
       SUM(COALESCE(duration_ms, 0)), COUNT(duration_ms),
       SUM(CASE WHEN finish_reason = 'error' THEN 1 ELSE 0 END), MIN(ts), MAX(ts)
FROM calls WHERE day = ? GROUP BY day, COALESCE(profile, ''), source, COALESCE(model_key, '?')"""


def touch_days(conn: sqlite3.Connection, days: Iterable[Any]) -> int:
    """Rebuild the rollup rows for the given days. Returns how many days were rebuilt.

    Callers pass whatever they have -- a set of ``day`` strings, or the raw ``ts`` values of rows they
    inserted/deleted, which are converted here so no caller has to remember the format. A day that lost
    its last row simply ends up with no rollup row, which is what the readers expect.
    """
    wanted = set()
    for value in days:
        if value is None:
            continue
        if isinstance(value, (int, float)):
            wanted.add(day_of(value))
        else:
            text = str(value)
            wanted.add(text[:10] if len(text) > 10 and "-" in text[:10] else text)
    for day in sorted(wanted):
        conn.execute("DELETE FROM day_stats WHERE day = ?", (day,))
        conn.execute("DELETE FROM day_model_stats WHERE day = ?", (day,))
        conn.execute(_DAY_STATS_SQL, (day,))
        conn.execute(_DAY_MODEL_SQL, (day,))
    return len(wanted)


def _touch_ts(conn: sqlite3.Connection, ts_list: Iterable[Any]) -> None:
    touch_days(conn, ts_list)


def prune_calls(conn: sqlite3.Connection) -> int:
    """Drop rows past retention and rebuild the days they belonged to.

    The deleted rows' days are listed first because after the DELETE they are gone: without that, the
    rollup would keep counting calls the store no longer holds, and the only way to notice would be a
    Trend square that never fades.
    """
    cutoff = time.time() - RETENTION_DAYS * 86400
    days = [r[0] for r in conn.execute("SELECT DISTINCT day FROM calls WHERE ts < ?", (cutoff,))]
    cur = conn.execute("DELETE FROM calls WHERE ts < ?", (cutoff,))
    if days:
        touch_days(conn, days)
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


MODEL_KEY_FIX = "model_key_backfilled"


def _backfill_model_key(conn: sqlite3.Connection) -> int:
    """Derive ``model_key`` for rows stored before it was filled in.

    The pre-shared per-profile files wrote the column but left it NULL, so every one of those rows is
    invisible to the model rollup and cannot pair with a charge -- the join key is what both use. It is
    computed by the same model_key() the write path uses, so a healed row and a row written tomorrow
    land on the same key.
    """
    if conn.execute("SELECT 1 FROM meta WHERE key=? AND value='1'", (MODEL_KEY_FIX,)).fetchone():
        return 0
    rows = conn.execute("SELECT DISTINCT model FROM calls WHERE model_key IS NULL").fetchall()
    with _tx(conn):
        for (model,) in rows:
            conn.execute("UPDATE calls SET model_key = ? WHERE model_key IS NULL AND model IS ?",
                         (model_key(model), model))
        conn.execute("UPDATE calls SET model_key = '?' WHERE model_key IS NULL AND model IS NULL")
        conn.execute("INSERT INTO meta (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE"
                     " SET value='1'", (MODEL_KEY_FIX,))
    return len(rows)


def all_days(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT day FROM calls WHERE day IS NOT NULL ORDER BY day")]


def rebuild_rollups(conn: sqlite3.Connection) -> int:
    """Recompute every day. Used by the first-run migration and by any consistency failure."""
    days = all_days(conn)
    with _tx(conn):
        conn.execute("DELETE FROM day_stats")
        conn.execute("DELETE FROM day_model_stats")
        touch_days(conn, days)
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                     " SET value=excluded.value", (ROLLUP_STAMP, _rollup_state(conn)))
    return len(days)


ROLLUP_STAMP = "rollup_state"


def _rollup_state(conn: sqlite3.Connection) -> str:
    """A fingerprint of what the rollup must describe: count, billed sum and newest row.

    Only rows that *have* a day are counted, because that is the domain a day-keyed rollup can describe.
    Including the rest would make the check permanently fail on a row ``_backfill_day`` has not reached
    yet, and every drain tick would pay a full rebuild that cannot converge.
    """
    row = conn.execute("SELECT COUNT(*), ROUND(COALESCE(SUM(bill_cost_vnd), 0), 4),"
                       " COALESCE(MAX(rowid), 0) FROM calls WHERE day IS NOT NULL").fetchone()
    return "%d|%s|%d" % (int(row[0]), row[1], int(row[2]))


def rollup_consistent(conn: sqlite3.Connection) -> bool:
    """Do the two tables still describe the calls table?

    A cheap cross-check, not a recompute: the calls-side fingerprint (rows, billed dong, highest rowid)
    against the same numbers as seen through the rollup. Anything that wrote to ``calls`` without going
    through touch_days -- an older build, a hand-edited file, a crash between a write and its rebuild --
    shows up here, and the caller rebuilds.
    """
    try:
        want = _rollup_state(conn)
        n = conn.execute("SELECT SUM(calls) FROM day_stats").fetchone()[0]
        dong = conn.execute("SELECT ROUND(SUM(dong), 4) FROM day_stats").fetchone()[0]
        last = conn.execute("SELECT MAX(rowid) FROM calls").fetchone()[0]
        if not n or int(n) == 0:
            return conn.execute("SELECT COUNT(*) FROM calls WHERE day IS NOT NULL").fetchone()[0] == 0
        if int(n) != int(want.split("|")[0]):
            return False
        if abs(float(dong or 0) - float(want.split("|")[1] or 0)) > 0.5:
            return False
        # The newest rowid has to be inside a day the rollup knows about, otherwise a write landed
        # without a rebuild even though the totals happen to still match.
        newest = conn.execute("SELECT day FROM calls WHERE rowid = ?", (last,)).fetchone()
        if newest and newest[0]:
            known = conn.execute("SELECT 1 FROM day_stats WHERE day = ? LIMIT 1", (newest[0],)).fetchone()
            return known is not None
        return True
    except sqlite3.Error:
        return False


def ensure_rollups(conn: sqlite3.Connection) -> str:
    """Build the rollups if they are missing or disagree with the calls table. Returns an action tag."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "day_stats" not in tables or "day_model_stats" not in tables:
        return "absent"
    if rollup_consistent(conn):
        return "ok"
    rebuild_rollups(conn)
    return "rebuilt"


def _conn_key(db_path: str) -> str:
    """One canonical key per database file.

    ``store_path()`` builds backslash paths and a caller may hand over the same file with forward slashes
    (a probe, a test, an as_posix() path). Keying the caches on the raw string made the same file boot
    twice and hold two connections, which measured 20 ms of duplicated migration work per alias.
    """
    return os.path.normcase(os.path.abspath(db_path))


def _connect(db_path: str) -> sqlite3.Connection:
    """One connection per (thread, path); the schema is created at most once per process.

    ``isolation_level=None`` is load-bearing. In the default legacy mode, the first bare SELECT opens
    an implicit read transaction that sqlite3 never closes, so a long-lived route thread would keep
    serving the snapshot it read at startup and never see calls written afterwards.

    The DDL and the data fixes are gated per path per process, and the legacy drain is throttled instead
    of gated. Measured on the live store, the ungated version cost 19-21 ms on every new connection --
    17 ms of that was ``_import_legacy`` scanning the profile directories -- and the route pool hands out
    a fresh thread per request, so a page that fires four queries paid it four times. Throttling the
    import (rather than running it once) is deliberate: the other four homes keep writing into their
    legacy files while this process lives, and a one-shot flag would strand everything written after it
    until the app restarted -- exactly the failure the incremental watermark was built to avoid.
    """
    conns = getattr(_local, "conns", None)
    if conns is None:
        conns = {}
        _local.conns = conns
    key = _conn_key(db_path)
    existing = conns.get(key)
    if existing is not None:
        return existing
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if key not in _booted:
        with _schema_lock:
            if key not in _booted:
                if not _rollup_shape(conn):
                    _drop_rollups(conn)
                conn.executescript(_SCHEMA)
                _ensure_columns(conn)
                _ensure_rollup_columns(conn)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_key_ts ON calls (bill_key_id, ts)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_billed ON calls (ts, day)"
                             " WHERE bill_cost_vnd IS NOT NULL")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_day ON calls (day)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_calls_session ON calls (session_id)")
                _import_legacy(conn, db_path)
                _unattribute_bill_rows(conn)
                _backfill_day(conn)
                _backfill_model_key(conn)
                prune_calls(conn)
                prune_catalog(conn)
                ensure_rollups(conn)
                _booted.add(key)
                _last_drain[key] = time.time()
    elif time.time() - float(_last_drain.get(key) or 0) >= DRAIN_INTERVAL_S:
        _last_drain[key] = time.time()
        _import_legacy(conn, db_path)
        # A process still running the previous build writes rows without ``day`` (its INSERT does not
        # carry the column), and those rows would silently vanish from every day-bucketed view. Re-filling
        # NULL buckets on the drain tick makes the store self-heal within a minute of any old writer,
        # and costs nothing once nobody is writing the old shape. The same tick re-fingerprints the
        # rollups, which is how rows written by such a process get counted instead of going missing.
        _backfill_day(conn, force=True)
        _backfill_model_key(conn)
        prune_calls(conn)
        prune_catalog(conn)
        ensure_rollups(conn)
    else:
        # Retention on every open: 0.1 ms measured, and "pruned at the next open, not on write" is the
        # behaviour the page and its tests rely on. Gating it with the expensive migrations would have
        # let an old row live indefinitely in a long-lived process. prune_calls rebuilds the days it
        # empties, which is the part a bare DELETE cannot do.
        prune_calls(conn)
    conns[key] = conn
    return conn


def close_connections() -> None:
    """Release this thread's handles. Windows refuses to delete a directory while a SQLite file inside
    it is still open, so tests (and any host that retires a plugin) need this to run."""
    conns = getattr(_local, "conns", None)
    if not conns:
        return
    for conn in conns.values():
        try:
            conn.close()
        except sqlite3.Error:
            pass
    conns.clear()


def profile_label() -> str:
    """The home that made this call, named the way the page should show it.

    The root home is not a profile, and ``basename`` would call it ``hermes`` next to ``neo_agent``;
    it is the default, and that is what it is labelled.
    """
    home = profile_home().rstrip(SEPS)
    root = root_home().rstrip(SEPS)
    if home.lower() == root.lower():
        return "default"
    return os.path.basename(home) or "?"


def root_home() -> str:
    """The install root, which is where the shared store lives.

    ``get_default_hermes_root()`` unwraps ``<root>/profiles/<name>`` back to ``<root>``, so a hook running
    under any of the five homes resolves the same file.
    """
    try:
        from hermes_constants import get_default_hermes_root

        return str(get_default_hermes_root())
    except Exception:
        return profile_home()


_COLUMNS = ("ts", "profile", "session_id", "platform", "task", "provider", "model", "model_key",
            "api_mode", "tok_in", "tok_out", "tok_cache_read", "tok_cache_write", "tok_reasoning",
            "tok_total", "duration_ms", "ttft_ms", "finish_reason", "context_length", "est_cost_usd",
            "bill_cost_vnd", "bill_key_id", "match_state", "bill_request_id", "source", "day")


def _import_legacy(conn: sqlite3.Connection, db_path: str) -> int:
    """Copy rows out of the per-profile stores this shared file replaces, incrementally.

    Those files hold the only record of what was billed before the merge (tens of thousands of charges),
    and a page that woke up on a new path and showed nothing would read as "this account never spent".

    It runs on every connect and takes a per-file watermark, not a one-shot flag, because the app keeps
    writing through whichever build it booted with: a flag would strand every call made between the
    import and the restart. Additive throughout -- the unique indexes reject a charge two homes both
    ingested -- and the source files are never touched, so an interrupted run loses nothing.

    Only the shared store imports. If the path resolved to a home under ``profiles/`` the root could not
    be determined, and pulling a sibling's history into that home's file would corrupt both.
    """
    if "profiles" in Path(db_path).parts:
        return 0
    root = Path(root_home())
    total = 0
    for src_path in sorted(root.glob("profiles/*/plugin-data/resetwatch/calls.sqlite")):
        if str(src_path).lower() == str(db_path).lower():
            continue
        key = "legacy:" + src_path.parts[-4]
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        watermark = float(row["value"]) if row else 0.0
        try:
            src = sqlite3.connect(src_path.as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            if not src.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='calls'").fetchone()[0]:
                continue
            # An old file may predate a column this build adds; take the intersection and let the
            # missing one stay NULL instead of losing the whole store to one OperationalError.
            have = {r[1] for r in src.execute("PRAGMA table_info(calls)")}
            cols = [c for c in _COLUMNS if c in have]
            sql = "SELECT %s FROM calls" % ",".join(cols)
            args: list[Any] = []
            if watermark and "ts" in have:
                sql += " WHERE ts > ?"
                args = [watermark]
            rows = src.execute(sql, args).fetchall()
            top = float(src.execute("SELECT COALESCE(MAX(ts), 0) FROM calls").fetchone()[0] or 0)
        except sqlite3.Error:
            src.close()
            continue
        src.close()
        if rows:
            with _tx(conn):
                conn.executemany("INSERT OR IGNORE INTO calls (%s) VALUES (%s)"
                                 % (",".join(cols), ",".join("?" * len(cols))), rows)
                conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                             " SET value=excluded.value", (key, str(top)))
            total += len(rows)
        elif top > watermark:
            conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                         " SET value=excluded.value", (key, str(top)))
    if total:
        # Legacy files predate the `day` column, so the rows just copied in have no bucket yet and would
        # be missing from every day-based view while the once-per-store flag says the backfill is done.
        _backfill_day(conn, force=True)
        # Those rows also arrived without passing through touch_days, so rebuild exactly the days that
        # have calls but no rollup -- stated that way rather than guessed from timestamps.
        touch_days(conn, [r[0] for r in conn.execute(
            "SELECT DISTINCT c.day FROM calls c LEFT JOIN day_stats d ON d.day = c.day"
            " WHERE c.day IS NOT NULL AND d.day IS NULL")])
    return total


# Billing rows used to be stamped with the home that ran the ingest, which filed 41k account-level
# charges under whichever profile woke up first. The rewrite of the write path only fixes rows from now
# on, so the existing ones are corrected once, here, and remembered so a restart does not rescan 42k rows.
BILL_ATTRIBUTION_FIX = "bill_profile_blanked"


def _unattribute_bill_rows(conn: sqlite3.Connection) -> int:
    """Blank ``profile`` on every billing row, once. Returns how many rows were corrected.

    A charge belongs to the account and to the API key that was billed, not to a Hermes profile; the
    pairing that gives a call its profile keeps the *hook* row and deletes the bill row, so no row that
    still says ``source='bill'`` was ever attributed to a real profile.
    """
    done = conn.execute("SELECT 1 FROM meta WHERE key=? AND value='1'", (BILL_ATTRIBUTION_FIX,)).fetchone()
    if done:
        return 0
    with _tx(conn):
        cur = conn.execute("UPDATE calls SET profile='' WHERE source='bill' AND profile<>''")
        fixed = cur.rowcount or 0
        conn.execute("INSERT INTO meta (key, value) VALUES (?, '1') ON CONFLICT(key) DO UPDATE"
                     " SET value='1'", (BILL_ATTRIBUTION_FIX,))
    return fixed


def store_path() -> str:
    """One database for every profile's calls, under the install root.

    Each home used to keep its own file, which made the page blind to the other four and re-ingested the
    same vilao charges once per home -- charges belong to the account, not to a profile. Provenance does
    not need the path any more: a hook row carries the acting profile in ``profile``, and a billing row
    deliberately carries none (see :func:`_unattribute_bill_rows`).

    Falls back to the acting home when the install root cannot be resolved, so a standalone process (the
    test runner, a CLI probe) still gets a writable store instead of an import error.
    """
    return os.path.join(root_home(), "plugin-data", "resetwatch", "calls.sqlite")


def _row_values(ts: float, payload: dict[str, Any], source: str) -> dict[str, Any]:
    usage = payload.get("usage") or {}
    tok_in = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    tok_out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    return {
        "ts": float(ts),
        # Only a hook row knows which home made the call. A bill row is an account-level charge: the
        # profile that happened to run the ingest did not necessarily make it, so billing rows are
        # stored unattributed and the page shows the API key they were charged to instead.
        "profile": profile_label() if source == "hook" else "",
        "session_id": payload.get("session_id"),
        "platform": payload.get("platform"),
        "task": payload.get("aux_task") or payload.get("task"),
        "provider": payload.get("provider"),
        "model": payload.get("model") or payload.get("response_model"),
        "model_key": model_key(payload.get("model") or payload.get("response_model")),
        "api_mode": payload.get("api_mode"),
        "tok_in": tok_in,
        "tok_out": tok_out,
        "tok_cache_read": int(usage.get("cache_read_tokens") or usage.get("cached_input_tokens")
                              or usage.get("cache_read_input_tokens") or 0),
        "tok_cache_write": int(usage.get("cache_write_tokens") or 0),
        "tok_reasoning": int(usage.get("reasoning_tokens") or 0),
        "tok_total": tok_in + tok_out,
        "duration_ms": payload.get("duration_ms"),
        "ttft_ms": payload.get("ttft_ms"),
        "finish_reason": payload.get("finish_reason"),
        "context_length": payload.get("context_length"),
        "est_cost_usd": payload.get("est_cost_usd"),
        "bill_cost_vnd": payload.get("bill_cost_vnd"),
        "bill_key_id": payload.get("bill_key_id"),
        "bill_request_id": payload.get("bill_request_id"),
        "source": source,
        "day": day_of(ts),
    }


_INSERT_SQL = """INSERT INTO calls (
    ts, profile, session_id, platform, task, provider, model, model_key, api_mode,
    tok_in, tok_out, tok_cache_read, tok_cache_write, tok_reasoning, tok_total,
    duration_ms, ttft_ms, finish_reason, context_length,
    est_cost_usd, bill_cost_vnd, bill_key_id, match_state, bill_request_id, source, day
) VALUES (:ts, :profile, :session_id, :platform, :task, :provider, :model, :model_key, :api_mode,
    :tok_in, :tok_out, :tok_cache_read, :tok_cache_write, :tok_reasoning, :tok_total,
    :duration_ms, :ttft_ms, :finish_reason, :context_length,
    :est_cost_usd, :bill_cost_vnd, :bill_key_id, :match_state, :bill_request_id, :source, :day)"""


def record_local(*, ts: float, payload: dict[str, Any], est_cost_usd: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Store one call Hermes made, or return None when this exact call is already stored.

    The None path is what keeps a double-fired observer hook (launch-scope manager plus profile-scope
    manager in one multiplex process) from counting the same call twice.
    """
    values = _row_values(ts, {**payload, "est_cost_usd": est_cost_usd}, "hook")
    values["match_state"] = "local_only"
    conn = _connect(store_path())
    try:
        with _tx(conn):
            conn.execute(_INSERT_SQL, values)
            touch_days(conn, [values["day"]])
    except sqlite3.IntegrityError:
        # Only idx_calls_local can reject a hook row (bill ids are NULL here), so this is the
        # already-stored case rather than a real fault.
        return None
    return dict(values)


def record_bill_rows(rows: Iterable[dict[str, Any]]) -> int:
    """Insert vilao charges, skipping ones already ingested. Returns the number newly stored.

    A skipped row still gets its API key id filled in when it is missing: rows stored before the key
    was tracked are otherwise invisible to the page's per-key filter, and re-deriving that would mean
    rebuilding the store.
    """
    incoming = [r for r in rows if r.get("bill_request_id")]
    if not incoming:
        return 0
    conn = _connect(store_path())
    known = {r["bill_request_id"] for r in incoming}
    placeholders = ",".join("?" * len(known))
    touched: set[str] = set()
    with _tx(conn):
        seen = {row[0]: row[1] for row in conn.execute(
            f"SELECT bill_request_id, bill_key_id FROM calls WHERE bill_request_id IN ({placeholders})",
            tuple(known))}
        added = 0
        for raw in incoming:
            if raw["bill_request_id"] in seen:
                if not seen[raw["bill_request_id"]] and raw.get("bill_key_id"):
                    conn.execute("UPDATE calls SET bill_key_id=? WHERE bill_request_id=?",
                                 (raw["bill_key_id"], raw["bill_request_id"]))
                continue
            values = _row_values(raw["ts"], raw, "bill")
            values["model_key"] = model_key(raw.get("model"))
            values["bill_cost_vnd"] = raw.get("bill_cost_vnd")
            values["match_state"] = "bill_only"
            values["bill_request_id"] = raw["bill_request_id"]
            values["tok_total"] = raw.get("tok_total") or values["tok_total"]
            conn.execute(_INSERT_SQL, values)
            touched.add(values["day"])
            added += 1
        if touched:
            touch_days(conn, touched)
    return added


def _claimable(conn: sqlite3.Connection, since: float) -> dict[str, tuple[list, list]]:
    """Unclaimed local calls and unclaimed bills, bucketed per model key and sorted by time."""
    buckets: dict[str, dict[str, list]] = {}
    for row in conn.execute(
            "SELECT rowid AS rid, * FROM calls WHERE match_state IN ('local_only','bill_only') AND ts >= ? "
            "ORDER BY model_key, ts", (since,)):
        bucket = buckets.setdefault(row["model_key"], {"local": [], "bill": []})
        bucket["local" if row["match_state"] == "local_only" else "bill"].append(row)
    return buckets


def _drop_replayed(conn: sqlite3.Connection, since: float) -> int:
    """Fold bill rows that re-arrive for an already-paired call back into that call, then drop them.

    Pairing folds a charge into the call row. Rows paired before the call row kept the bill's id have
    no id to be recognised by, so the next walk of the same page re-inserts the charge and the call
    bills twice. Cost is precise to the micro-dong and the pairing window is 90 s, so an identical
    (model, cost, instant) triple is the same charge. Zero-cost duplicates are left alone: they cannot
    distort a sum, and two genuinely free calls in the same minute are plausible.

    The replay is also the only surviving copy of that call's API key when the pairing predates key
    tracking, so its ``bill_key_id`` is handed over before the row goes.
    """
    conn.execute(
        """UPDATE calls SET bill_key_id = (
               SELECT b.bill_key_id FROM calls b
               WHERE b.match_state = 'bill_only' AND b.bill_key_id IS NOT NULL
                 AND b.model_key = calls.model_key
                 AND b.bill_cost_vnd = calls.bill_cost_vnd
                 AND abs(b.ts - calls.ts) <= ?
               LIMIT 1)
           WHERE match_state = 'matched' AND bill_key_id IS NULL AND ts >= ?""",
        (MATCH_WINDOW_S, since))
    cur = conn.execute(
        """DELETE FROM calls WHERE match_state = 'bill_only' AND bill_cost_vnd > 0 AND ts >= ?
           AND EXISTS (SELECT 1 FROM calls m WHERE m.match_state = 'matched'
                       AND m.model_key = calls.model_key
                       AND m.bill_cost_vnd = calls.bill_cost_vnd
                       AND abs(m.ts - calls.ts) <= ?)""", (since, MATCH_WINDOW_S))
    return cur.rowcount if cur.rowcount > 0 else 0


def match_rows(since: Optional[float] = None) -> int:
    """Pair unmatched local calls with unmatched bills inside the pairing window.

    Candidates are generated per model key with a moving time window, then claimed by ascending gap so
    the closest pair wins -- first-come-first-served would let a call 80 s away take a bill that a call
    2 s away was waiting for.

    The default window is ``MATCH_LOOKBACK``, not the whole retention: only rows recorded while the
    hook was live can pair, and rescanning months of pre-install bills on every sync is quadratic
    cost for zero matches. Pass an explicit ``since`` to widen it.
    """
    conn = _connect(store_path())
    window_start = since if since is not None else time.time() - MATCH_LOOKBACK
    pairs: list[tuple[float, int, int]] = []
    with _tx(conn):
        buckets = _claimable(conn, window_start)
        by_rid = {b["rid"]: b for group in buckets.values() for b in group["bill"]}
        for group in buckets.values():
            locals_, bills = group["local"], group["bill"]
            left = 0
            for bill in bills:
                b_ts = float(bill["ts"])
                while left < len(locals_) and float(locals_[left]["ts"]) < b_ts - MATCH_WINDOW_S:
                    left += 1
                for local in locals_[left:]:
                    if float(local["ts"]) > b_ts + MATCH_WINDOW_S:
                        break
                    if _tokens_agree(local, bill):
                        pairs.append((abs(float(local["ts"]) - b_ts), local["rid"], bill["rid"]))
        pairs.sort()
        claimed_local: set[int] = set()
        claimed_bill: set[int] = set()
        matched = 0
        for _gap, local_rid, bill_rid in pairs:
            if local_rid in claimed_local or bill_rid in claimed_bill:
                continue
            claimed_local.add(local_rid)
            claimed_bill.add(bill_rid)
            bill = by_rid[bill_rid]
            # Delete first, then claim the id: copying the bill id onto the call row is what stops a
            # later re-sync from re-ingesting a paired charge as a fresh bill_only row (that doubled
            # every matched call's cost), but the unique index will not tolerate the two rows holding
            # it at the same moment.
            conn.execute("DELETE FROM calls WHERE rowid=?", (bill_rid,))
            conn.execute(
                "UPDATE calls SET match_state='matched', bill_cost_vnd=?, "
                "  bill_key_id=COALESCE(bill_key_id, ?), bill_request_id=?, "
                "  tok_total=COALESCE(tok_total, ?) WHERE rowid=?",
                (bill["bill_cost_vnd"], bill["bill_key_id"], bill["bill_request_id"],
                 bill["tok_total"], local_rid))
            matched += 1
        _drop_replayed(conn, window_start)
        # Every row pairing can have deleted or re-sourced lies inside the window it worked on, and the
        # distinct-day list for a window is a handful of index lookups. Listing the days rather than
        # threading them through the loop is the difference between "correct by construction" and
        # "correct because no future edit forgets one".
        touch_days(conn, [r[0] for r in conn.execute(
            "SELECT DISTINCT day FROM calls WHERE ts >= ?", (window_start,))])
    return matched


def _tokens_agree(local: sqlite3.Row, bill: sqlite3.Row) -> bool:
    """Reject a pairing whose token mass disagrees beyond the tolerance, comparing each side's own units.

    Hermes reports a call as ``input + output``; vilao bills ``input + output + cache read`` (verified on
    the live store: a 3,019/126/107,520-token call arrives locally as 3,145 and on the bill as 110,665).
    Comparing the raw totals therefore rejected 377 of the 386 unpaired rows on this account -- nearly
    every cached call, which are exactly the expensive ones -- so the local side is rebuilt in the
    billing shape before it is compared.
    """
    billed = (int(local["tok_in"] or 0) + int(local["tok_out"] or 0)
              + int(local["tok_cache_read"] or 0)) or local["tok_total"]
    a, b = billed, bill["tok_total"]
    if not a or not b:
        return True
    return abs(float(a) - float(b)) / max(float(a), float(b)) <= TOKEN_TOLERANCE


# The filter value the page uses for "billing rows, profile unknown". A real home can never be named
# "-": profile labels come from a path basename, which cannot be a single dash.
NO_PROFILE = "-"


def _profile_clause(profile: Optional[str]) -> tuple[str, list[Any]]:
    """SQL fragment for a profile filter, with room for the unattributed bucket.

    Billing rows carry no profile, because a charge belongs to the account and to the API key it was
    billed to -- not to whichever home happened to run the ingest. They still have to be reachable from
    the filter, so ``'-'`` selects them instead of selecting nothing.
    """
    if not profile:
        return "", []
    if profile == NO_PROFILE:
        return " AND profile=''", []
    return " AND profile=?", [profile]


def _filter_sql(*, provider: Optional[str] = None, model: Optional[str] = None,
                since: Optional[float] = None, main_only: bool = False,
                key_id: Optional[str] = None, billed_only: bool = False,
                outcome: Optional[str] = None, min_dong: Optional[float] = None,
                profile: Optional[str] = None, source: Optional[str] = None,
                day: Optional[str] = None) -> tuple[str, list[Any]]:
    """The one WHERE clause the row page and its count share, so a pager can never disagree with a list."""
    sql, args = " WHERE 1=1", []
    clause, extra = _profile_clause(profile)
    sql += clause
    args += extra
    if source:
        sql += " AND source=?"
        args.append(source)
    if day:
        # Clicking a square in the year grid asks for that calendar day; the bucket is stored, so this
        # is an index seek rather than the date(ts,...) scan that used to cost 75 ms per call.
        sql += " AND day=?"
        args.append(day)
    if provider:
        sql += " AND provider=?"
        args.append(provider)
    if model:
        sql += " AND model_key=?"
        args.append(model_key(model))
    if since:
        sql += " AND ts >= ?"
        args.append(float(since))
    if main_only:
        sql += " AND task IS NULL"
    if key_id:
        sql += " AND bill_key_id=?"
        args.append(key_id)
    if billed_only:
        sql += " AND bill_cost_vnd IS NOT NULL"
    if min_dong:
        sql += " AND COALESCE(bill_cost_vnd, 0) >= ?"
        args.append(float(min_dong))
    # vilao marks a failed call with success=false, which to_row folds into finish_reason.
    if outcome == "error":
        sql += " AND finish_reason='error'"
    elif outcome == "ok":
        sql += " AND COALESCE(finish_reason, '') <> 'error'"
    return sql, args


def recent(limit: int = 200, offset: int = 0, provider: Optional[str] = None,
           model: Optional[str] = None, since: Optional[float] = None,
           main_only: bool = False, key_id: Optional[str] = None,
           billed_only: bool = False, outcome: Optional[str] = None,
           min_dong: Optional[float] = None, profile: Optional[str] = None,
           source: Optional[str] = None, day: Optional[str] = None) -> list[dict[str, Any]]:
    sql, args = _filter_sql(provider=provider, model=model, since=since, main_only=main_only,
                            key_id=key_id, billed_only=billed_only, outcome=outcome,
                            min_dong=min_dong, profile=profile, source=source, day=day)
    sql = "SELECT * FROM calls" + sql + " ORDER BY ts DESC LIMIT ? OFFSET ?"
    args = args + [max(1, min(int(limit), 500)), max(0, int(offset))]
    return [dict(r) for r in _connect(store_path()).execute(sql, args)]


def recent_count(provider: Optional[str] = None, model: Optional[str] = None,
                 since: Optional[float] = None, main_only: bool = False,
                 key_id: Optional[str] = None, billed_only: bool = False,
                 outcome: Optional[str] = None, min_dong: Optional[float] = None,
                 profile: Optional[str] = None, source: Optional[str] = None,
                 day: Optional[str] = None) -> int:
    """How many rows the same filter matches -- what a pager needs to know it can go further."""
    sql, args = _filter_sql(provider=provider, model=model, since=since, main_only=main_only,
                            key_id=key_id, billed_only=billed_only, outcome=outcome,
                            min_dong=min_dong, profile=profile, source=source, day=day)
    row = _connect(store_path()).execute("SELECT COUNT(*) FROM calls" + sql, args).fetchone()
    return int(row[0] or 0)


def dong_since(seconds: float) -> float:
    """Real dong billed over the last ``seconds`` -- the window the Wallet tab headlines."""
    row = _connect(store_path()).execute(
        "SELECT COALESCE(SUM(bill_cost_vnd), 0) AS d FROM calls WHERE ts >= ?",
        (time.time() - max(1.0, float(seconds)),)).fetchone()
    return round(float(row["d"] or 0), 4)


def dong_by_key_since(seconds: float) -> dict[str, float]:
    """The same window split by API key; rows whose key is unknown are left out rather than guessed."""
    rows = _connect(store_path()).execute(
        "SELECT bill_key_id AS k, COALESCE(SUM(bill_cost_vnd), 0) AS d FROM calls "
        "WHERE ts >= ? AND bill_key_id IS NOT NULL GROUP BY k",
        (time.time() - max(1.0, float(seconds)),)).fetchall()
    return {r["k"]: round(float(r["d"] or 0), 4) for r in rows}



def _day_start(days: int) -> str:
    """The first day a ``days``-wide window includes, as a ``day`` string."""
    return day_of(time.time() - max(1, min(int(days), RETENTION_DAYS)) * 86400)


def matrix(days: int = 30, profile: Optional[str] = None) -> list[dict[str, Any]]:
    """Day x model totals, read from the rollup instead of the 42k-row call table.

    The old version grouped ``calls`` on every request -- 75 ms measured -- which is why a memo had to be
    bolted over it. day_model_stats holds one row per (day, home, model), so this reads a few hundred.
    """
    clause, extra = _profile_clause(profile)
    sql = """
        SELECT day, model_key, SUM(calls) AS calls, SUM(tokens) AS tokens,
               SUM(dong) AS dong, SUM(usd) AS usd
        FROM day_model_stats WHERE day >= ?""" + clause + \
        " GROUP BY day, model_key ORDER BY day, dong DESC"
    rows = _connect(store_path()).execute(sql, [_day_start(days)] + extra).fetchall()
    return [dict(r) for r in rows]


def daily(days: int = 30, profile: Optional[str] = None,
          source: Optional[str] = None) -> list[dict[str, Any]]:
    """Per-day totals, optionally restricted to one feed (the Hermes log or the vilao bill).

    ``avg_ms`` is stored sum over stored count, never an average of averages -- weighting a 3-call day
    like a 900-call one is the mistake a rollup makes easy and a reader makes invisible.
    """
    clause, extra = _profile_clause(profile)
    if source:
        clause += " AND source=?"
        extra.append(source)
    sql = """
        SELECT day, SUM(calls) AS calls, SUM(tokens) AS tokens, SUM(dong) AS dong, SUM(usd) AS usd,
               CASE WHEN SUM(ms_n) > 0 THEN SUM(ms_sum) / SUM(ms_n) ELSE NULL END AS avg_ms,
               SUM(tok_cache) AS tok_cache_read, SUM(tok_in) AS tok_in, SUM(tok_out) AS tok_out,
               SUM(tok_reasoning) AS tok_reasoning
        FROM day_stats WHERE day >= ?""" + clause + " GROUP BY day ORDER BY day"
    rows = _connect(store_path()).execute(sql, [_day_start(days)] + extra).fetchall()
    return [dict(r) for r in rows]



def match_states() -> dict[str, int]:
    """How many rows sit in each pairing state -- the store's own honesty report.

    A page that shows dong totals without this cannot say how much of the bill it has actually managed to
    attach to a call of its own. Counted per day into the rollup, because GROUP BY over 42k rows on every
    /summary was the last read that still walked the whole call table.
    """
    rows = _connect(store_path()).execute(
        "SELECT SUM(matched), SUM(local_only), SUM(bill_only) FROM day_stats").fetchone()
    return {"matched": int(rows[0] or 0), "local_only": int(rows[1] or 0),
            "bill_only": int(rows[2] or 0)}


ROLLUP_COLUMNS = {
    "day_stats": ("matched", "local_only", "bill_only"),
    "day_model_stats": (),
}


def _rollup_shape(conn: sqlite3.Connection) -> bool:
    """Is the model rollup already shaped like the current _SCHEMA?

    A missing ``source`` column cannot be ALTERed into usefulness: the primary key would still be
    (day, profile, model_key), so a model billed on both feeds would collide on one row. The rollups are
    derived data, so the old-shaped table is dropped and rebuilt instead of carried forward.
    """
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='day_model_stats'").fetchone():
        return True
    return "source" in {row[1] for row in conn.execute("PRAGMA table_info(day_model_stats)")}


def _drop_rollups(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS day_model_stats")
    conn.execute("DROP TABLE IF EXISTS day_stats")


def _ensure_rollup_columns(conn: sqlite3.Connection) -> None:
    """Add counters to a rollup table an earlier build of this feature already created.

    Same failure mode as an index over a column _ensure_columns has not added yet: CREATE TABLE IF NOT
    EXISTS no-ops on the old shape, and the new INSERT then names columns that are not there.
    """
    for table, columns in ROLLUP_COLUMNS.items():
        if not columns:
            continue
        have = {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}
        if not have:
            continue
        for column in columns:
            if column not in have:
                conn.execute("ALTER TABLE %s ADD COLUMN %s INTEGER NOT NULL DEFAULT 0"
                             % (table, column))


CATALOG_RETENTION_DAYS = 120


def _net_price(row: dict[str, Any], base: str, promo: Optional[str] = None) -> Optional[float]:
    """The price the invoice will use: the promo figure while a promo is on, the list price otherwise."""
    off = float(row.get("promo_off") or 0)
    if off > 0 and promo:
        value = row.get(promo)
        if value is not None:
            return float(value)
    value = row.get(base)
    return None if value is None else float(value)


def record_catalog(rows: list[dict[str, Any]]) -> int:
    """Store today's snapshot of the catalogue prices the page just read.

    Called from the read path on purpose: the catalogue is fetched at most four times a day and the only
    moment its prices are known is the moment they arrive. Writing here is what lets the page answer
    "did this get cheaper" instead of only "what does it cost right now".
    """
    if not rows:
        return 0
    day = day_of(time.time())
    conn = _connect(store_path())
    stored = 0
    with _tx(conn):
        for r in rows:
            key = r.get("key")
            if not key:
                continue
            context = r.get("context")
            conn.execute(
                "INSERT INTO catalog_prices (day, key, model_key, provider, price_in, price_out,"
                " cache_read, context, floor) VALUES (?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(day, key) DO UPDATE SET model_key=excluded.model_key,"
                " provider=excluded.provider, price_in=excluded.price_in,"
                " price_out=excluded.price_out, cache_read=excluded.cache_read,"
                " context=excluded.context, floor=excluded.floor",
                (day, str(key), model_key(r.get("id")), str(r.get("provider") or ""),
                 _net_price(r, "in", "promo_in"), _net_price(r, "out", "promo_out"),
                 _net_price(r, "cache_read"), None if context is None else int(context),
                 None if r.get("floor") is None else float(r["floor"])))
            stored += 1
    return stored


def prune_catalog(conn: sqlite3.Connection) -> int:
    """Drop price history older than the watch needs. The call rows keep their own retention."""
    cutoff = time.strftime("%Y-%m-%d", time.localtime(
        time.time() - CATALOG_RETENTION_DAYS * 86400))
    cur = conn.execute("DELETE FROM catalog_prices WHERE day < ?", (cutoff,))
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def catalog_changes() -> dict[str, Any]:
    """What moved between the two most recent catalogue snapshots.

    A field-by-field diff of the same offer key, not a fuzzy match on the model name: the same model is
    sold by many providers and each offer is priced on its own. An offer that appears or disappears is
    reported as such -- a delisted offer is the more expensive answer of the two.
    """
    conn = _connect(store_path())
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT day FROM catalog_prices ORDER BY day DESC LIMIT 2")]
    out = {"days": days, "rows": [], "count": 0}
    if len(days) < 2:
        return out
    newer, older = days[0], days[1]
    prev = {r["key"]: r for r in conn.execute(
        "SELECT key, model_key, provider, price_in, price_out, cache_read, floor FROM catalog_prices"
        " WHERE day=?", (older,))}
    seen = set()
    fields = (("price_in", "vào"), ("price_out", "ra"), ("cache_read", "cache"))
    for r in conn.execute(
            "SELECT key, model_key, provider, price_in, price_out, cache_read, floor FROM catalog_prices"
            " WHERE day=?", (newer,)):
        seen.add(r["key"])
        before = prev.get(r["key"])
        if before is None:
            out["rows"].append({"key": r["key"], "model_key": r["model_key"],
                                 "provider": r["provider"], "kind": "new", "field": "",
                                 "from": None, "to": None, "pct": None})
            continue
        for name, label in fields:
            a, b = before[name], r[name]
            if a is None or b is None or abs(float(a) - float(b)) < 1e-12:
                continue
            out["rows"].append({"key": r["key"], "model_key": r["model_key"],
                                "provider": r["provider"], "kind": "price", "field": label,
                                "from": float(a), "to": float(b),
                                "pct": (float(b) - float(a)) / float(a) * 100.0 if a else None})
        if (before["floor"]) is not None and (r["floor"]) is not None \
                and abs(float(before["floor"]) - float(r["floor"])) > 1e-12:
            a, b = float(before["floor"]), float(r["floor"])
            out["rows"].append({"key": r["key"], "model_key": r["model_key"],
                                "provider": r["provider"], "kind": "price", "field": "sàn",
                                "from": a, "to": b, "pct": (b - a) / a * 100.0 if a else None})
    for key, before in prev.items():
        if key not in seen:
            out["rows"].append({"key": key, "model_key": before["model_key"],
                                "provider": before["provider"], "kind": "gone", "field": "",
                                "from": before["price_in"], "to": None, "pct": None})
    out["count"] = len(out["rows"])
    return out


def summary(days: int = 30, profile: Optional[str] = None) -> dict[str, Any]:
    span = max(1, min(int(days), RETENTION_DAYS))
    rows = daily(span, profile)
    dong = sum(float(r["dong"] or 0) for r in rows)
    usd = sum(float(r["usd"] or 0) for r in rows)
    states = match_states()
    tokens = {k: round(sum(float(r[k] or 0) for r in rows))
              for k in ("tokens", "tok_in", "tok_out", "tok_cache_read", "tok_reasoning")}
    recent_days = [r for r in rows if float(r["dong"] or 0) > 0][-7:]
    burn = (sum(float(r["dong"]) for r in recent_days) / len(recent_days)) if recent_days else 0.0
    return {
        "days": span,
        "calls": sum(int(r["calls"]) for r in rows),
        "dong": round(dong, 2),
        "usd_estimated": round(usd, 6),
        "dong_per_day": round(burn, 2),
        "tokens": int(tokens["tokens"]), "token_parts": tokens,
        "days_by_date": rows,
        "counts": states,
    }



def bill_span() -> dict[str, Any]:
    """What the store actually holds of the account's billing history.

    The calendar has to say which months it can fill: a blank square means either the account did not
    spend that day or the walk never reached it, and only this span tells the two apart. Days come from
    the rollup, where counting them is an index walk; the endpoints come from calls, where MIN/MAX ts are
    answered straight from the index.
    """
    conn = _connect(store_path())
    edges = conn.execute(
        "SELECT MIN(ts), MAX(ts), COUNT(*) FROM calls WHERE bill_cost_vnd IS NOT NULL").fetchone()
    days = conn.execute("SELECT COUNT(DISTINCT day) FROM day_stats WHERE dong > 0").fetchone()[0]
    return {"first_ts": edges[0], "last_ts": edges[1],
            "rows": int(edges[2] or 0), "days": int(days or 0)}



def rate_window(hours: float = 24.0, profile: Optional[str] = None,
              provider: Optional[str] = None) -> dict[str, Any]:
    """Self-measured request rate: per-minute buckets over a window, plus the peaks.

    NVIDIA, OpenCode Zen and Kilo publish request limits but expose no usage API and send no
    ``x-ratelimit-*`` headers, so the call log is the only signal that exists. Peaks are computed from
    the buckets rather than from a single clock minute: a free tier's limit is a sliding window, and the
    worst rolling hour is what actually predicts a 429.
    """
    span = max(60.0, float(hours) * 3600.0)
    since = time.time() - span
    sql = ("SELECT CAST(ts / 60 AS INTEGER) * 60 AS minute, COUNT(*) AS calls, "
           "SUM(COALESCE(tok_total, 0)) AS tokens, SUM(COALESCE(bill_cost_vnd, 0)) AS dong "
           "FROM calls WHERE ts >= ?")
    args: list[Any] = [since]
    clause, extra = _profile_clause(profile)
    sql += clause
    args += extra
    if provider:
        sql += " AND provider=?"
        args.append(provider)
    sql += " GROUP BY minute ORDER BY minute"
    rows = [dict(r) for r in _connect(store_path()).execute(sql, args)]
    counts = [int(r["calls"] or 0) for r in rows]
    peak_rpm = max(counts) if counts else 0
    peak_rph = 0
    for start in range(0, max(1, len(counts) - 59)):
        peak_rph = max(peak_rph, sum(counts[start:start + 60]))
    now_minute = int(time.time() // 60)
    return {
        "hours": span / 3600.0,
        "minutes": [{"minute": int(r["minute"]), "calls": int(r["calls"] or 0),
                     "tokens": int(r["tokens"] or 0), "dong": round(float(r["dong"] or 0), 4)}
                    for r in rows],
        "rpm_now": counts[-1] if rows and int(rows[-1]["minute"]) == now_minute else 0,
        "peak_rpm": peak_rpm,
        "peak_rph": peak_rph,
        "calls": sum(counts),
        "tokens": sum(int(r["tokens"] or 0) for r in rows),
        "dong": round(sum(float(r["dong"] or 0) for r in rows), 4),
    }


def rate_by_provider(hours: float = 24.0) -> list[dict[str, Any]]:
    """Per-provider request rate, because a limit belongs to one provider and not to the account.

    A single global peak would read as 253 req/h against Kilo's published 200 and be wrong: that hour is
    the sum of everything routed at once. Each provider gets its own buckets, and the caller is left to
    say which limits are published fact and which are community folklore.
    """
    out = []
    for row in _connect(store_path()).execute(
            "SELECT provider, COUNT(*) AS calls, SUM(COALESCE(tok_total,0)) AS tokens, "
            "SUM(COALESCE(bill_cost_vnd,0)) AS dong, MAX(ts) AS last_ts, "
            "SUM(CASE WHEN finish_reason='error' THEN 1 ELSE 0 END) AS errors "
            "FROM calls WHERE ts >= ? GROUP BY provider ORDER BY calls DESC",
            (time.time() - max(60.0, float(hours) * 3600.0),)):
        d = dict(row)
        one = rate_window(hours, provider=d.get("provider"))
        d.update(peak_rpm=one["peak_rpm"], peak_rph=one["peak_rph"], rpm_now=one["rpm_now"])
        out.append(d)
    return out



def usage_by_model(models: Optional[list[str]] = None, days: int = 400,
                   profile: Optional[str] = None, source: Optional[str] = None) -> list[dict[str, Any]]:
    """Per-model totals over the window, summed from the day x model rollup.

    The window defaults to the whole retention: "what have I ever spent through this model" is the
    question, and a 30-day default answers it wrongly for anything last used in August.

    ``tok_billed`` is in + out + cache read, the shape the invoice divides by. Hermes reports a call as
    input+output only, so a đ/1M derived from that overstates the price by exactly the cached share --
    measured as 3,145 tokens locally against 110,665 billed on one call.
    """
    clause, extra = _profile_clause(profile)
    if source:
        clause += " AND source=?"
        extra.append(source)
    span = max(1, min(int(days), RETENTION_DAYS))
    args: list[Any] = [day_of(time.time() - span * 86400)] + extra
    if models:
        clause += " AND model_key IN (%s)" % ",".join("?" * len(models))
        args.extend(model_key(m) for m in models)
    sql = """
        SELECT model_key, GROUP_CONCAT(DISTINCT providers) AS providers,
               GROUP_CONCAT(DISTINCT NULLIF(profile, '')) AS profiles,
               SUM(calls) AS calls, SUM(tokens) AS tokens,
               SUM(tok_in) AS tok_in, SUM(tok_out) AS tok_out, SUM(tok_cache) AS tok_cache,
               SUM(tok_in) + SUM(tok_out) + SUM(tok_cache) AS tok_billed,
               SUM(dong) AS dong, SUM(usd) AS usd,
               CASE WHEN SUM(ms_n) > 0 THEN SUM(ms_sum) / SUM(ms_n) ELSE NULL END AS avg_ms,
               MIN(first_ts) AS first_ts, MAX(last_ts) AS last_ts,
               COUNT(DISTINCT day) AS days, SUM(errors) AS errors
        FROM day_model_stats WHERE day >= ?""" + clause + \
        " GROUP BY model_key ORDER BY dong DESC, calls DESC"
    rows = [dict(r) for r in _connect(store_path()).execute(sql, args)]
    # providers is a comma list per (day, feed) row, and the outer DISTINCT cannot see inside it, so a
    # model billed through two feeds arrives as "dogi,dogi". Splitting and rejoining is what makes the
    # column honest -- the page prints it as the set of sellers seen.
    for r in rows:
        for col in ("providers", "profiles"):
            r[col] = ",".join(sorted({p for p in str(r[col] or "").split(",") if p}))
    return rows



def sessions(days: int = 30, profile: Optional[str] = None, limit: int = 60) -> list[dict[str, Any]]:
    """Per-session rollup of the Hermes log: what one conversation actually cost.

    The per-call log answers "which call was expensive" and the day aggregates answer "which day", but the
    question a user asks when the wallet drops is usually "what did that session do" -- how many calls,
    which models, how much of it cache reads. Only hook rows carry a session id, so the bill feed is
    excluded by construction rather than by filter.
    """
    span = max(1, min(int(days), RETENTION_DAYS))
    clause, extra = _profile_clause(profile)
    sql = """
        SELECT session_id,
               MIN(profile) AS profile, MIN(platform) AS platform,
               COUNT(*) AS calls, COUNT(DISTINCT model_key) AS models,
               GROUP_CONCAT(DISTINCT provider) AS providers,
               SUM(COALESCE(tok_total, 0)) AS tokens,
               SUM(COALESCE(tok_cache_read, 0)) AS tok_cache,
               SUM(COALESCE(bill_cost_vnd, 0)) AS dong,
               SUM(COALESCE(est_cost_usd, 0)) AS usd,
               SUM(COALESCE(duration_ms, 0)) AS ms,
               MIN(ts) AS first_ts, MAX(ts) AS last_ts,
               SUM(CASE WHEN finish_reason = 'error' THEN 1 ELSE 0 END) AS errors
        FROM calls WHERE source = 'hook' AND session_id IS NOT NULL AND ts >= ?""" + clause + \
        " GROUP BY session_id ORDER BY dong DESC, last_ts DESC LIMIT ?"
    args: list[Any] = [time.time() - span * 86400] + extra + [max(1, min(int(limit), 500))]
    return [dict(r) for r in _connect(store_path()).execute(sql, args)]



def profiles_seen() -> list[dict[str, Any]]:
    """Which homes the shared store holds calls for, most recent first.

    Sharing one file is only useful if the page can say what it covers: a profile whose plugin is not
    enabled simply does not appear, which is the honest signal, where a zero would read as "unused".
    The empty-named bucket is the vilao billing feed, which belongs to the account, not to a home.
    """
    sql = """
        SELECT profile, SUM(calls) AS calls, MAX(last_ts) AS last_ts,
               SUM(dong) AS dong, SUM(tokens) AS tokens,
               SUM(CASE WHEN source = 'hook' THEN calls ELSE 0 END) AS local_rows,
               SUM(CASE WHEN source = 'bill' THEN calls ELSE 0 END) AS bill_rows
        FROM day_stats GROUP BY profile ORDER BY last_ts DESC"""
    rows = _connect(store_path()).execute(sql).fetchall()
    return [dict(r) for r in rows]



def stats() -> dict[str, Any]:
    conn = _connect(store_path())
    row = conn.execute(
        "SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM calls").fetchone()
    return {"rows": int(row["n"] or 0), "first_ts": row["first_ts"], "last_ts": row["last_ts"]}


def get_meta(key: str, default: Optional[str] = None) -> Optional[str]:
    """Small durable scratch (the bill cursor) so a backfill resumes across host restarts."""
    row = _connect(store_path()).execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str) -> None:
    conn = _connect(store_path())
    with _tx(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))


def claim_slot(key: str, cooldown_s: float, now: Optional[float] = None) -> bool:
    """Take a recurring-job slot that every process sharing this store competes for.

    The claim is a single UPDATE whose WHERE clause *is* the cooldown, because the five profile gateways
    and the dashboard host all write this one file: a read-then-write pair would have all six decide the
    last run was stale, and then all six go and pull the same bill.
    """
    stamp = time.time() if now is None else float(now)
    conn = _connect(store_path())
    with _tx(conn):
        conn.execute("INSERT INTO meta (key, value) VALUES (?, '0') ON CONFLICT(key) DO NOTHING", (key,))
        cur = conn.execute("UPDATE meta SET value=? WHERE key=? AND CAST(value AS REAL) <= ?",
                           (stamp, key, stamp - max(0.0, float(cooldown_s))))
        return cur.rowcount == 1
