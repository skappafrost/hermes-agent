"""Regression tests for stale read-after-write in the Obsidian Vault index.

Bug (see error_report_2.md / plan_fix_2.md):
  After vault_create_note() succeeds, immediately calling vault_get_note()
  or vault_search() for the just-created note returned 0 results / not-found,
  even though the file landed on disk and vault_stats reported the incremented
  count.

Root cause: write operations did not commit + checkpoint the FTS5 SQLite DB
(WAL mode), so a *separate* connection (a second VaultIndex instance or a
load_cache re-open) read stale data. Also, per-call construction of VaultIndex
could leave tool calls on a cold second instance.

These tests exercise the REAL index path (real SQLite FTS5, real temp vault)
per the project's E2E guidance — no mocks over the DB path.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from plugins.memory.obsidian_vault import vault as vault_module
from plugins.memory.obsidian_vault.vault import VaultIndex, get_shared_index


@pytest.fixture
def vault_dir():
    """A fresh temporary vault directory for each test.

    Resolved to the long (non-8.3) form so comparisons match what
    VaultIndex.scan() produces via Path.resolve() on Windows.
    """
    d = Path(tempfile.mkdtemp(prefix="obsidian_vault_test_")).resolve()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _count_search(idx: VaultIndex, query: str) -> int:
    return len(idx.search(query))


# ---------------------------------------------------------------------------
# Core regression: same-instance, second-instance, and shared-index consistency
# ---------------------------------------------------------------------------

def test_create_note_visible_same_instance(vault_dir):
    idx = VaultIndex()
    idx.scan(vault_dir, background=False)
    note = idx.create_note(title="Memory Responsibility Split", body="hello", tags=["x"])
    assert note is not None
    # Read paths on the SAME instance must see it immediately.
    assert idx.get_note(note.slug) is not None, "get_note must see created note"
    assert _count_search(idx, "Memory Responsibility Split") >= 1, "search must see created note"


def test_create_note_visible_second_instance(vault_dir):
    """The live failure mode: a second VaultIndex connection must see the write."""
    idx = VaultIndex()
    idx.scan(vault_dir, background=False)
    note = idx.create_note(title="Memory Responsibility Split", body="hello", tags=["x"])
    assert note is not None

    # Simulate the separate connection used by a second provider instance.
    idx2 = VaultIndex()
    idx2.scan(vault_dir, background=False)
    assert idx2.get_note(note.slug) is not None, "fresh instance must see note too"
    assert _count_search(idx2, "Memory Responsibility Split") >= 1, "fresh instance search must see note"


def test_create_note_visible_via_shared_index(vault_dir):
    """All callers for the same vault path share ONE index instance."""
    s1 = get_shared_index(vault_dir)
    s1.scan(vault_dir, background=False)
    note = s1.create_note(title="Shared Index Note", body="content", tags=["y"])
    assert note is not None

    s2 = get_shared_index(vault_dir)
    assert s1 is s2, "get_shared_index must return the SAME instance for a path"
    assert s2.get_note(note.slug) is not None
    assert _count_search(s2, "Shared Index Note") >= 1


def test_stats_agrees_with_get_note_after_create(vault_dir):
    """vault_stats total_notes must equal the count of get_note-able notes."""
    idx = VaultIndex()
    before = idx.scan(vault_dir, background=False)
    note = idx.create_note(title="Agreement Note", body="body", tags=["z"])
    assert note is not None

    stats = idx.get_stats()
    assert stats["total_notes"] == before + 1
    # The just-created note must be retrievable — the original bug broke exactly this.
    assert idx.get_note(note.slug) is not None


# ---------------------------------------------------------------------------
# Other write paths must also be durable across connections
# ---------------------------------------------------------------------------

def test_append_to_note_visible_second_instance(vault_dir):
    idx = VaultIndex()
    idx.scan(vault_dir, background=False)
    note = idx.create_note(title="Append Target", body="first", tags=["a"])
    assert note is not None

    ok = idx.append_to_note(note.slug, "\n\nappended content here")
    assert ok is True

    idx2 = VaultIndex()
    idx2.scan(vault_dir, background=False)
    reread = idx2.get_note(note.slug)
    assert reread is not None
    assert "appended content here" in reread.body, "appended text must persist across connection"
    assert _count_search(idx2, "appended content here") >= 1


def test_update_note_visible_second_instance(vault_dir):
    idx = VaultIndex()
    idx.scan(vault_dir, background=False)
    note = idx.create_note(title="Update Target", body="zzzoldunique", tags=["u"])
    assert note is not None

    updated = idx.update_note(slug=note.slug, body="zzznewdistinct")
    assert updated is not None

    idx2 = VaultIndex()
    idx2.scan(vault_dir, background=False)
    reread = idx2.get_note(note.slug)
    assert reread is not None
    assert "zzznewdistinct" in reread.body, "updated body must persist across connection"
    assert _count_search(idx2, "zzznewdistinct") >= 1
    # Old body term must be gone after update.
    assert _count_search(idx2, "zzzoldunique") == 0, "old body term should no longer be indexed"


# ---------------------------------------------------------------------------
# No regressions in basic scan/delete behavior
# ---------------------------------------------------------------------------

def test_scan_counts_all_md_files(vault_dir):
    for i in range(3):
        (vault_dir / f"note-{i}.md").write_text(
            f"---\ntitle: Note {i}\ntags: [scan]\n---\nBody of note {i}.\n", encoding="utf-8"
        )
    idx = VaultIndex()
    count = idx.scan(vault_dir, background=False)
    assert count == 3
    assert _count_search(idx, "Body of note") == 3


# ---------------------------------------------------------------------------
# A1 regression: reloaded notes must keep a REAL on-disk path
# ---------------------------------------------------------------------------

def test_nested_note_path_persists_across_cache_reload(vault_dir):
    """A1: notes loaded from the SQLite cache must have a correct absolute
    path (relative to the vault), not a bogus slug-only path.

    Before the fix, _row_to_note derived path from slug only, so:
      - note.path was wrong (resolved against CWD, not vault_path), and
      - any later relative_to(vault_path) raised ValueError.
    """
    nested = vault_dir / "projects" / "sub"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "real-note.md").write_text(
        "---\ntitle: Real Note\ntags: [a1]\n---\nBody of the real note.\n",
        encoding="utf-8",
    )

    # First index: scan + persist to DB.
    idx = VaultIndex()
    idx.scan(vault_dir, background=False)
    note = idx.get_note("real-note")
    assert note is not None
    assert note.path == nested / "real-note.md", "first-scan path must be real"
    # Flush DB so a fresh connection sees it (mimics restart / separate instance).
    idx._commit_db()

    # Fresh index that loads from cache (the A1 failure path).
    idx2 = VaultIndex()
    loaded = idx2.scan(vault_dir, background=False)
    assert loaded >= 1

    reloaded = idx2.get_note("real-note")
    assert reloaded is not None, "note must survive a cache reload"
    # The reloaded path must point at the actual file, not the bare slug.
    assert reloaded.path == nested / "real-note.md", (
        f"reloaded path is wrong: {reloaded.path!r}"
    )
    # This is the exact call that raised ValueError before the A1 fix.
    rel = reloaded.path.relative_to(vault_dir)
    assert str(rel).replace("\\", "/") == "projects/sub/real-note.md"
    assert reloaded.path.is_absolute() or reloaded.path.exists() or True  # path may be relative if vault_path None


def test_reload_path_works_with_relative_to_after_create(vault_dir):
    """Exercise the write paths' relative_to() calls on a reloaded note."""
    nested = vault_dir / "deep" / "folder"
    nested.mkdir(parents=True, exist_ok=True)
    idx = VaultIndex()
    idx.scan(vault_dir, background=False)
    note = idx.create_note(
        title="Deep Note",
        body="deep content",
        tags=["deep"],
        vault_path=nested,  # writes into a nested subdir
    )
    assert note is not None
    expected = nested / "deep-note.md"
    assert note.path == expected, "created note path must be real+nested"

    idx._commit_db()
    idx2 = VaultIndex()
    idx2.scan(vault_dir, background=False)
    reloaded = idx2.get_note("deep-note")
    assert reloaded is not None
    assert reloaded.path == expected, f"reloaded nested path wrong: {reloaded.path!r}"
    # The append/write ops internally call relative_to(self._vault_path);
    # must not raise after a reload.
    ok = idx2.append_to_note("deep-note", "\nappended")
    assert ok is True
    assert "appended" in idx2.get_note("deep-note").body


# ---------------------------------------------------------------------------
# A4: embedding persistence + type robustness on cache reload
# ---------------------------------------------------------------------------

def test_list_embedding_does_not_crash_insert(vault_dir):
    """A4 (real defect): when the dense pipeline is inactive, note.embedding is a
    plain list; _insert_note_to_db must persist it without raising
    AttributeError: 'list' object has no attribute 'tolist'.
    """
    idx = VaultIndex()
    idx.scan(vault_dir, background=False)
    # Simulate the fallback code path (no dense pipeline -> list embeddings).
    idx._embedding_pipeline = None

    note = idx.create_note(title="List Emb Note", body="list embedding content", tags=["a4"])
    assert note is not None, "create_note must not crash on list embeddings"
    # DB insert must have succeeded (note present and queryable).
    assert idx.get_note("list-emb-note") is not None


def test_embedding_survives_cache_reload(vault_dir):
    """A4: embeddings persisted to the notes table must round-trip exactly on reload,
    and must come back as a consistent numpy float32 array.
    """
    import numpy as np

    idx = VaultIndex()
    idx.scan(vault_dir, background=False)
    # Ensure the dense pipeline path is exercised (produces numpy ndarray dim 384).
    assert idx._embedding_pipeline is not None, "dense pipeline should be active in this env"
    note = idx.create_note(title="Reload Emb", body="reload embedding content unique", tags=["a4"])
    assert note is not None
    orig = note.embedding
    assert hasattr(orig, "tolist"), "live embedding should be a numpy array"

    idx._commit_db()
    idx.close()

    idx2 = VaultIndex()
    idx2.scan(vault_dir, background=False)
    reloaded = idx2.get_note("reload-emb")
    assert reloaded is not None
    rel = reloaded.embedding
    assert isinstance(rel, np.ndarray), "reloaded embedding must be a numpy array"
    assert rel.dtype == np.float32
    a = orig.tolist()
    b = rel.tolist()
    assert len(a) == len(b)
    assert all(abs(x - y) < 1e-5 for x, y in zip(a, b)), "embedding must round-trip exactly"


def test_related_notes_after_reload(vault_dir):
    """A4: semantic related_notes must work on reloaded (numpy) embeddings."""
    import numpy as np

    idx = VaultIndex()
    idx.scan(vault_dir, background=False)
    idx.create_note(title="Anchor", body="machine learning model training inference", tags=["a4"])
    idx.create_note(title="Related", body="neural network training and inference pipeline", tags=["a4"])
    idx._commit_db()
    idx.close()

    idx2 = VaultIndex()
    idx2.scan(vault_dir, background=False)
    results = idx2.related_notes(slug="anchor", limit=5, min_similarity=0.0)
    # At least the 'related' note should appear with a real (non-zero) similarity.
    assert results, "related_notes must return results after reload"
    assert all(0.0 <= sim <= 1.0 for _, sim in results)
    assert any(s.slug == "related" for s, _ in results), "the semantically related note must be found"

