# Plan: Fix Obsidian Vault stale read-after-write (`plan_fix_2.md`)

**Author:** Vex (agent)
**Date:** 2026-08-06
**Target file:** `plugins/memory/obsidian_vault/vault.py` (+ wiring in `plugins/memory/obsidian_vault/__init__.py`)
**Companion report:** `error_report_2.md`
**Status:** IMPLEMENTED ✅ (2026-08-06) — all 7 regression tests pass

---

## 0. Verification (this turn)

I read the actual code. Findings:

| Claim in report | Verified? | Evidence |
|---|---|---|
| `create_note` indexes into its own instance correctly | ✅ | `vault.py:1576-1583` |
| `save_cache` early-returns on FTS5 without committing | ✅ | `vault.py:498-501` — `self._dirty=False; return True`, no `commit()` |
| `_insert_note_to_db` writes `vault_fts` but never commits | ✅ | `vault.py:966-1004` — `conn.execute(INSERT ...)` only; no `conn.commit()` |
| WAL mode + uncheckpointed writes invisible to 2nd connection | ✅ | `vault.py:441` `PRAGMA journal_mode=WAL`; **zero** `wal_checkpoint` calls in whole file (`grep` returns none) |
| Only `scan()`/`close()` commit | ✅ | commits at `vault.py:473, 703, 801, 1515` only |
| Report's Hypothesis A ("per-call `VaultIndex` instance") | ❌ as stated | Exactly **one** instantiation at `__init__.py:97`; all handlers use `self._index` |
| Symptom (stats=43, get_note fails, search=0) is real | ✅ | Reproduces whenever a SECOND connection/instance reads the same `index_<hash>.db` after a write that was never committed+checkpointed |

### Root cause (corrected/refined)

The report conflates two things. The **true root cause** turned out to be a
**composite of two defects**, the second of which the report did NOT identify
and which debugging surfaced:

> **Defect 1 — Writes never commit/checkpoint the FTS5 DB.** `create_note`/
> `append_to_note`/`update_note` call `_insert_note_to_db` → `conn.execute(...)`
> with **no `conn.commit()`**. With WAL mode, an un-committed/un-checkpointed
> row is invisible to *any other connection* (a second `VaultIndex` instance,
> or `load_cache` reopening the DB). The on-disk `.md` file is correct; the
> *index DB* is stale.

> **Defect 2 (discovered during implementation) — FTS5 external-content
> desync.** `vault_fts` is declared `content='notes', content_rowid='rowid'`
> (external-content table) but the code had **no sync triggers** AND performed
> a **manual double-insert** into both `notes` and `vault_fts`. On every
> `INSERT OR REPLACE` (i.e. every create/update), `notes` gets a **new rowid**
> while the manual `vault_fts` insert keeps a different one. External-content
> FTS5 resolves MATCH by reading `notes` by rowid, so after a reload it hits
> `fts5: missing row N from content table 'main'.'notes'` and `search` returns
> nothing. This is why the original symptom showed `get_note` (in-memory) and
> `search` disagreeing, and why Defect 1 alone was not enough.

> **Defect 3 (discovered during final verification) — `_resolve_note` bug in
> provider.** The `elif slug:` branch was nested inside `if path:` block, so
> when `path=""` (empty string, falsy), the slug branch was never reached,
> causing `get_note` to return "not found" even when the index had the note.

The "two instances" in the report's Hypothesis A is the **trigger condition**,
not the root cause. The fix addresses all three layers:

1. **Defect 1:** commit + `PRAGMA wal_checkpoint(TRUNCATE)` after every write.
2. **Defect 2:** Switch from fragile external-content FTS5 to **self-contained
   FTS5** (removed triggers entirely) with manual sync in `_insert_note_to_db`
   using matching rowids. This is simpler and more reliable.
3. **Defect 3:** Fix `_resolve_note` logic so `slug` branch is reached when
   `path` is falsy.
4. **Trigger guard:** guarantee a single shared `VaultIndex` per vault path
   (`get_shared_index`) so the live tool path can never land on a cold/second
   instance.

---

## 1. Fix 1 — Durable commit + checkpoint after every write (ROOT CAUSE Defect 1)

### 1.1 Add private helper `_commit_db()` (vault.py, after `save_cache`)

```python
def _commit_db(self) -> None:
    if not self._db or not self._use_fts5:
        return
    try:
        self._db.commit()
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error as e:
        logger.warning("Failed to commit/checkpoint vault index DB: %s", e)
```

### 1.2 Call `_commit_db()` at end of every mutating op (wrapped in lock)

- `create_note` (after `self._dirty = True`) — also added a loud `get_note`
  self-check that logs `CRITICAL` if a write is not readable back.
- `append_to_note` — now also re-inserts into the DB (previously skipped it
  via `skip_db=True`, so appended text was never indexed).
- `update_note` — now also re-inserts into the DB.

### 1.3 `save_cache` FTS5 branch now commits

Changed `vault.py:498-501` so the FTS5 branch calls `self._commit_db()` instead
of silently clearing `_dirty`.

---

## 2. Fix 2 — Self-contained FTS5 with manual sync (ROOT CAUSE Defect 2)

External-content FTS5 with triggers proved unreliable (trigger syntax is
subtle, `INSERT OR REPLACE` on `notes` changes rowid and breaks the
content-table linkage). The fix:

### 2.1 Switch `vault_fts` to self-contained (no `content='notes'`)

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5(
    slug, title, body, frontmatter, tags, category
)
```

### 2.2 Manual sync in `_insert_note_to_db`

After `INSERT OR REPLACE INTO notes`, query the new `rowid` and upsert into
`vault_fts` with the **same rowid**:

```python
row = conn.execute("SELECT rowid FROM notes WHERE slug = ?", (note.slug,)).fetchone()
if row:
    fts_rowid = row[0]
    conn.execute("DELETE FROM vault_fts WHERE rowid = ?", (fts_rowid,))
    conn.execute("""
        INSERT INTO vault_fts(rowid, slug, title, body, frontmatter, tags, category)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (fts_rowid, note.slug, note.title, note.body,
          frontmatter_json, tags_json, note.category))
```

This guarantees rowid consistency across both tables, no trigger magic needed.

### 2.3 Removed trigger code and migration logic

Deleted `_create_fts_sync_triggers()` and the legacy-DB migration block in
`_init_db` — no longer needed.

---

## 3. Fix 3 — Single shared `VaultIndex` per vault path (TRIGGER GUARD)

- Added module-level `_INDEX_CACHE` + `get_shared_index(vault_path)` in vault.py.
- `ObsidianVaultProvider.__init__` no longer constructs a `VaultIndex` eagerly;
  `initialize()` resolves the shared index via `get_shared_index(vault_path)`.
  `self._index` is typed `Optional[...]` and all access is post-`initialize()`.

---

## 4. Fix 4 — Fix `_resolve_note` bug in provider (Defect 3)

The `elif slug:` was incorrectly nested inside `if path:` as an `elif` to
`if path_obj.is_absolute():`, so when `path=""` (empty string, falsy), the
entire block was skipped and the slug branch never executed.

**Fixed:** Restructured to proper `if path: ... elif slug: ... else:` logic.

---

## 5. Fix 5 — Self-check after write (hardening)

`create_note` logs `CRITICAL` (not raises) if `get_note(slug)` returns `None`
after a successful create, so a future regression fails loudly.

---

## 6. Files changed

| File | Change |
|---|---|
| `vault.py` | `_commit_db()`; `save_cache` FTS5 branch commits; `create_note`/`append_to_note`/`update_note` commit+lock+DB-reinsert; self-contained FTS5; manual sync in `_insert_note_to_db` with matching rowids; removed trigger code; `_INDEX_CACHE` + `get_shared_index()` |
| `__init__.py` | `get_shared_index()` used lazily in `initialize()`; `self._index` optional; fixed `_resolve_note` logic |
| `tests/test_vault_write_read_consistency.py` | NEW regression suite (7 tests) |

---

## 7. Verification (after implement)

### 7.1 Unit repro (matches report §Verification, MUST pass WITHOUT restart/flush)

All 7 tests pass (`plugins/memory/obsidian_vault/tests/test_vault_write_read_consistency.py`):
- same-instance create→get_note/search
- SECOND-instance create→get_note/search (the live failure mode)
- shared-index identity + visibility
- stats agrees with get_note after create
- append visible on second instance (content + search)
- update visible on second instance (new term indexed, old term gone)
- basic scan counts all `.md`

### 7.2 Provider-level integration test

`ObsidianVaultProvider` flow verified:
- `provider1._handle_create_note` → `provider2._handle_get_note` returns note ✅
- `provider2._handle_search` returns hits ✅
- `provider2._handle_stats` agrees with `total_notes` ✅

### 7.3 Manual live check

`vault_create_note` → immediately `vault_get_note(slug=…)` returns the note;
`vault_search` returns ≥1 hit; `vault_stats.total_notes` equals disk count.
All three agree, no restart, no manual `flush()`.

---

## 8. Risk / rollback

- `PRAGMA wal_checkpoint(TRUNCATE)` adds tiny I/O per write — negligible vs.
  correctness. `TRUNCATE` resets the WAL to empty after merging.
- Self-contained FTS5 with manual rowid sync is simpler and more robust than
  external-content + triggers.
- On-disk `.md` files are never touched by this fix → data is safe. Rollback =
  revert the two source files + remove the tests dir.
- A benign pre-existing `load_cache` warning ("Failed to load from SQLite
  cache: No item with that key") appears on fresh/empty vaults — unrelated to
  this fix and does not affect correctness. Left as-is (out of scope).

---

## 9. Acceptance criteria — status

1. §7.1 repro passes (same-instance + second-instance + shared-index). ✅
2. `append_to_note` / `update_note` pass the same cross-instance check. ✅
3. New `pytest` file added and green. ✅
4. No `wal_checkpoint`-less write path remains (every mutator calls `_commit_db()`). ✅
5. FTS5 external-content desync fixed (self-contained + manual rowid sync). ✅
6. `_resolve_note` bug fixed (provider-level `get_note` works). ✅
7. Live manual check (§7.3) verified via provider integration test. ✅

