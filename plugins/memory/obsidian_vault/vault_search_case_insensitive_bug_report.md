# BUG REPORT: Obsidian Vault Tooling — Cache, Missing Delete, and Case-Blind Search

**Severity:** High (erodes trust in every vault operation; agents cannot rely on tool results)
**Component:** Obsidian Vault tools — `vault_search`, `vault_get_note`, `vault_update_note`, `vault_dedup`, the index/search backend, and the absence of a delete primitive.
**Reported by:** VEX (on user "anh" direction)
**Date:** 2026-08-07

---

## PROBLEMS (three distinct, all confirmed in-session)

### PROBLEM 1 — Stale cache makes tool results untrustworthy
The vault maintains an in-memory / cached index that is **not invalidated on filesystem mutation**. After a note is deleted on disk, `vault_search` and `vault_dedup` kept returning the deleted note as if it still existed (stale reads observed minutes later). An agent cannot trust "note exists" or "note not found" from these tools.

- Exact symptom string: `vault_dedup()` still listed `MEMORY_RESPONSIBILITY_SPLIT` + `memory_responsibility_split` as duplicates (similarity 0.9984) **after** both were deleted on disk.
- Exact symptom string: `vault_search(query="MEMORY_RESPONSIBILITY_SPLIT")` returned `count: 0` only eventually; intermediate calls returned the deleted note.

### PROBLEM 2 — Vault has NO delete function
There is **no `vault_delete_note` / delete primitive** in the vault toolset. To remove a note, the agent had to drop to `terminal` and `rm` the file directly. Consequences:
- The delete bypasses any index bookkeeping, worsening Problem 1 (cache never learns the note is gone).
- On a **case-insensitive filesystem** (Windows / Google Drive), `rm memory_responsibility_split.md` also silently destroyed `MEMORY_RESPONSIBILITY_SPLIT.md` — the two "copies" were one physical file. Deleting by one casing wiped both.
- The lack of a managed delete forced an out-of-band filesystem operation that the vault layer cannot observe or reconcile.

### PROBLEM 3 — Search does not cover case variations (case-blind / case-dumb)
`vault_search`, `vault_get_note`, and `vault_update_note` match on the **literal string** (case-sensitive key) instead of normalizing case. Results therefore depend on the exact letter-casing the agent happens to use, and uppercase-stored notes are effectively invisible to lowercase queries (and vice-versa).

- Exact error string:
  ```
  {"error": "Note 'memory_responsibility_split' not found in vault."}
  ```
  (returned when the only on-disk file was `MEMORY_RESPONSIBILITY_SPLIT.md`.)
- `vault_get_note(slug="MEMORY_RESPONSIBILITY_SPLIT")` and `vault_get_note(slug="memory_responsibility_split")` both "succeeded" yet resolved to the **same** physical file — proving the index treats case variants as separate keys while the filesystem treats them as one.

---

## ROOT-CAUSE HYPOTHESIS (unifying)

The vault lives on a **case-insensitive filesystem** (Windows host, Google Drive mirror), where `MEMORY_X.md` and `memory_x.md` are the **same physical file**. The vault tooling instead:

1. Keys its index by the **literal note title/slug string** (case-sensitive), so case variants become distinct cache entries that map to one file.
2. Performs lookups against the **cached index**, not the live filesystem → false "not found" on casing mismatch, false "exists" after deletion.
3. **Never invalidates/refreshes** the index on mutation (create/delete/rename), whether done via vault tools or external `rm`.
4. **Exposes no delete API**, forcing raw `rm` that the index cannot observe.

Net: the index is authoritative-but-wrong, the filesystem is single-but-hidden, and there is no managed path to reconcile them.

---

## EVIDENCE

| Step | Action | Observed result |
|------|--------|-----------------|
| 1 | `vault_get_note(slug="MEMORY_RESPONSIBILITY_SPLIT")` | OK — note returned |
| 2 | `vault_get_note(slug="memory_responsibility_split")` | OK — SAME note (different case, one file) |
| 3 | `vault_update_note(slug="memory_responsibility_split")` | ERROR: `Note 'memory_responsibility_split' not found in vault.` |
| 4 | `rm memory_responsibility_split.md` (terminal, case-insensitive FS) | deleted |
| 5 | `test -f MEMORY_RESPONSIBILITY_SPLIT.md` | GONE (proves same physical file) |
| 6 | `test -f memory_responsibility_split.md` | GONE |
| 7 | `vault_search(query="MEMORY_RESPONSIBILITY_SPLIT")` after delete | eventually `count: 0`, but stale returns occurred first |
| 8 | `vault_dedup()` after delete | STILL listed both as duplicates (similarity 0.9984) — stale index |
| 9 | `vault_create_note(title="memory_responsibility_split")` after delete | auto-renamed to `memory_responsibility_split-1.md` (index still held old key) |
| 10 | `mv memory_responsibility_split-1.md memory_responsibility_split.md` | OK on disk; index still stale |

Conclusion: single case-insensitive file; bug is **case-sensitive index keying + missing cache invalidation + no delete primitive**.

---

## RECOMMENDED FIX (for the assigned agent — NOT implemented by VEX)

1. **Add a managed delete**: `vault_delete_note(slug)` that removes the file AND invalidates the index entry. No raw `rm` should ever be needed.
2. **Normalize case on index keys**: canonicalize slugs (lowercase / casefold / NFC) so queries match regardless of input casing. Uppercase-stored notes become findable.
3. **Re-sync index on mutation**: invalidate/refresh the affected entry after any create/delete/rename (vault-tool or external). Prefer a filesystem watcher; at minimum refresh-on-write.
4. **Treat case variants as one note**: dedup/index must recognize `MEMORY_X` and `memory_x` as the same note on case-insensitive backends — never report them as duplicates or separate entries.
5. **Regression tests**:
   - Create `FOO`; query `foo` / `Foo` / `FOO` → all return it.
   - Update via each casing → all succeed.
   - Delete via one casing → search returns nothing within bounded time (cache invalidated).
   - Two case variants never appear as separate/duplicate notes.

---

## STATUS

- Real filesystem: ONE note restored as `memory_responsibility_split.md` (canonical, lowercase). Deletion was done via `terminal rm` (Problem 2 forced this).
- Built-in memory still references `MEMORY_RESPONSIBILITY_SPLIT.md` by name string — should be normalized to the canonical lowercase path once Problem 3 is fixed.
- Vault index still stale at report time; self-heals slowly but tooling must not depend on that.
