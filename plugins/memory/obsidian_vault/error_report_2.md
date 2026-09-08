# Bug Report: Obsidian Vault search/get_note return stale results after create_note

**Reported by:** Vex (agent) — observed during a memory-governance task
**Date:** 2026-08-06
**Severity:** High (breaks write→read consistency; agent cannot verify its own writes)
**Component:** `plugins/memory/obsidian_vault/vault.py` (VaultIndex)

---

## Symptom

After `vault_create_note(...)` returns success, immediately calling `vault_search(...)` or `vault_get_note(slug=...)` for the **just-created note** returns **0 results / "note not found"**, even though the file exists on disk and `vault_stats` reports the incremented note count.

Observed sequence in-session:
1. `vault_create_note(title="Memory Responsibility Split", ...)` → `{ "slug": "memory-responsibility-split", "path": "...\\memory-responsibility-split.md" }` (success)
2. `vault_stats` → `total_notes: 43` (was 42 before) ✅ file landed on disk
3. `vault_get_note(slug="memory-responsibility-split")` → `error: Note 'memory-responsibility-split' not found in vault.` ❌
4. `vault_search(query="Memory Responsibility Split")` → `count: 0` ❌
5. Direct `ls` on disk confirmed the file exists at the exact path from step 1.

Conclusion: **the on-disk write succeeded, but the read/search index did not see it.** The read path and write path are inconsistent.

---

## Exact error string

```
vault_get_note(slug='memory-responsibility-split') -> error: "Note 'memory-responsibility-split' not found in vault."
vault_search(query='Memory Responsibility Split')   -> { "count": 0, "results": [] }
vault_stats()                                       -> { "total_notes": 43, ... }   # inconsistent with above
```

---

## Root-cause hypothesis

There are **two plausible mechanisms**; both point at the same architectural flaw. Need to confirm which one is live by checking the tool-server wiring (see Verification).

### Hypothesis A — Write path and read path use DIFFERENT `VaultIndex` instances (most likely)

- `vault.py` defines a single `VaultIndex` class. The MCP/tool server that exposes `vault_create_note`, `vault_search`, `vault_get_note`, `vault_stats` almost certainly constructs **one `VaultIndex` per tool call / per request handler** (or one per connection), instead of holding a single shared, long-lived instance.
- `create_note` (vault.py:1523) DOES correctly update the in-memory index on its own instance:
  - line 1576 `note = self._parse_note(note_path)`
  - line 1578 `self._notes[note.slug] = note`
  - line 1579 `self._index_note(note)`
  - line 1580 `self._insert_note_to_db(note, note_path, base_path)`
  - line 1581 `self._rebuild_search_stats()`
  So **within the same instance**, the note is indexed. The bug appears only because the *next* tool call (`search`/`get_note`) runs against a *fresh* instance that re-runs `scan()` and either:
  - loads a stale `.obsidian_vault_cache/index_<hash>.db` (FTS5 cache, written at vault.py:493 `save_cache` only when `self._dirty`), or
  - re-walks the filesystem but with a cold/old in-memory dict.
- `vault_stats` (vault.py:1485) reporting `43` while `get_note` reports not-found is the smoking gun: `get_stats()` reads `self._notes` of one instance, `get_note()` reads `self._notes` of another. They disagree → two instances.

### Hypothesis B — FTS5 cache not flushed before read

- `save_cache()` (vault.py:493) early-returns unless `self._dirty`. `create_note` sets `self._dirty = True` (line 1582), but `save_cache()` is only invoked on `flush()`/`close()` or at scan end — **not** at the end of `create_note`.
- If the read instance loads from `index_<hash>.db` (via `load_cache`, vault.py ~498) and that DB was checkpointed *before* the create, the FTS5 `vault_fts` table won't contain the new row → `search` returns 0. `get_note` (memory path) would still fail if it too loaded from cache without re-scan.
- WAL mode is on (`PRAGMA journal_mode=WAL`, line 441) so un-checkpointed writes may also be invisible across separate connections.

**Both hypotheses share one fix:** a single shared, long-lived `VaultIndex` instance for the plugin lifetime, with the FTS5 DB committed (and WAL checkpointed) immediately inside `create_note` / `update_note` / `delete` / `append_to_note`.

---

## Fix guidance (where / what to change)

File: `plugins/memory/obsidian_vault/vault.py` (also check the tool-server wrapper that imports `VaultIndex` — likely `plugin.yaml` + a server module under the same plugin dir; search for `VaultIndex()` instantiation).

1. **Singleton index per vault path.** Ensure the plugin constructs `VaultIndex` once and reuses it across all tool calls for a given `vault_path` (cache by `vault_path.resolve()`). Do NOT instantiate a new `VaultIndex` per request. If the server is async/concurrent, guard with the existing `self._lock` (already an `RLock`, good) but the *instance* must be shared, not per-call.

2. **Commit + checkpoint inside write ops.** At the end of `create_note` (after line 1583), `update_note`, `append_to_note`, and `delete`, explicitly flush the DB:
   ```python
   if self._db:
       self._db.commit()
       self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
   ```
   This guarantees a separate connection/scan sees the new row immediately instead of waiting for `save_cache()`.

3. **Make `scan()` reuse the live instance state.** If a fresh instance is unavoidable, have `scan()` / read entrypoints first check `self._notes` is non-empty AND `_last_scan` is recent before falling back to the cached DB; when in doubt after a write, prefer re-reading the FTS5 `notes` table (source of truth) rather than the in-memory dict that may belong to a different instance.

4. **Optional hardening:** in `create_note`/`update_note`, after indexing, run a self-check `assert self.get_note(note.slug) is not None` (or log a warning) so a future instance-split regression fails loudly instead of silently.

---

## Workaround (current session)

The on-disk file IS correct. To read a note you just wrote without waiting for a re-scan:
- Use the terminal / `read_file` on the exact vault path returned by `vault_create_note` (the `path` field). Disk is the source of truth.
- Or call `vault_stats` first (it forces a fresh scan in some instances) then retry `vault_get_note` / `vault_search`.
- Or restart the plugin/server so all tool calls share a re-built index from the now-correct files.

Do NOT trust `vault_search` / `vault_get_note` immediately after a write in the current build.

---

## Verification after fix

1. Repro script (run against a temp `HERMES_HOME` vault):
   ```python
   from pathlib import Path
   from obsidian_vault.vault import VaultIndex
   idx = VaultIndex()
   idx.scan(Path("/tmp/test_vault"))
   n = idx.create_note(title="Test Note", body="hello", tags=["x"])
   assert idx.get_note(n.slug) is not None, "get_note must see created note"
   assert len(idx.search("Test Note")) >= 1, "search must see created note"
   idx2 = VaultIndex()           # second instance, as the server would
   idx2.scan(Path("/tmp/test_vault"))
   assert idx2.get_note(n.slug) is not None, "fresh instance must see note too"
   ```
2. The test above must pass without restarting the server and without a manual `flush()`.
3. Run the existing vault test suite (`pytest plugins/memory/obsidian_vault/` if present) — all green.
4. Manual: in a live agent session, `vault_create_note` → immediately `vault_get_note(slug=…)` returns the note; `vault_search` returns ≥1 hit; `vault_stats.total_notes` equals `len(disk *.md) - templates`. All three must agree.

---

## Notes for the fixer

- `vault.py` is 2145 lines; this report targets only the write/read consistency path. The FTS5 + in-memory dual backend (vault.py:1082 `_search_fts5` vs `:1085 _search_inmemory`) means a fix must keep BOTH backends consistent — prefer committing the DB so the FTS5 path is always current, and ensure the in-memory dict is the shared instance.
- `save_cache` early-return on `not self._dirty` (vault.py:495) is fine for periodic persistence but must NOT be the only flush point for writes.
- Watch the `WAL` mode: separate connections reading the `.db` without a checkpoint can miss recent commits — `PRAGMA wal_checkpoint(TRUNCATE)` after each write resolves it.
