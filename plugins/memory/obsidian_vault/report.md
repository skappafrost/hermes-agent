# Obsidian Vault Read/Write Inconsistency — Problem Report

**Date:** 2026-08-06
**Component:** `plugins/memory/obsidian_vault/vault.py` (VaultIndex) + the tool-server wiring that exposes the vault_* tools
**Storage backend:** Google Drive (mounted as `G:\`), vault at `G:\My Drive\Hermes Memory\Vex\Hermes Vex`

---

## The Problem

After a note is written to the vault (via `vault_create_note`, which returns success with a valid slug and on-disk path), immediately reading it back through `vault_get_note(slug=...)` or `vault_search(query=...)` **fails to find the note** — even after waiting long enough for Google Drive to finish syncing the new file.

This is a **read/write index inconsistency inside the plugin**, not a storage problem.

---

## Evidence

Three independent test runs, same result:

| Step | Result |
|------|--------|
| `vault_create_note(...)` | ✅ success — returns valid `slug` + on-disk `path` |
| `vault_get_note(slug=<just created>)` | ❌ `error: Note '<slug>' not found in vault.` |
| `vault_search(query=<just created title>)` | ❌ `count: 0`, empty results |
| `vault_stats()` | `total_notes` increments correctly (42 → 43 → 44) and the new file is confirmed present on disk via terminal `ls` |

The inconsistency was reproduced both immediately after write AND after a 3-minute wait (to rule out Google Drive sync latency). The failure persists in both cases.

---

## What Works / What Doesn't

- **Works:** writing the file to disk, and any operation that reads the filesystem directly (`vault_stats` counting `.md` files, terminal `ls`, `read_file`).
- **Does NOT work:** `vault_get_note` and `vault_search` returning the just-written (or externally-added) note.

---

## Key Observations Pointing at the Plugin

1. **`vault_stats` sees the new file but `vault_get_note` does not.** `get_stats()` reports `total_notes: 44` while `get_note()` for the same note returns not-found. These two read operations disagree about what exists → they are reading from different sources of truth (one reads the filesystem/cache, the other reads an in-memory index that was never updated).

2. **`last_scan` timestamp does not change between runs.** The index's `last_scan` value stayed frozen (`1786023342.227369`) across multiple create+read cycles, meaning the in-memory/FTS5 index is never refreshed after a file is added — whether the file was added by the plugin's own `create_note` or appeared externally (e.g. synced in from Google Drive).

3. **`indexed_terms: 0` is reported** by `vault_stats` even though notes clearly exist, indicating the in-memory term index is empty/stale on the read instance.

4. **Google Drive is ruled out as the cause.** `vault_stats` correctly counts the new file after sync, proving the file reaches disk and is visible. The read tools still fail afterward, so the break is entirely in how the plugin's index is (or isn't) updated and shared between write and read operations.

---

## Root Problem Statement (no fix — for the owner to address)

The plugin's read path (`vault_get_note`, `vault_search`) relies on an in-memory / FTS5 index that is **not consistent with the actual vault contents after a write or external change**. Specifically:

- Write operations report success and the file lands on disk, but the index used by subsequent read calls is not updated / not shared, so reads cannot find the note.
- The index is not re-scanned or refreshed when files change (the `last_scan` timestamp never advances), so notes added by the plugin itself OR synced in from Google Drive are invisible to search/get_note until something forces a full rebuild.
- The net effect: **the vault write tools and the vault read tools disagree about what notes exist, despite the files being correctly present on disk.** Storage (Google Drive / local disk) is not at fault.

---

## Scope

- Affected tools: `vault_get_note`, `vault_search` (and any read tool built on the same stale index).
- Not affected: `vault_stats` (filesystem count is correct), direct disk access (`read_file`, terminal).
- Environment note: vault lives on a Google Drive-mounted path (`G:\`), which adds sync latency but is proven NOT to be the cause of the read failure.
