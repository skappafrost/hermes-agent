"""Obsidian Vault core — markdown note storage, indexing, and retrieval.

Scans an Obsidian vault directory, parses YAML frontmatter, extracts
wiki-links and tags, and provides full-text search with context-aware
ranking. Uses SQLite FTS5 for high-performance search with persistent
indexing.

Key features:
- SQLite FTS5 backend for full-text search
- Incremental re-indexing (only changed files)
- Vector embeddings for semantic search (numpy-based)
- BM25 ranking with phrase matching
- Query parser: boolean, field-specific, phrases, wildcards
- Bidirectional link graph
- Persistent index cache (instant cold start)
- Date range filtering
- Search result highlighting
- Cross-vault support
"""

from __future__ import annotations

import json
import logging
import math
import numpy as np
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import Counter
import hashlib

# New imports for dense embeddings
try:
    from .embeddings import (
        SentenceTransformerEmbedder,
        FAISSIndex,
        VectorStore,
        EmbeddingPipeline,
        HybridSearcher,
        DEFAULT_MODEL_NAME,
        DEFAULT_EMBEDDING_DIM
    )
    DENSE_EMBEDDINGS_AVAILABLE = True
except ImportError as e:
    logging.getLogger(__name__).warning(f"Dense embeddings not available: {e}")
    DENSE_EMBEDDINGS_AVAILABLE = False

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared index cache
# ---------------------------------------------------------------------------
# One long-lived VaultIndex per resolved vault path. Guarantees all tool
# calls share the same in-memory dict AND the same SQLite connection, so a
# write performed by one call is immediately visible to the next read call
# even if the provider/plugin layer constructs a fresh wrapper object.
_INDEX_CACHE: Dict[str, "VaultIndex"] = {}
_INDEX_CACHE_LOCK = threading.Lock()


def get_shared_index(vault_path: Path, cache_dir: Optional[Path] = None) -> "VaultIndex":
    """Return the shared VaultIndex for ``vault_path``.

    Reuses a single instance per resolved vault path instead of building a
    new one per call. This keeps write/read consistency intact across
    separate plugin/provider objects and prevents cold second instances
    from reading a stale FTS5 DB.
    """
    key = str(Path(vault_path).resolve())
    with _INDEX_CACHE_LOCK:
        idx = _INDEX_CACHE.get(key)
        if idx is None:
            idx = VaultIndex(cache_dir=cache_dir)
            _INDEX_CACHE[key] = idx
        return idx

# Constants & compiled regex patterns (cached)
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n",
    re.DOTALL,
)

WIKI_LINK_RE = re.compile(r"\[\[([^\]]+)(?:\|([^\]]+))?\]\]")
TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9_/-]+)")
HEADING_RE = re.compile(r"^#+\s+(.+)$", re.MULTILINE)

# Tokenization: split on non-alphanumeric, keep alphanumeric + hyphen/underscore
_TOKENIZE_RE = re.compile(r"[a-z0-9_-]+", re.IGNORECASE)

# Query parser patterns
_QUERY_FIELD_RE = re.compile(r"(\w+):(\S+)")
_QUERY_PHRASE_RE = re.compile(r'"([^"]+)"')
_QUERY_EXCLUDE_RE = re.compile(r'-\s*(\S+)')

# Index cache version (bump when schema changes)
_INDEX_CACHE_VERSION = 4

# BM25 parameters
_K1 = 1.5
_B = 0.75


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------

try:
    import yaml

    def parse_frontmatter(text: str) -> Dict[str, Any]:
        """Parse YAML frontmatter text into a dict."""
        try:
            return yaml.safe_load(text) or {}
        except yaml.YAMLError:
            return {}
except ImportError:
    # Fallback: minimal frontmatter parser
    def parse_frontmatter(text: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1]
            if val.lower() == "true":
                val = True
            elif val.lower() == "false":
                val = False
            else:
                try:
                    val = int(val)
                except ValueError:
                    try:
                        val = float(val)
                    except ValueError:
                        pass
            result[key] = val
        return result


# ---------------------------------------------------------------------------
# Tokenization & Text Processing
# ---------------------------------------------------------------------------

def tokenize(text: str) -> List[str]:
    """Extract lowercase tokens from text for indexing."""
    if not text:
        return []
    return _TOKENIZE_RE.findall(text.lower())


def build_query_embedding(text: str, dim: int = 128) -> List[float]:
    """Build a simple deterministic embedding for a query string.
    
    Uses TF-weighted character n-gram hashing - no external dependencies.
    This is a lightweight bag-of-character-ngrams approach that provides
    reasonable semantic similarity for short queries.
    """
    tokens = tokenize(text)
    vec = [0.0] * dim
    if not tokens:
        return vec
    
    for token in tokens:
        # Hash each character n-gram into the vector
        for i in range(len(token)):
            for n in (2, 3, 4):
                if i + n <= len(token):
                    gram = token[i:i+n]
                    idx = hash(gram) % dim
                    vec[idx] += 1.0
    
    # Normalize
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def build_note_embedding(title: str, body: str, frontmatter: Dict[str, Any], dim: int = 128) -> List[float]:
    """Build embedding for a note.
    
    Combines title, body, and frontmatter into a weighted embedding.
    Title gets 3x weight, frontmatter gets 1.5x weight.
    """
    vec = [0.0] * dim
    
    # Title (weight 3x)
    title_vec = _hash_text_to_vec(title, dim)
    for i in range(dim):
        vec[i] += title_vec[i] * 3.0
    
    # Body (weight 1x)
    body_vec = _hash_text_to_vec(body, dim)
    for i in range(dim):
        vec[i] += body_vec[i] * 1.0
    
    # Frontmatter (weight 1.5x)
    fm_text = ""
    for key, val in frontmatter.items():
        if isinstance(val, str):
            fm_text += f" {val}"
        elif isinstance(val, (list, tuple)):
            for item in val:
                if isinstance(item, str):
                    fm_text += f" {item}"
    if fm_text.strip():
        fm_vec = _hash_text_to_vec(fm_text, dim)
        for i in range(dim):
            vec[i] += fm_vec[i] * 1.5
    
    # Normalize
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def _hash_text_to_vec(text: str, dim: int) -> List[float]:
    """Hash text to a vector using character n-grams."""
    vec = [0.0] * dim
    tokens = tokenize(text)
    for token in tokens:
        for i in range(len(token)):
            for n in (2, 3, 4):
                if i + n <= len(token):
                    gram = token[i:i+n]
                    idx = hash(gram) % dim
                    vec[idx] += 1.0
    return vec


def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if vec_a is None or vec_b is None:
        return 0.0
    # numpy arrays have no safe boolean truthiness; use .size / len.
    if (hasattr(vec_a, "size") and vec_a.size == 0) or (not hasattr(vec_a, "size") and len(vec_a) == 0):
        return 0.0
    if (hasattr(vec_b, "size") and vec_b.size == 0) or (not hasattr(vec_b, "size") and len(vec_b) == 0):
        return 0.0
    if len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _has_embedding(emb) -> bool:
    """Safely test whether an embedding is non-empty.

    Works for both plain lists and numpy arrays (numpy truthiness on a
    multi-element array is ambiguous and raises ValueError).
    """
    if emb is None:
        return False
    if hasattr(emb, "size"):  # numpy ndarray
        return emb.size > 0
    return len(emb) > 0


# ---------------------------------------------------------------------------
# Query Parsing
# ---------------------------------------------------------------------------

@dataclass
class ParsedQuery:
    """Parsed search query with structured components."""
    terms: List[str]
    phrases: List[str]
    fields: Dict[str, List[str]]
    exclude_terms: List[str]
    wildcards: List[str]  # terms ending with *
    raw: str

    @property
    def has_structured(self) -> bool:
        return bool(self.phrases or self.fields or self.exclude_terms or self.wildcards)


def parse_query(query: str) -> ParsedQuery:
    """Parse query string into structured components.

    Supports:
    - "exact phrase" (quoted)
    - title:foo, tag:bar, category:baz (field-specific)
    - term* (prefix wildcard)
    - -exclude terms
    - general terms
    """
    query = query.strip()
    if not query:
        return ParsedQuery([], [], {}, [], [], query)

    # Extract phrases first
    phrases = _QUERY_PHRASE_RE.findall(query)
    remaining = _QUERY_PHRASE_RE.sub(" ", query)

    # Extract field:value pairs
    fields: Dict[str, List[str]] = {}
    for match in _QUERY_FIELD_RE.finditer(remaining):
        field_name, value = match.groups()
        field_name = field_name.lower()
        if field_name in ("title", "tag", "tags", "category", "cat"):
            # Handle date ranges: created:>2026-01-01, modified:<2026-06-01
            if value.startswith(">") or value.startswith("<") or value.startswith("="):
                fields.setdefault(f"{field_name}_{'gt' if value.startswith('>') else 'lt' if value.startswith('<') else 'eq'}", []).append(value[1:])
            else:
                # Preserve original case for category field (used for exact matching)
                # Other fields like title, tag are typically case-insensitive in search
                if field_name in ("category", "cat"):
                    fields.setdefault(field_name, []).append(value.lower())
                else:
                    fields.setdefault(field_name, []).append(value.lower())

    # Remove field:value from remaining
    remaining = _QUERY_FIELD_RE.sub(" ", remaining)

    # Extract exclusion terms (-term)
    exclude_terms = []
    for match in _QUERY_EXCLUDE_RE.finditer(remaining):
        exclude_terms.append(match.group(1).lower())
    remaining = _QUERY_EXCLUDE_RE.sub(" ", remaining)

    # Extract wildcard terms (term*)
    wildcards = []
    # Find terms that contain * 
    raw_terms = remaining.split()
    terms = []
    for t in raw_terms:
        t = t.strip()
        if not t:
            continue
        if "*" in t:
            wildcards.append(t.rstrip("*").lower())
        else:
            terms.append(t.lower())

    return ParsedQuery(terms, phrases, fields, exclude_terms, wildcards, query)


# ---------------------------------------------------------------------------
# Note model
# ---------------------------------------------------------------------------

@dataclass
class VaultNote:
    """A single note from the Obsidian vault."""

    path: Path
    slug: str
    title: str
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    body: str = ""
    tags: List[str] = field(default_factory=list)
    links: List[str] = field(default_factory=list)
    backlinks: List[str] = field(default_factory=list)
    last_modified: float = 0.0
    size_bytes: int = 0
    embedding: List[float] = field(default_factory=list)

    # Cached lowercase versions for fast search
    _title_lower: str = field(default="", init=False, repr=False)
    _body_lower: str = field(default="", init=False, repr=False)
    _full_text: str = field(default="", init=False, repr=False)
    _tokens: List[str] = field(default_factory=list, init=False, repr=False)
    _token_counts: Counter = field(default_factory=Counter, init=False, repr=False)

    def __post_init__(self):
        self._rebuild_caches()

    def _rebuild_caches(self):
        """Rebuild lowercase and token caches."""
        self._title_lower = self.title.lower()
        self._body_lower = self.body.lower()
        # Include frontmatter in searchable text
        fm_text = ""
        for key, val in self.frontmatter.items():
            if isinstance(val, str):
                fm_text += " " + val
            elif isinstance(val, (list, tuple)):
                for item in val:
                    if isinstance(item, str):
                        fm_text += " " + item
        self._full_text = f"{self.title} {self.body}{fm_text}"
        self._tokens = tokenize(self._full_text)
        self._token_counts = Counter(self._tokens)

    @property
    def category(self) -> str:
        """Primary category from frontmatter 'category' field or first tag."""
        if "category" in self.frontmatter:
            cat = self.frontmatter["category"]
            if isinstance(cat, str):
                return cat
        if self.tags:
            return self.tags[0]
        return "general"

    @property
    def mtime(self) -> datetime:
        return datetime.fromtimestamp(self.last_modified)

    @property
    def created(self) -> Optional[str]:
        """Get created date from frontmatter."""
        val = self.frontmatter.get("created")
        if isinstance(val, (str, date, datetime)):
            return str(val)
        return None

    @property
    def modified(self) -> Optional[str]:
        """Get modified date from frontmatter."""
        val = self.frontmatter.get("modified") or self.frontmatter.get("updated")
        if isinstance(val, (str, date, datetime)):
            return str(val)
        return None


# ---------------------------------------------------------------------------
# Vault Index
# ---------------------------------------------------------------------------

class VaultIndex:
    """In-memory index of an Obsidian vault's markdown notes.

    Features:
    - Thread-safe: all mutation and query methods acquire the internal lock
    - Incremental indexing: only re-parses changed files
    - SQLite FTS5 backend for high-performance search
    - Vector embeddings for semantic search
    - BM25-style ranking with phrase matching
    - Bidirectional link graph
    - Date range filtering
    - Persistent cache
    """

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        self._notes: Dict[str, VaultNote] = {}
        self._by_tag: Dict[str, Set[str]] = {}
        self._by_category: Dict[str, Set[str]] = {}
        self._by_link: Dict[str, Set[str]] = {}
        self._backlinks: Dict[str, Set[str]] = {}

        # Inverted index: term -> set of slugs (for in-memory fallback)
        self._term_index: Dict[str, Set[str]] = {}

        # Document frequencies for BM25
        self._doc_freq: Dict[str, int] = {}
        self._avg_doc_len: float = 0.0
        self._total_token_count: int = 0

        self._lock = threading.RLock()
        self._vault_path: Optional[Path] = None
        self._last_scan: Optional[float] = None
        self._file_mtimes: Dict[str, float] = {}
        self._max_notes: int = 10000  # default, will be set by provider
        self._last_incremental_scan: float = 0.0  # track last incremental scan time

        # Whether Obsidian wiki-tags (#tag) are also retrievable as categories
        # (e.g. `category:project-alpha` matches a note tagged #project-alpha).
        # Mirrors the `tags_as_categories` provider config (default True).
        self.tags_as_categories: bool = True

        # SQLite FTS5 database
        self._db: Optional[sqlite3.Connection] = None
        self._db_path: Optional[Path] = None
        self._use_fts5 = True

        # Cache configuration
        self._cache_dir = cache_dir
        self._dirty = False

        # Dense embeddings (Phase 1)
        self._embedding_pipeline = None
        self._vector_store = None
        self._embedding_config: Dict[str, Any] = {}
        self._hybrid_searcher = None

        # Background scan lifecycle state.  All access must hold ``self._lock``.
        # States: idle, loading, building, partial, ready, error
        self._scan_state = "idle"
        self._scan_error: Optional[str] = None
        self._scan_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Database Management
    # ------------------------------------------------------------------

    def _get_db_path(self, vault_path: Path) -> Path:
        """Get SQLite database path for a vault."""
        if self._cache_dir:
            cache_dir = self._cache_dir
        else:
            cache_dir = vault_path / ".obsidian_vault_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        vault_hash = hashlib.md5(str(vault_path.resolve()).encode()).hexdigest()[:12]
        return cache_dir / f"index_{vault_hash}.db"

    def _init_db(self, vault_path: Path) -> int:
        """Initialize SQLite database for FTS5 search.
        
        Reuses existing connection if already initialized for this vault path.
        """
        db_path = self._get_db_path(vault_path)
        self._db_path = db_path

        # Reuse existing connection if valid
        if self._db is not None:
            try:
                self._db.execute("SELECT 1")
                return 1  # Connection valid
            except sqlite3.Error:
                # Connection dead, will create new one below
                self._db = None

        try:
            conn = sqlite3.connect(str(db_path), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")

            # Create FTS5 virtual table (self-contained, no external content)
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5(
                    slug, title, body, frontmatter, tags, category
                )
            """)

            # Create notes table for metadata
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notes (
                    slug TEXT PRIMARY KEY,
                    title TEXT,
                    body TEXT,
                    frontmatter TEXT,
                    tags TEXT,
                    category TEXT,
                    last_modified REAL,
                    size_bytes INTEGER,
                    embedding BLOB,
                    links TEXT,
                    backlinks TEXT,
                    path TEXT
                )
            """)

            # Create indexes for tag/category lookup
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_category ON notes(category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_tags ON notes(tags)")

            # Migrate older cache DBs that lack the `path` column (added for A1 fix).
            # Without this, notes reloaded from a pre-existing cache have a bogus path.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(notes)").fetchall()}
            if "path" not in cols:
                conn.execute("ALTER TABLE notes ADD COLUMN path TEXT")


            conn.commit()
            self._db = conn
            logger.info("Initialized FTS5 database at %s", db_path)
        except Exception as e:
            logger.warning("FTS5 unavailable, falling back to in-memory index: %s", e)
            self._use_fts5 = False
            self._db = None

        return self._db is not None

    def _create_fts_sync_triggers(self, conn: sqlite3.Connection) -> None:
        """Create the standard external-content FTS5 sync triggers.

        vault_fts uses content='notes', so SQLite must keep it in sync with
        the notes table. These triggers mirror the documented FTS5 external
        content recipe: DELETE/UPDATE on notes drive the corresponding FTS
        row (by rowid), and INSERT populates it.

        For external-content tables (content='notes'), the trigger INSERT
        must use the special format: INSERT INTO fts(fts, rowid, ...) VALUES
        ('insert', new.rowid, ...) -- note the table name as first column
        and the command ('insert'/'delete') as first value.
        """
        # No triggers needed for self-contained FTS5; _insert_note_to_db
        # handles manual sync with matching rowids.

    def _get_cache_file(self, vault_path: Path) -> Path:
        """Get JSON cache file path for a vault (legacy fallback)."""
        if self._cache_dir:
            cache_dir = self._cache_dir
        else:
            cache_dir = vault_path / ".obsidian_vault_cache"
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            # Another thread/process created the directory between the check
            # and the mkdir; this is harmless on Windows.
            pass
        vault_hash = hashlib.md5(str(vault_path.resolve()).encode()).hexdigest()[:12]
        return cache_dir / f"index_{vault_hash}.json"

    def save_cache(self) -> bool:
        """Save index to persistent storage."""
        if not self._dirty or not self._vault_path:
            return True

        if self._use_fts5 and self._db:
            # FTS5 DB is the source of truth on disk, but pending writes
            # may still sit in the WAL (un-checkpointed). Commit + truncate
            # so a separate connection (second VaultIndex instance or a
            # load_cache re-open) immediately sees them.
            self._commit_db()
            self._dirty = False
            return True

        # Fallback to JSON cache
        cache_file = self._get_cache_file(self._vault_path)
        try:
            with self._lock:
                data = {
                    "version": _INDEX_CACHE_VERSION,
                    "vault_path": str(self._vault_path),
                    "last_scan": self._last_scan,
                    "file_mtimes": self._file_mtimes,
                    "notes": {slug: note.__dict__ for slug, note in self._notes.items()},
                    "by_tag": {k: list(v) for k, v in self._by_tag.items()},
                    "by_category": {k: list(v) for k, v in self._by_category.items()},
                    "by_link": {k: list(v) for k, v in self._by_link.items()},
                    "backlinks": {k: list(v) for k, v in self._backlinks.items()},
                    "doc_freq": self._doc_freq,
                    "avg_doc_len": self._avg_doc_len,
                    "total_token_count": self._total_token_count,
                }
            cache_file.write_text(json.dumps(data, default=str), encoding="utf-8")
            self._dirty = False
            return True
        except Exception as e:
            logger.warning("Failed to save cache: %s", e)
            return False

    def _commit_db(self) -> None:
        """Durably persist FTS5 writes across connections.

        Commits the pending transaction and truncates the WAL so that any
        *separate* SQLite connection (a second VaultIndex instance, or
        load_cache re-opening index_<hash>.db) immediately sees the
        new/updated rows. Without this, WAL-mode writes are invisible to
        other connections -> stale read-after-write.

        Intended to be called at the end of every mutating operation
        (create_note, append_to_note, update_note) and from save_cache().
        """
        if not self._db or not self._use_fts5:
            return
        try:
            self._db.commit()
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as e:
            logger.warning("Failed to commit/checkpoint vault index DB: %s", e)

    def load_cache(self, vault_path: Path) -> bool:
        """Load index from persistent storage if valid.

        Returns True if the on-disk cache is structurally usable.  A cache may
        be usable but empty; callers that need a non-empty index should check
        ``self._notes`` afterwards.
        """
        db_path = self._get_db_path(vault_path)
        cache_file = self._get_cache_file(vault_path)

        # Try SQLite FTS5 first
        if db_path.exists():
            try:
                # Use _init_db to get/reuse connection properly
                if self._init_db(vault_path):
                    conn = self._db
                    conn.row_factory = sqlite3.Row

                    # Basic schema validation: ensure the columns we need exist.
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(notes)").fetchall()}
                    required_cols = {"slug", "title", "body", "frontmatter", "tags", "category",
                                     "last_modified", "size_bytes", "embedding", "links", "backlinks", "path"}
                    if not required_cols.issubset(cols):
                        logger.warning("SQLite cache schema mismatch: missing columns %s", required_cols - cols)
                        return False

                    # Verify vault path matches
                    row = conn.execute("SELECT COUNT(*) as c FROM notes LIMIT 1").fetchone()
                    if row is not None:
                        with self._lock:
                            self._vault_path = vault_path
                            self._use_fts5 = True

                            # Load notes from DB.  Select every column _row_to_note needs.
                            rows = conn.execute("""
                                SELECT slug, title, body, frontmatter, tags, category,
                                       last_modified, size_bytes, embedding, links, backlinks, path
                                FROM notes
                            """).fetchall()

                            loaded_count = 0
                            skipped_count = 0
                            for row in rows:
                                try:
                                    note = self._row_to_note(row, vault_path)
                                    self._notes[note.slug] = note
                                    self._index_note(note, skip_db=True)
                                    # Store relative path for mtime tracking
                                    try:
                                        rel_path = str(note.path.relative_to(vault_path))
                                    except (ValueError, OSError):
                                        rel_path = note.slug + ".md"
                                    self._file_mtimes[rel_path] = note.last_modified
                                    loaded_count += 1
                                except Exception as e:
                                    skipped_count += 1
                                    slug = row["slug"] if "slug" in row.keys() else "<unknown>"
                                    logger.warning("Failed to load cached note %s: %s", slug, e)

                            # Rebuild search index stats
                            self._rebuild_search_stats()

                            # Check for file changes
                            cache_mtime = db_path.stat().st_mtime
                            for rel_path, mtime in list(self._file_mtimes.items()):
                                full_path = vault_path / rel_path
                                if full_path.exists() and full_path.stat().st_mtime > mtime:
                                    # File changed, will re-index incrementally
                                    break
                            else:
                                self._last_scan = datetime.now().timestamp()
                                self._dirty = False
                                logger.info("Loaded vault index from SQLite cache (%d notes, %s skipped)",
                                            loaded_count, skipped_count)
                                return True

                            # File changes detected, but DB is loaded - do incremental
                            return True
            except Exception as e:
                logger.warning("Failed to load from SQLite cache: %s", e)

        # Fallback to JSON cache
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if data.get("version") != _INDEX_CACHE_VERSION:
                    return False
                if data.get("vault_path") != str(vault_path.resolve()):
                    return False

                with self._lock:
                    self._vault_path = vault_path
                    self._last_scan = data.get("last_scan")
                    self._file_mtimes = data.get("file_mtimes", {})
                    self._notes = {slug: self._dict_to_note(d) for slug, d in data.get("notes", {}).items()}
                    self._by_tag = {k: set(v) for k, v in data.get("by_tag", {}).items()}
                    self._by_category = {k: set(v) for k, v in data.get("by_category", {}).items()}
                    self._by_link = {k: set(v) for k, v in data.get("by_link", {}).items()}
                    self._backlinks = {k: set(v) for k, v in data.get("backlinks", {}).items()}
                    self._doc_freq = data.get("doc_freq", {})
                    self._avg_doc_len = data.get("avg_doc_len", 0.0)
                    self._total_token_count = data.get("total_token_count", 0)
                    self._dirty = False

                logger.info("Loaded vault index cache from JSON (%d notes)", len(self._notes))
                return True
            except Exception as e:
                logger.warning("Failed to load JSON cache: %s", e)

        return False

    def _dict_to_note(self, d: Dict[str, Any]) -> VaultNote:
        """Reconstruct VaultNote from dict (JSON cache)."""
        return VaultNote(
            path=Path(d["path"]),
            slug=d["slug"],
            title=d["title"],
            frontmatter=d.get("frontmatter", {}),
            body=d.get("body", ""),
            tags=d.get("tags", []),
            links=d.get("links", []),
            backlinks=d.get("backlinks", []),
            last_modified=d.get("last_modified", 0.0),
            size_bytes=d.get("size_bytes", 0),
        )

    def _row_to_note(self, row: sqlite3.Row, vault_path: Optional[Path] = None) -> VaultNote:
        """Convert DB row to VaultNote.

        Reconstructs the real on-disk path from the stored `path` column
        (relative to the vault root) so reloaded notes keep a correct
        absolute path. Falls back to slug-based path if the column is empty
        (older caches from before the A1 fix).
        """
        links = json.loads(row["links"]) if row["links"] else []
        backlinks = json.loads(row["backlinks"]) if row["backlinks"] else []
        frontmatter = json.loads(row["frontmatter"]) if row["frontmatter"] else {}
        tags = json.loads(row["tags"]) if row["tags"] else []
        embedding = []
        if row["embedding"]:
            try:
                raw = json.loads(row["embedding"]) if isinstance(row["embedding"], str) else list(row["embedding"])
                embedding = np.asarray(raw, dtype=np.float32)
            except Exception:
                embedding = []

        note = VaultNote(
            path=Path(row["title"]),  # placeholder, replaced below
            slug=row["slug"],
            title=row["title"],
            frontmatter=frontmatter,
            body=row["body"],
            tags=tags,
            links=links,
            backlinks=backlinks,
            last_modified=row["last_modified"] or 0.0,
            size_bytes=row["size_bytes"] or 0,
            embedding=embedding,
        )
        # Reconstruct the real on-disk path (A1 fix).
        stored_path = row["path"] if "path" in row.keys() else None
        if stored_path:
            note.path = (Path(vault_path) / stored_path) if vault_path else Path(stored_path)
        else:
            # Legacy cache without a path column: best-effort slug derivation.
            note.path = (Path(vault_path) / f"{row['slug']}.md") if vault_path else Path(row["slug"])
        return note

    # ------------------------------------------------------------------
    # Scanning (Incremental)
    # ------------------------------------------------------------------

    def scan(self, vault_path: Path, max_notes: int = 10000, *,
             background: bool = True) -> int:
        """Scan the vault directory and index all .md files.

        Uses incremental indexing: only re-parses files that have changed.
        Loads from cache if available and valid.

        If ``background`` is True (the default), the entire scan process is
        scheduled on a daemon thread and this method returns 0 immediately.
        The provider/tool layer must remain usable while the background builder
        runs; calls should check ``scan_state``/``is_ready`` and report
        "starting up" until the state becomes ``ready``.
        """
        vault_path = vault_path.resolve()
        if not vault_path.is_dir():
            logger.warning("Vault path does not exist or is not a directory: %s", vault_path)
            return 0

        self._max_notes = max_notes

        if background:
            with self._lock:
                if self._scan_state in ("building", "loading"):
                    logger.info("Background vault scan already in progress for %s", vault_path)
                    return 0
                self._scan_state = "loading"
                self._scan_error = None

            def _run_scan():
                try:
                    # Synchronous scan inside the background thread.
                    count = self._do_scan(vault_path, max_notes)
                    with self._lock:
                        self._scan_state = "ready"
                    logger.info("Background vault scan finished for %s: %d notes", vault_path, count)
                    return count
                except Exception as e:
                    logger.exception("Background vault scan failed for %s: %s", vault_path, e)
                    with self._lock:
                        self._scan_state = "error"
                        self._scan_error = str(e)
                    raise

            thread = threading.Thread(target=_run_scan, daemon=True, name=f"obsidian-vault-scan-{vault_path.name}")
            self._scan_thread = thread
            thread.start()
            logger.info("Started background vault scan for %s", vault_path)
            return 0

        # Synchronous path (used by tests or explicit callers).
        if DENSE_EMBEDDINGS_AVAILABLE and self._embedding_pipeline is None:
            self._init_embedding_pipeline(vault_path)
        count = self._do_scan(vault_path, max_notes)
        with self._lock:
            self._scan_state = "ready"
        return count

    def _do_scan(self, vault_path: Path, max_notes: int) -> int:
        """Internal synchronous scan: load cache, then incremental or full scan."""
        cache_loaded = self.load_cache(vault_path)

        with self._lock:
            self._vault_path = vault_path

        if cache_loaded:
            return self._incremental_scan(vault_path, max_notes)

        # No usable cache — full scan.
        return self._full_scan(vault_path, max_notes)

    def _init_embedding_pipeline(self, vault_path: Path):
        """Initialize the dense embedding pipeline."""
        if not DENSE_EMBEDDINGS_AVAILABLE:
            logger.warning("Dense embeddings not available, skipping initialization")
            return

        try:
            cache_dir = self._cache_dir or (vault_path / ".obsidian_vault_cache")
            cache_dir.mkdir(parents=True, exist_ok=True)

            self._embedding_config = {
                "model_name": "sentence-transformers/all-MiniLM-L6-v2",
                "embedding_dim": 384,
                "index_backend": "faiss",
                "index_path": str(vault_path / ".obsidian_vault_cache" / "faiss_index.bin"),
                "cache_dir": str(vault_path / ".obsidian_vault_cache")
            }

            self._embedding_pipeline = EmbeddingPipeline(
                model_name=self._embedding_config["model_name"],
                dim=self._embedding_config["embedding_dim"],
                index_backend=self._embedding_config["index_backend"],
                index_path=Path(self._embedding_config["index_path"]),
                cache_dir=self._embedding_config["cache_dir"]
            )

            # Initialize hybrid searcher
            self._hybrid_searcher = HybridSearcher(
                vault_index=self,
                embedding_pipeline=self._embedding_pipeline,
                bm25_weight=0.5,
                dense_weight=0.5,
                rrf_k=60
            )

            logger.info("Initialized dense embedding pipeline with FAISS index")
        except Exception as e:
            logger.warning(f"Failed to initialize embedding pipeline: {e}")
            self._embedding_pipeline = None
            self._hybrid_searcher = None

    def _full_scan(self, vault_path: Path, max_notes: int) -> int:
        """Full index rebuild from scratch."""
        with self._lock:
            self._vault_path = vault_path
            self._notes.clear()
            self._by_tag.clear()
            self._by_category.clear()
            self._by_link.clear()
            self._backlinks.clear()
            self._doc_freq.clear()
            self._file_mtimes.clear()
            self._total_token_count = 0

            # Initialize DB
            if self._init_db(vault_path):
                conn = self._db
                if conn:
                    conn.execute("DELETE FROM notes")
                    conn.execute("DELETE FROM vault_fts")
                    conn.commit()

            count = 0
            commit_batch = 50
            for md_file in sorted(vault_path.rglob("*.md")):
                if count >= max_notes:
                    break
                parts = md_file.relative_to(vault_path).parts
                if any(p.startswith(".") for p in parts):
                    continue
                if ".obsidian_vault_cache" in parts:
                    continue
                try:
                    note = self._parse_note(md_file)
                    if note and note.slug:
                        self._notes[note.slug] = note
                        self._index_note(note)
                        rel_path = str(md_file.relative_to(vault_path))
                        self._file_mtimes[rel_path] = note.last_modified
                        self._insert_note_to_db(note, md_file, vault_path)
                        count += 1
                        if count % commit_batch == 0:
                            self._commit_db()
                            logger.debug("Committed batch of %d notes during full scan", commit_batch)
                except Exception as e:
                    logger.debug("Failed to index note %s: %s", md_file, e)

            self._rebuild_search_stats()
            self._last_scan = datetime.now().timestamp()
            self._dirty = True
            self._commit_db()  # Commit inserts from full scan
            return count

    def _incremental_scan(self, vault_path: Path, max_notes: int) -> int:
        """Incremental scan: only process new/changed/deleted files."""
        with self._lock:
            if not self._init_db(vault_path):
                # Fallback without DB
                return self._incremental_scan_no_db(vault_path, max_notes)

            conn = self._db
            if not conn:
                return self._incremental_scan_no_db(vault_path, max_notes)

            current_files: Set[str] = set()
            count = 0
            processed = 0

            for md_file in sorted(vault_path.rglob("*.md")):
                if processed >= max_notes:
                    break
                parts = md_file.relative_to(vault_path).parts
                if any(p.startswith(".") for p in parts):
                    continue
                if ".obsidian_vault_cache" in parts:
                    continue

                rel_path = str(md_file.relative_to(vault_path))
                current_files.add(rel_path)

                try:
                    stat = md_file.stat()
                    mtime = stat.st_mtime
                    old_mtime = self._file_mtimes.get(rel_path)

                    # Fast path: file unchanged since the cache was written.
                    # Skip it WITHOUT the (sleeping) stability check so a warm
                    # cache scan stays fast and never blocks agent init.
                    if old_mtime is not None and mtime <= old_mtime:
                        processed += 1
                        continue

                    # New or changed file: skip the expensive stability sleep during
                    # scan; if the file is being written to, parse_note will fail
                    # and we simply skip it. This keeps agent init fast.
                    if not self._is_file_stable_fast(md_file):
                        logger.debug("File not stable, skipping: %s", md_file)
                        processed += 1
                        continue

                    if old_mtime is None or mtime > old_mtime:
                        note = self._parse_note(md_file)
                        if note and note.slug:
                            if old_mtime is not None and note.slug in self._notes:
                                self._remove_note(note.slug)
                                conn.execute("DELETE FROM notes WHERE slug = ?", (note.slug,))
                            self._notes[note.slug] = note
                            self._index_note(note)
                            self._file_mtimes[rel_path] = mtime
                            self._insert_note_to_db(note, md_file, vault_path)
                            count += 1
                            self._dirty = True
                        elif note and not note.slug:
                            # Note without slug - use rel_path
                            self._file_mtimes[rel_path] = mtime
                            self._insert_note_to_db(note, md_file, vault_path)
                            count += 1
                            self._dirty = True
                except (OSError, PermissionError) as e:
                    # Retry with exponential backoff for transient errors
                    retry_count = 0
                    max_retries = 3
                    base_delay = 0.5
                    while retry_count < max_retries:
                        time.sleep(base_delay * (2 ** retry_count))
                        try:
                            stat = md_file.stat()
                            mtime = stat.st_mtime
                            old_mtime = self._file_mtimes.get(rel_path)
                            
                            if not self._is_file_stable_fast(md_file):
                                logger.debug("File not stable, skipping: %s", md_file)
                                break
                            
                            if old_mtime is None or mtime > old_mtime:
                                note = self._parse_note(md_file)
                                if note and note.slug:
                                    if old_mtime is not None and note.slug in self._notes:
                                        self._remove_note(note.slug)
                                        conn.execute("DELETE FROM notes WHERE slug = ?", (note.slug,))
                                    self._notes[note.slug] = note
                                    self._index_note(note)
                                    self._file_mtimes[rel_path] = mtime
                                    self._insert_note_to_db(note, md_file, vault_path)
                                    count += 1
                                    self._dirty = True
                                elif note and not note.slug:
                                    self._file_mtimes[rel_path] = mtime
                                    self._insert_note_to_db(note, md_file, vault_path)
                                    count += 1
                                    self._dirty = True
                            break
                        except Exception as e:
                            retry_count += 1
                            if retry_count >= max_retries:
                                logger.debug("Failed to index note %s after retries: %s", md_file, e)
                            else:
                                time.sleep(base_delay * (2 ** retry_count))
                except Exception as e:
                    logger.debug("Failed to index note %s: %s", md_file, e)
                processed += 1

            # Detect deleted files
            indexed_files = set(self._file_mtimes.keys())
            deleted = indexed_files - current_files
            for rel_path in deleted:
                slug_to_remove = None
                for slug, note in list(self._notes.items()):
                    try:
                        if str(note.path.resolve().relative_to(vault_path.resolve())) == rel_path:
                            slug_to_remove = slug
                            break
                    except (ValueError, AttributeError):
                        if str(note.path.resolve()) == str((vault_path / rel_path).resolve()):
                            slug_to_remove = slug
                            break
                if slug_to_remove:
                    self._remove_note(slug_to_remove)
                    conn.execute("DELETE FROM notes WHERE slug = ?", (slug_to_remove,))
                    self._dirty = True
                del self._file_mtimes[rel_path]

            if count > 0 or deleted:
                self._rebuild_search_stats()
                conn.commit()

            self._last_scan = datetime.now().timestamp()
            return len(self._notes)

    def _incremental_scan_no_db(self, vault_path: Path, max_notes: int) -> int:
        """Incremental scan without SQLite DB (fallback)."""
        with self._lock:
            current_files: Set[str] = set()
            count = 0
            processed = 0

            for md_file in sorted(vault_path.rglob("*.md")):
                if processed >= max_notes:
                    break
                parts = md_file.relative_to(vault_path).parts
                if any(p.startswith(".") for p in parts):
                    continue
                if ".obsidian_vault_cache" in parts:
                    continue

                rel_path = str(md_file.relative_to(vault_path))
                current_files.add(rel_path)

                try:
                    stat = md_file.stat()
                    mtime = stat.st_mtime
                    old_mtime = self._file_mtimes.get(rel_path)

                    if old_mtime is None or mtime > old_mtime:
                        note = self._parse_note(md_file)
                        if note and note.slug:
                            if old_mtime is not None and note.slug in self._notes:
                                self._remove_note(note.slug)
                            self._notes[note.slug] = note
                            self._index_note(note)
                            self._file_mtimes[rel_path] = mtime
                            count += 1
                            self._dirty = True
                except Exception as e:
                    logger.debug("Failed to index note %s: %s", md_file, e)
                processed += 1

            indexed_files = set(self._file_mtimes.keys())
            deleted = indexed_files - current_files
            for rel_path in deleted:
                for slug, note in list(self._notes.items()):
                    try:
                        if str(note.path.relative_to(vault_path)) == rel_path:
                            self._remove_note(slug)
                            self._dirty = True
                            break
                    except (ValueError, AttributeError):
                        pass
                del self._file_mtimes[rel_path]

            if count > 0 or deleted:
                self._rebuild_search_stats()

            self._last_scan = datetime.now().timestamp()
            return count

    def _parse_note(self, path: Path) -> Optional[VaultNote]:
        """Parse a single .md file into a VaultNote."""
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.debug("Cannot read note %s: %s", path, e)
            return None

        slug = path.stem
        frontmatter: Dict[str, Any] = {}
        body = raw

        fm_match = _FRONTMATTER_RE.match(raw)
        if fm_match:
            frontmatter = parse_frontmatter(fm_match.group(1))
            body = raw[fm_match.end():]

        # Title: from frontmatter 'title', then first H1, then slug
        title = slug.replace("-", " ").replace("_", " ").title()
        if "title" in frontmatter and isinstance(frontmatter["title"], str):
            title = frontmatter["title"]
        else:
            h1_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
            if h1_match:
                title = h1_match.group(1).strip()

        tags = extract_tags(body)
        if "tags" in frontmatter:
            fm_tags = frontmatter["tags"]
            if isinstance(fm_tags, list):
                tags.extend(str(t) for t in fm_tags)
            elif isinstance(fm_tags, str):
                tags.extend(t.strip() for t in fm_tags.split(",") if t.strip())

        links = extract_wiki_links(body)
        resolved_links: List[str] = []
        for link in links:
            resolved_links.append(link)
            slugified = link.lower().replace(" ", "-")
            if slugified != link:
                resolved_links.append(slugified)

        try:
            stat = path.stat()
        except OSError:
            stat = None

        # Build embedding
        if self._embedding_pipeline:
            embedding = self._embedding_pipeline.encode_single(f"{title} {body}")
        else:
            embedding = build_note_embedding(title, body, frontmatter)

        return VaultNote(
            path=path,
            slug=slug,
            title=title,
            frontmatter=frontmatter,
            body=body,
            tags=list(set(tags)),
            links=resolved_links,
            last_modified=stat.st_mtime if stat else 0.0,
            size_bytes=stat.st_size if stat else 0,
            embedding=embedding,
        )

    def _index_note(self, note: VaultNote, skip_db: bool = False) -> None:
        """Add a note to all reverse indices."""
        slug = note.slug

        for tag in note.tags:
            self._by_tag.setdefault(tag, set()).add(slug)
        self._by_category.setdefault(note.category, set()).add(slug)
        # When tags act as categories, a note tagged #project-alpha is also
        # retrievable via `category:project-alpha`.
        if self.tags_as_categories:
            for tag in note.tags:
                self._by_category.setdefault(tag, set()).add(slug)
        for link in note.links:
            self._by_link.setdefault(link, set()).add(slug)

        # Populate term index for in-memory fallback search
        tokens = set(note._tokens)
        for token in tokens:
            self._term_index.setdefault(token, set()).add(slug)

    def _remove_note(self, slug: str) -> None:
                """Remove a note from all indices."""
                note = self._notes.pop(slug, None)
                if not note:
                    return

                for tag in note.tags:
                    if tag in self._by_tag:
                        self._by_tag[tag].discard(slug)
                        if not self._by_tag[tag]:
                            del self._by_tag[tag]

                cat = note.category
                if cat in self._by_category:
                    self._by_category[cat].discard(slug)
                    if not self._by_category[cat]:
                        del self._by_category[cat]

                # Remove from tag-as-category buckets too
                if self.tags_as_categories:
                    for tag in note.tags:
                        if tag in self._by_category:
                            self._by_category[tag].discard(slug)
                            if not self._by_category[tag]:
                                del self._by_category[tag]

                for link in note.links:
                    if link in self._by_link:
                        self._by_link[link].discard(slug)
                        if not self._by_link[link]:
                            del self._by_link[link]

                for backlink in note.backlinks:
                    if backlink in self._backlinks:
                        self._backlinks[backlink].discard(slug)
                        if not self._backlinks[backlink]:
                            del self._backlinks[backlink]

                # Remove from term index
                tokens = set(note._tokens)
                for token in tokens:
                    if token in self._term_index:
                        self._term_index[token].discard(slug)
                        if not self._term_index[token]:
                            del self._term_index[token]

                # Remove from FAISS index (tombstone)
                if self._embedding_pipeline:
                    self._embedding_pipeline.remove_note(slug)

    def _insert_note_to_db(self, note: VaultNote, path: Path, vault_path: Path) -> None:
        """Insert or update a note in the SQLite database."""
        if not self._db or not self._use_fts5:
            return

        conn = self._db
        # Get relative path
        try:
            rel_path = str(path.relative_to(vault_path))
        except ValueError:
            rel_path = str(path)

        # Store note metadata
        frontmatter_json = json.dumps(note.frontmatter, default=str)
        tags_json = json.dumps(note.tags)
        links_json = json.dumps(note.links)
        backlinks_json = json.dumps(note.backlinks)
        # Embeddings may be a numpy ndarray (dense pipeline) or a plain list
        # (fallback build_note_embedding). Normalize to a JSON-serializable
        # list so both paths persist without raising AttributeError.
        if note.embedding is not None:
            emb_list = note.embedding.tolist() if hasattr(note.embedding, "tolist") else list(note.embedding)
            embedding_json = json.dumps(emb_list)
        else:
            embedding_json = None

        conn.execute("""
            INSERT OR REPLACE INTO notes
                (slug, title, body, frontmatter, tags, category,
                 last_modified, size_bytes, embedding, links, backlinks, path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            note.slug, note.title, note.body, frontmatter_json,
            tags_json, note.category, note.last_modified,
            note.size_bytes, embedding_json, links_json, backlinks_json,
            rel_path,
        ))

        # For self-contained FTS5, manually insert/update with matching rowid.
        # Use INSERT OR REPLACE on notes first to get stable rowid, then sync FTS.
        row = conn.execute("SELECT rowid FROM notes WHERE slug = ?", (note.slug,)).fetchone()
        if row:
            fts_rowid = row[0]
            conn.execute("DELETE FROM vault_fts WHERE rowid = ?", (fts_rowid,))
            conn.execute("""
                INSERT INTO vault_fts(rowid, slug, title, body, frontmatter, tags, category)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                fts_rowid, note.slug, note.title, note.body,
                frontmatter_json, tags_json, note.category
            ))

        # Update FAISS index
        if self._embedding_pipeline and note.embedding is not None:
            try:
                # Check if embedding is a valid array with elements
                embedding = note.embedding
                if hasattr(embedding, '__len__') and len(embedding) > 0:
                    self._embedding_pipeline.add_note(note.slug, f"{note.title} {note.body}")
            except Exception:
                # Embedding/FAISS failures must not break core vault writes.
                # The except is broad because FAISS can raise RuntimeError
                # (e.g. "add_with_ids not implemented for this type of index")
                # when the index backend is misconfigured.
                logger.debug("FAISS add_note skipped for %s", note.slug)


    def _rebuild_search_stats(self) -> None:
        """Rebuild document frequencies and BM25 stats from all notes."""
        self._doc_freq.clear()
        self._total_token_count = 0

        for note in self._notes.values():
            doc_tokens = set(note._tokens)
            self._total_token_count += len(note._tokens)
            for term in doc_tokens:
                self._doc_freq[term] = self._doc_freq.get(term, 0) + 1

        # Rebuild backlink index
        self._backlinks.clear()
        for slug, note in self._notes.items():
            for link in note.links:
                self._backlinks.setdefault(link, set()).add(slug)
        for slug, note in self._notes.items():
            note.backlinks = list(self._backlinks.get(slug, set()))

        doc_count = len(self._notes)
        self._avg_doc_len = self._total_token_count / doc_count if doc_count > 0 else 0.0
        self._dirty = True

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def _check_and_refresh(self, vault_path: Path) -> bool:
        """Check if vault has changed since last scan and trigger incremental scan if needed.
        
        Returns True if index was refreshed, False if no changes detected.
        """
        if not self._vault_path or not vault_path.exists():
            return False
        
        try:
            # Quick check: compare vault directory mtime with last_scan
            vault_mtime = vault_path.stat().st_mtime
            if self._last_scan and vault_mtime <= self._last_scan:
                # Directory mtime unchanged, but individual files might have been deleted
                # Check a few known files' mtimes as a lightweight check
                if self._file_mtimes:
                    for rel_path, mtime in list(self._file_mtimes.items())[:5]:  # Sample check
                        full_path = vault_path / rel_path
                        if full_path.exists() and full_path.stat().st_mtime > mtime:
                            break
                    else:
                        # Check if any tracked file was deleted
                        for rel_path in list(self._file_mtimes.keys())[:5]:
                            full_path = vault_path / rel_path
                            if not full_path.exists():
                                break
                        else:
                            return False  # No changes detected
            
            # Trigger incremental scan to pick up new/changed/deleted files
            logger.info("Vault changed since last scan, triggering incremental re-scan")
            self._incremental_scan(vault_path, max_notes=self._max_notes)
            return True
        except Exception as e:
            logger.warning("Failed to check/refresh vault: %s", e)
            return False

    def _is_file_stable(self, path: Path, min_stable_duration: float = 0.5) -> bool:
        """Check if a file is stable (not being written to).
        
        A file is considered stable if its size remains constant over a short period.
        
        Args:
            path: Path to the file
            min_stable_duration: Minimum time (seconds) the file size must remain stable
            
        Returns:
            True if file appears stable, False otherwise
        """
        try:
            if not path.exists():
                return False
            
            size1 = path.stat().st_size
            if size1 == 0:
                return False  # Empty file likely being written
            
            time.sleep(min_stable_duration)
            
            if not path.exists():
                return False
            
            size2 = path.stat().st_size
            return size1 == size2
        except (OSError, PermissionError):
            return False

    def _is_file_stable_fast(self, path: Path) -> bool:
        """Non-blocking variant used during scans.

        We cannot afford a 0.5s sleep per file when indexing hundreds or
        thousands of notes during agent init.  If the file is mid-write, the
        parse step will fail and we skip it.
        """
        try:
            if not path.exists():
                return False
            size1 = path.stat().st_size
            if size1 == 0:
                return False
            # Tiny nap (10ms) to catch very fast writes without blocking init.
            time.sleep(0.01)
            if not path.exists():
                return False
            size2 = path.stat().st_size
            return size1 == size2
        except (OSError, PermissionError):
            return False

    def _wait_for_stable(self, path: Path, max_wait: float = 10.0, poll_interval: float = 0.5) -> bool:
        """Wait for a file to become stable (size stops changing).
        
        Args:
            path: Path to the file
            max_wait: Maximum time to wait in seconds
            poll_interval: Time between size checks
            
        Returns:
            True if file stabilized, False if timeout or error
        """
        import time
        start_time = time.time()
        last_size = -1
        
        while time.time() - start_time < max_wait:
            try:
                if not path.exists():
                    return False
                
                size = path.stat().st_size
                if size == 0:
                    time.sleep(0.5)
                    continue
                
                if size == last_size:
                    return True  # Size stable
                
                last_size = size
                time.sleep(0.5)
            except (OSError, PermissionError):
                return False
        
        return False  # Timeout

    def search(
        self,
        query: str,
        *,
        mode: str = "both",
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        tag: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        sort_by: str = "relevance",
        semantic: bool = False,
        semantic_weight: float = 0.3,
    ) -> List[VaultNote]:
        """Search the vault index.

        Args:
            query: Search query string. Supports:
                - "exact phrase" (quoted)
                - title:foo, tag:bar, category:baz (field-specific)
                - term* (prefix wildcard)
                - -exclude terms
            mode: 'frontmatter', 'content', or 'both' (legacy)
            category: Filter by category (legacy)
            tags: Filter by tags - all must match (legacy)
            tag: Single tag filter (legacy)
            limit: Maximum results
            offset: Pagination offset
            sort_by: 'relevance', 'modified', 'title'
            semantic: Use semantic search (embedding similarity)
            semantic_weight: Weight for semantic vs keyword score (0-1)

        Returns notes ranked by relevance score (descending).
        """
        parsed = parse_query(query)

        # Handle legacy parameters
        if category:
            parsed.fields.setdefault("category", []).append(category)  # preserve original case
        if tag:
            parsed.fields.setdefault("tag", []).append(tag.lower())
        if tags:
            parsed.fields.setdefault("tag", []).extend(t.lower() for t in tags)

        with self._lock:
            # Auto-refresh if vault has changed externally (e.g., Drive sync)
            if self._vault_path:
                self._check_and_refresh(self._vault_path)
            
            if not self._notes:
                return []

            # Use hybrid search if semantic mode and dense embeddings available
            if semantic and DENSE_EMBEDDINGS_AVAILABLE:
                if self._hybrid_searcher is None and self._vault_path:
                    self._init_embedding_pipeline(self._vault_path)
                if self._hybrid_searcher:
                    return self._hybrid_searcher.search(
                        query=query,
                        limit=limit,
                        offset=offset,
                        category=category,
                        tags=tags,
                        sort_by=sort_by
                    )

            # Fallback to existing FTS5 search
            if self._use_fts5 and self._db:
                return self._search_fts5(parsed, mode, limit, offset, sort_by, semantic, semantic_weight)
            else:
                return self._search_inmemory(parsed, mode, limit, offset, sort_by, semantic, semantic_weight)

    def _search_fts5(
        self, parsed: ParsedQuery, mode: str, limit: int, offset: int,
        sort_by: str, semantic: bool, semantic_weight: float
    ) -> List[VaultNote]:
        """Search using SQLite FTS5."""
        conn = self._db
        if not conn:
            return []

        # Build FTS5 query - use implicit AND (space) for multi-word queries
        # FTS5 treats space as implicit AND, which is more natural for user queries
        fts_query_parts = []
        for term in parsed.terms:
            fts_query_parts.append(term)
        for phrase in parsed.phrases:
            fts_query_parts.append(f'"{phrase}"')

        if not fts_query_parts and not parsed.fields and not parsed.phrases:
            # Field-only query (category/tag filter) or empty query
            candidates = set(self._notes.keys())
            for field_name, values in parsed.fields.items():
                if field_name in ("category", "cat"):
                    for v in values:
                        # Case-insensitive category match (categories are stored
                        # in their original frontmatter case).
                        matched = set()
                        for k, slugs in self._by_category.items():
                            if k.lower() == v.lower():
                                matched |= slugs
                        candidates &= matched
                elif field_name in ("tag", "tags"):
                    for v in values:
                        candidates &= self._by_tag.get(v, set())
            # Empty query with no filters returns empty
            if not parsed.terms and not parsed.phrases and not parsed.fields and not parsed.exclude_terms:
                if not parsed.fields:
                    return []
            notes = [self._notes[s] for s in candidates if s in self._notes]
            return self._sort_notes(notes, sort_by)[offset:offset+limit]

        # Build FTS5 query: use implicit AND (space) for multiple terms
        # This is more natural for user queries and avoids OR precedence issues
        fts_query = " ".join(fts_query_parts) if fts_query_parts else "*"

        # Build WHERE clause for field filters
        where_clauses = []
        params = []

        # Add FTS MATCH condition
        if fts_query_parts:
            where_clauses.append(f"n.slug IN (SELECT slug FROM vault_fts WHERE vault_fts MATCH ?)")
            params.append(fts_query)

        # Category filter
        if parsed.fields.get("category"):
            cats = parsed.fields["category"]
            # Case-insensitive match against the stored category (raw frontmatter case).
            where_clauses.append(f"LOWER(n.category) IN ({','.join('?' * len(cats))})")
            params.extend(c.lower() for c in cats)

        # Tag filter
        if parsed.fields.get("tag"):
            tags_list = parsed.fields["tag"]
            # tags stored as JSON, use LIKE for simplicity
            for t in tags_list:
                where_clauses.append(f"n.tags LIKE ?")
                params.append(f'%"{t}"%')

        # Exclusion
        for exclude_term in parsed.exclude_terms:
            where_clauses.append(f"n.slug NOT IN (SELECT slug FROM vault_fts WHERE vault_fts MATCH ?)")
            params.append(exclude_term)

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        # For reliability, fetch candidates and score in Python
        # Get matching slugs from FTS
        matching_slugs = set()
        if fts_query_parts:
            try:
                fts_sql = f"SELECT slug FROM vault_fts WHERE vault_fts MATCH ?"
                rows = conn.execute(fts_sql, [fts_query]).fetchall()
                matching_slugs = {row[0] for row in rows}
            except Exception as e:
                logger.warning("FTS5 search failed for query '%s': %s", fts_query, e)
                # Fall through to fallback
                matching_slugs = set()
        elif parsed.fields:
            # Pure field filter (tag/category) without FTS terms - start with all notes
            matching_slugs = set(self._notes.keys())

        # If no FTS match, fall back to inverted index
        if not matching_slugs and parsed.terms:
            for term in parsed.terms:
                matching_slugs |= self._term_index.get(term, set())

        # Apply field filters
        if parsed.fields:
            field_candidates = set(self._notes.keys())
            for field_name, values in parsed.fields.items():
                if field_name in ("category", "cat"):
                    for v in values:
                        # Case-insensitive category match (categories are stored
                        # in their original frontmatter case).
                        matched = set()
                        for k, slugs in self._by_category.items():
                            if k.lower() == v.lower():
                                matched |= slugs
                        field_candidates &= matched
                elif field_name in ("tag", "tags"):
                    for v in values:
                        field_candidates &= self._by_tag.get(v, set())
            matching_slugs &= field_candidates

        # Apply exclusions
        for exclude_term in parsed.exclude_terms:
            matching_slugs -= self._term_index.get(exclude_term, set())

        # Also add phrase matches
        for phrase in parsed.phrases:
            phrase_terms = tokenize(phrase)
            for term in phrase_terms:
                matching_slugs |= self._term_index.get(term, set())

        # Score candidates
        scored = self._score_candidates(matching_slugs, parsed, mode, semantic, semantic_weight)

        if sort_by == "relevance":
            scored.sort(key=lambda x: (-x[1], -x[0].last_modified))
            return [note for note, _ in scored[offset:offset+limit]]
        else:
            notes = [n for n, _ in scored]
            return self._sort_notes(notes, sort_by)[offset:offset+limit]

    def _search_inmemory(
        self, parsed: ParsedQuery, mode: str, limit: int, offset: int,
        sort_by: str, semantic: bool, semantic_weight: float
    ) -> List[VaultNote]:
        """Search using in-memory inverted index (fallback)."""
        with self._lock:
            if not self._notes:
                return []

            if not parsed.has_structured and not parsed.terms:
                if not query.strip() and not parsed.fields:
                    return []
                candidates = set(self._notes.keys())
                if parsed.fields.get("category"):
                    for cat in parsed.fields["category"]:
                        # Case-insensitive category match (keys stored in raw frontmatter case).
                        matched = set()
                        for k, slugs in self._by_category.items():
                            if k.lower() == cat.lower():
                                matched |= slugs
                        candidates &= matched
                if parsed.fields.get("tag"):
                    for t in parsed.fields["tag"]:
                        candidates &= self._by_tag.get(t, set())
                notes = [self._notes[s] for s in candidates]
                return self._sort_notes(notes, sort_by)[offset:offset+limit]

            candidates = self._find_candidates(parsed)
            if not candidates:
                return []

            scored = self._score_candidates(candidates, parsed, mode, semantic, semantic_weight)

            if sort_by == "relevance":
                scored.sort(key=lambda x: (-x[1], -x[0].last_modified))
                return [note for note, _ in scored[offset:offset+limit]]
            else:
                notes = [n for n, _ in scored]
                return self._sort_notes(notes, sort_by)[offset:offset+limit]

    def _find_candidates(self, parsed: ParsedQuery) -> Set[str]:
        """Find candidate slugs using inverted index."""
        candidates: Optional[Set[str]] = None

        if not parsed.terms and not parsed.phrases and parsed.fields:
            candidates = set(self._notes.keys())

        for term in parsed.terms:
            term_slugs = self._term_index.get(term, set())
            if candidates is None:
                candidates = term_slugs.copy()
            else:
                candidates &= term_slugs
            if not candidates:
                return set()

        # Wildcard matching
        for wildcard_term in parsed.wildcards:
            wildcard_matches = set()
            for term in self._term_index:
                if term.startswith(wildcard_term):
                    wildcard_matches |= self._term_index[term]
            if candidates is None:
                candidates = wildcard_matches
            else:
                candidates &= wildcard_matches
            if not candidates:
                return set()

        for phrase in parsed.phrases:
            phrase_terms = tokenize(phrase)
            phrase_candidates = set()
            for term in phrase_terms:
                term_slugs = self._term_index.get(term, set())
                if not phrase_candidates:
                    phrase_candidates = term_slugs.copy()
                else:
                    phrase_candidates &= term_slugs
            if candidates is None:
                candidates = phrase_candidates
            else:
                candidates &= phrase_candidates
            if not candidates:
                return set()

        for exclude_term in parsed.exclude_terms:
            exclude_slugs = self._term_index.get(exclude_term, set())
            if candidates:
                candidates -= exclude_slugs

        return candidates or set()

    def _score_candidates(
        self, candidates: Set[str], parsed: ParsedQuery, mode: str = "both",
        semantic: bool = False, semantic_weight: float = 0.3
    ) -> List[Tuple[VaultNote, float]]:
        """Score candidates using BM25 + semantic similarity."""
        scored: List[Tuple[VaultNote, float]] = []
        N = len(self._notes)

        # Date filtering
        date_gt = parsed.fields.get("created_gt", []) + parsed.fields.get("modified_gt", [])
        date_lt = parsed.fields.get("created_lt", []) + parsed.fields.get("modified_lt", [])

        for slug in candidates:
            note = self._notes.get(slug)
            if not note:
                continue

            # Title field filter (post-hoc)
            if "title" in parsed.fields:
                title_match = False
                for title_term in parsed.fields["title"]:
                    if title_term in note._title_lower:
                        title_match = True
                        break
                if not title_match:
                    continue

            # Date range filtering
            should_skip = False
            for date_val in date_gt:
                note_date = note.modified or note.created
                if note_date and str(note_date) < date_val:
                    should_skip = True
                    break
            for date_val in date_lt:
                note_date = note.modified or note.created
                if note_date and str(note_date) > date_val:
                    should_skip = True
                    break
            if should_skip:
                continue

            score = 0.0
            doc_len = len(note._tokens)

            # BM25 for general terms
            for term in parsed.terms:
                tf = note._token_counts.get(term, 0)
                if tf == 0:
                    continue
                df = self._doc_freq.get(term, 1)
                idf = max(0.0, math.log((N - df + 0.5) / (df + 0.5) + 1))
                score += idf * (tf * (_K1 + 1)) / (tf + _K1 * (1 - _B + _B * doc_len / self._avg_doc_len))

            # Wildcard scoring
            for wildcard_term in parsed.wildcards:
                for note_term, tf in note._token_counts.items():
                    if note_term.startswith(wildcard_term):
                        df = self._doc_freq.get(note_term, 1)
                        idf = max(0.0, math.log((N - df + 0.5) / (df + 0.5) + 1))
                        score += idf * tf * 2.0  # Boost for wildcard matches

            # Phrase boost
            for phrase in parsed.phrases:
                if phrase.lower() in note._body_lower:
                    score += 20.0
                elif phrase.lower() in note._title_lower:
                    score += 30.0

            # Title field matches
            if "title" in parsed.fields:
                for title_term in parsed.fields["title"]:
                    if title_term == note._title_lower:
                        score += 25.0
                    elif title_term in note._title_lower:
                        score += 10.0

            # Tag/category boosts
            if parsed.fields.get("tag"):
                score += 2.0 * len(parsed.fields["tag"])
            if parsed.fields.get("category"):
                score += 2.0 * len(parsed.fields["category"])

            # Heading matches
            for m in HEADING_RE.finditer(note.body):
                heading = m.group(1).strip().lower()
                for term in parsed.terms:
                    if term in heading:
                        score += 5.0

            # Semantic search
            if semantic and _has_embedding(note.embedding):
                # Encode the query in the SAME space as the note embeddings.
                # When the dense pipeline is active, note embeddings are
                # dim-384 SentenceTransformer vectors, so the query must use the
                # pipeline too (not the dim-128 hashed fallback, which would
                # silently score 0.0 on the length-mismatch guard). When the
                # pipeline is unavailable, notes were indexed with the dim-128
                # fallback, and the dim-128 query is consistent.
                if self._embedding_pipeline is not None:
                    query_vec = self._embedding_pipeline.encode_single(parsed.raw)
                else:
                    query_vec = build_query_embedding(parsed.raw)
                sim = cosine_similarity(query_vec, note.embedding)
                score += sim * 10.0 * semantic_weight

            if score > 0:
                scored.append((note, score))

        return scored

    def _sort_notes(self, notes: List[VaultNote], sort_by: str) -> List[VaultNote]:
        """Sort notes by specified criterion."""
        if sort_by == "modified":
            return sorted(notes, key=lambda n: -n.last_modified)
        elif sort_by == "title":
            return sorted(notes, key=lambda n: n.title.lower())
        else:
            return sorted(notes, key=lambda n: -n.last_modified)

    def get_note(self, slug: str) -> Optional[VaultNote]:
        """Retrieve a single note by slug (case-insensitive)."""
        with self._lock:
            slug_lower = slug.casefold()
            for k, n in self._notes.items():
                if k.casefold() == slug_lower:
                    return n
            return None

    def get_link_context(
        self, slug: str, depth: int = 2, include_backlinks: bool = True
    ) -> List[VaultNote]:
        """Get notes linked from (and optionally to) the given note, up to depth hops."""
        with self._lock:
            note = self._notes.get(slug)
            if not note:
                return []

            visited: Set[str] = {slug}
            frontier: Set[str] = set()
            # Resolve links to actual slugs
            for link in note.links:
                slugified = link.lower().replace(" ", "-")
                frontier.add(slugified)
                frontier.add(link)

            if include_backlinks:
                for bl in note.backlinks:
                    frontier.add(bl)

            results: List[VaultNote] = []

            for _ in range(depth):
                next_frontier: Set[str] = set()
                for link_slug in frontier:
                    if link_slug in visited:
                        continue
                    visited.add(link_slug)
                    linked_note = self._notes.get(link_slug)
                    if linked_note:
                        results.append(linked_note)
                        for l in linked_note.links:
                            slugified = l.lower().replace(" ", "-")
                            next_frontier.add(slugified)
                            next_frontier.add(l)
                        if include_backlinks:
                            for bl in linked_note.backlinks:
                                next_frontier.add(bl)
                frontier = next_frontier

            return results

    def get_notes_by_tag(self, tag: str) -> List[VaultNote]:
        """Get all notes with a given tag (case-insensitive)."""
        with self._lock:
            tag_lower = tag.casefold()
            for k, v in self._by_tag.items():
                if k.casefold() == tag_lower:
                    return [self._notes[s] for s in v if s in self._notes]
            return []

    def get_notes_by_category(self, category: str) -> List[VaultNote]:
        """Get all notes in a given category (case-insensitive)."""
        with self._lock:
            cat_lower = category.casefold()
            for k, v in self._by_category.items():
                if k.casefold() == cat_lower:
                    return [self._notes[s] for s in v if s in self._notes]
            return []

    def get_all_notes(self) -> List[VaultNote]:
        """Return all indexed notes, newest first."""
        with self._lock:
            return sorted(self._notes.values(), key=lambda n: -n.last_modified)

    def get_stats(self) -> Dict[str, Any]:
        """Return index statistics."""
        with self._lock:
            all_tags: Set[str] = set()
            all_categories: Set[str] = set()
            for note in self._notes.values():
                all_tags.update(note.tags)
                all_categories.add(note.category)
            return {
                "total_notes": len(self._notes),
                "total_tags": len(all_tags),
                "total_categories": len(all_categories),
                "vault_path": str(self._vault_path) if self._vault_path else "",
                "last_scan": self._last_scan,
                "indexed_terms": len(self._term_index),
                "avg_doc_len": round(self._avg_doc_len, 1),
                "backend": "fts5" if self._use_fts5 else "in-memory",
            }

    def is_empty(self) -> bool:
        with self._lock:
            return len(self._notes) == 0

    @property
    def is_ready(self) -> bool:
        """Return True when the background scan has finished successfully."""
        with self._lock:
            return self._scan_state == "ready"

    @property
    def scan_state(self) -> str:
        """Current background scan state (idle/loading/building/partial/ready/error)."""
        with self._lock:
            return self._scan_state

    @property
    def scan_error(self) -> Optional[str]:
        with self._lock:
            return self._scan_error

    def flush(self) -> bool:
        """Force save cache to disk."""
        return self.save_cache()

    def close(self) -> None:
        """Close the database connection."""
        if self._db:
            self._db.commit()
            self._db.close()
            self._db = None

    # ------------------------------------------------------------------
    # Write Operations
    # ------------------------------------------------------------------

    def create_note(
        self,
        title: str,
        body: str = "",
        frontmatter: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
        vault_path: Optional[Path] = None,
    ) -> Optional[VaultNote]:
        """Create a new note in the vault.

        Args:
            title: Note title (will be slugified for filename)
            body: Markdown body content
            frontmatter: YAML frontmatter dict
            tags: List of tags
            category: Note category
            vault_path: Where to save in vault (defaults to self._vault_path root)

        Returns: VaultNote if created, None on failure.
        """
        if not self._vault_path and not vault_path:
            return None

        base_path = vault_path or self._vault_path
        slug = title.replace(" ", "-").lower()
        # Ensure uniqueness against both the in-memory index and the filesystem.
        # Re-check on every iteration (the previous implementation captured
        # `existing` once, so after the first rename the loop condition
        # `existing.slug == slug` went false and it could emit an already-taken
        # slug, overwriting an existing note).
        counter = 1
        while slug in self._notes or (base_path / f"{slug}.md").exists():
            slug = f"{title.replace(' ', '-').lower()}-{counter}"
            counter += 1

        note_path = base_path / f"{slug}.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)

        # Build frontmatter
        fm = frontmatter or {}
        if "title" not in fm:
            fm["title"] = title
        if category and "category" not in fm:
            fm["category"] = category
        if tags and "tags" not in fm:
            fm["tags"] = tags
        if "created" not in fm:
            fm["created"] = datetime.now().isoformat()
        if "modified" not in fm:
            fm["modified"] = datetime.now().isoformat()

        # Write file
        self._write_note_file_preserving(note_path, title, body, fm, "")

        # Re-read and index
        note = self._parse_note(note_path)
        if note:
            with self._lock:
                self._notes[note.slug] = note
                self._index_note(note)
                self._insert_note_to_db(note, note_path, base_path)
                rel_path = str(note.path.relative_to(self._vault_path))
                self._file_mtimes[rel_path] = note.last_modified
                self._rebuild_search_stats()
                self._dirty = True
                self._commit_db()
            # Loud failure if a future instance-split/commit regression
            # reappears: after a successful create, get_note must read it back.
            if self.get_note(note.slug) is None:
                logger.error(
                    "CRITICAL: create_note committed note '%s' but get_note "
                    "cannot read it back - read/write consistency broken.",
                    note.slug,
                )
        return note

    def delete_note(self, slug: str) -> bool:
        """Delete a note from the vault (file and index).
        
        Args:
            slug: Note slug to delete (case-insensitive).
            
        Returns: True if deleted, False if not found.
        """
        with self._lock:
            # Case-insensitive lookup
            slug_lower = slug.casefold()
            note = None
            actual_slug = None
            for k, n in self._notes.items():
                if k.casefold() == slug_lower:
                    note = n
                    actual_slug = k
                    break
            
            if not note:
                return False
            
            # Delete file
            try:
                note.path.unlink()
            except OSError:
                pass  # File already gone
            
            # Remove from all indices
            self._remove_note(actual_slug)
            
            # Remove from FTS5 DB
            if self._db and self._use_fts5:
                conn = self._db
                conn.execute("DELETE FROM notes WHERE slug = ?", (actual_slug,))
                conn.execute("DELETE FROM vault_fts WHERE slug = ?", (actual_slug,))
                conn.commit()
            
            # Remove from file_mtimes
            rel_path = str(note.path.relative_to(self._vault_path))
            if rel_path in self._file_mtimes:
                del self._file_mtimes[rel_path]
            
            self._rebuild_search_stats()
            self._dirty = True
            self._commit_db()
            return True

    def append_to_note(
        self,
        slug: str,
        content: str,
        vault_path: Optional[Path] = None,
    ) -> bool:
        """Append content to an existing note.

        Args:
            slug: Note slug to append to
            content: Content to append
            vault_path: Vault path (defaults to self._vault_path)

        Returns: True if successful, False if note not found.
        """
        # Case-insensitive lookup
        slug_lower = slug.casefold()
        note = None
        for k, n in self._notes.items():
            if k.casefold() == slug_lower:
                note = n
                break
        if not note:
            return False

        try:
            note_path = note.path
            with open(note_path, "a", encoding="utf-8") as f:
                f.write(content)

            # Re-parse and update index
            note = self._parse_note(note_path)
            if note:
                old_links = set(self._notes[note.slug].links)
                with self._lock:
                    self._notes[note.slug] = note
                    self._index_note(note)
                    self._insert_note_to_db(note, note_path, note_path.parent)
                    rel_path = str(note.path.relative_to(self._vault_path))
                    self._file_mtimes[rel_path] = note.last_modified
                    self._rebuild_search_stats()
                    self._dirty = True
                    self._commit_db()
            return True
        except Exception as e:
            logger.debug("Failed to append to note %s: %s", slug, e)
            return False

    def update_note(
        self,
        slug: str,
        title: Optional[str] = None,
        body: Optional[str] = None,
        frontmatter: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> Optional[VaultNote]:
        """Update an existing note's content.

        Args:
            slug: Note slug to update
            title: New title (optional)
            body: New body content (optional)
            frontmatter: Replace frontmatter (optional)
            tags: New tags list (optional)
            category: New category (optional)

        Returns: Updated VaultNote if successful, None if not found.
        """
        # Case-insensitive lookup
        slug_lower = slug.casefold()
        note = None
        actual_slug = None
        for k, n in self._notes.items():
            if k.casefold() == slug_lower:
                note = n
                actual_slug = k
                break
        if not note:
            return None

        try:
            # Read the original file to preserve structure
            original_content = note.path.read_text(encoding="utf-8")
            
            # Parse the original to get frontmatter and body boundaries
            fm_match = _FRONTMATTER_RE.match(original_content)
            if fm_match:
                original_fm_text = fm_match.group(1)
                original_body = original_content[fm_match.end():]
            else:
                original_fm_text = ""
                original_body = original_content

            # Parse frontmatter
            current_fm = parse_frontmatter(original_fm_text)
            current_body = original_body
            current_title = note.title

            if title is not None:
                current_title = title
            if frontmatter is not None:
                current_fm = frontmatter.copy()
                if "title" not in current_fm:
                    current_fm["title"] = current_title
            if category is not None:
                current_fm["category"] = category
            if tags is not None:
                current_fm["tags"] = tags
            if body is not None:
                current_body = body

            current_fm["modified"] = datetime.now().isoformat()

            # Write file with preserved structure
            self._write_note_file_preserving(note.path, current_title, current_body, current_fm, original_content)

            # Re-parse and update
            new_note = self._parse_note(note.path)
            if new_note:
                old_links = set(note.links)
                with self._lock:
                    self._notes[note.slug] = new_note
                    self._index_note(new_note)
                    self._insert_note_to_db(new_note, note.path, note.path.parent)
                    rel_path = str(new_note.path.relative_to(self._vault_path))
                    self._file_mtimes[rel_path] = new_note.last_modified
                    self._rebuild_search_stats()
                    self._dirty = True
                    self._commit_db()
                return new_note
            return note
        except Exception as e:
            logger.debug("Failed to update note %s: %s", slug, e)
            return None

    def _write_note_file_preserving(
        self, path: Path, title: str, body: str, frontmatter: Dict[str, Any], original_content: str
    ) -> None:
        """Write a note file preserving original structure where possible."""
        lines = ["---"]
        for key, val in frontmatter.items():
            if isinstance(val, str):
                if ":" in val or "#" in val or "\n" in val:
                    lines.append(f'{key}: "{val}"')
                else:
                    lines.append(f"{key}: {val}")
            elif isinstance(val, bool):
                lines.append(f"{key}: {str(val).lower()}")
            elif isinstance(val, (int, float)):
                lines.append(f"{key}: {val}")
            elif isinstance(val, list):
                lines.append(f"{key}:")
                for item in val:
                    lines.append(f"  - {item}")
            elif isinstance(val, (date, datetime)):
                lines.append(f"{key}: {val.isoformat()}")
            else:
                lines.append(f"{key}: {val}")
        lines.append("---")
        lines.append("")
        lines.append(body)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")

    # ------------------------------------------------------------------
    # Intelligence: Related Notes
    # ------------------------------------------------------------------

    def related_notes(
        self,
        slug: str,
        limit: int = 10,
        min_similarity: float = 0.1,
        exclude_wikilinks: bool = False,
    ) -> List[Tuple[VaultNote, float]]:
        """Find semantically related notes using embedding similarity.

        Args:
            slug: Note slug to find related notes for
            limit: Maximum results
            min_similarity: Minimum similarity score (0.0-1.0)
            exclude_wikilinks: If True, exclude notes already linked via wiki-links

        Returns: List of (note, similarity_score) tuples, sorted by similarity.
        """
        note = self._notes.get(slug)
        if not note:
            for k in self._notes:
                if k == slug or k == slug.lower().replace(" ", "-"):
                    note = self._notes[k]
                    break
        if not note:
            return []

        if not _has_embedding(note.embedding):
            return []

        # Get wikilink neighbors (to optionally exclude)
        wikilink_neighbors: Set[str] = set()
        if exclude_wikilinks:
            for link in note.links:
                target = link.lower().replace(" ", "-")
                if target in self._notes:
                    wikilink_neighbors.add(target)
            for bl in note.backlinks:
                if bl in self._notes:
                    wikilink_neighbors.add(bl)

        results: List[Tuple[VaultNote, float]] = []
        for other_slug, other_note in self._notes.items():
            if other_slug == note.slug:
                continue
            if other_slug in wikilink_neighbors:
                continue
            if not _has_embedding(other_note.embedding):
                continue

            sim = cosine_similarity(note.embedding, other_note.embedding)
            if sim >= min_similarity:
                results.append((other_note, sim))

        results.sort(key=lambda x: -x[1])
        return results[:limit]

    # ------------------------------------------------------------------
    # Intelligence: Validation
    # ------------------------------------------------------------------

    def validate(self) -> Dict[str, Any]:
        """Validate the vault for inconsistencies.

        Checks for:
        - Broken wiki-links (links to nonexistent notes)
        - Duplicate slugs
        - Invalid frontmatter
        - Missing metadata
        - Invalid categories
        """
        with self._lock:
            broken_links = []
            missing_metadata = []
            invalid_frontmatter = []
            duplicate_slugs = []
            missing_categories = []

            # Check for broken wiki-links
            for slug, note in self._notes.items():
                for link in note.links:
                    target_slug = link.lower().replace(" ", "-")
                    if target_slug not in self._notes and link not in self._notes:
                        # Try case-insensitive match
                        found = False
                        for k in self._notes:
                            if k.lower() == target_slug:
                                found = True
                                break
                        if not found:
                            broken_links.append({
                                "from": slug,
                                "link": link,
                                "resolved": target_slug,
                            })

            # Check for duplicate slugs (should be caught by dict, but check)
            slug_counts: Counter = Counter()
            for slug, note in self._notes.items():
                slug_counts[slug] += 1
            duplicate_slugs = [slug for slug, count in slug_counts.items() if count > 1]

            # Check for missing metadata
            for slug, note in self._notes.items():
                if not note.tags:
                    missing_metadata.append({
                        "slug": slug,
                        "issue": "missing_tags",
                    })
                if not note.frontmatter.get("created"):
                    missing_metadata.append({
                        "slug": slug,
                        "issue": "missing_created_date",
                    })

            # Check for invalid categories (non-string or empty)
            for slug, note in self._notes.items():
                cat = note.category
                if not cat or not isinstance(cat, str) or cat == "general":
                    missing_categories.append({
                        "slug": slug,
                        "category": cat,
                    })

            return {
                "total_notes_validated": len(self._notes),
                "broken_links": broken_links,
                "broken_links_count": len(broken_links),
                "duplicate_slugs": duplicate_slugs,
                "duplicate_slugs_count": len(duplicate_slugs),
                "missing_metadata": missing_metadata,
                "missing_metadata_count": len(missing_metadata),
                "missing_categories": missing_categories,
                "missing_categories_count": len(missing_categories),
                "is_valid": len(broken_links) == 0 and len(duplicate_slugs) == 0,
                # Machine-readable summary
                "summary": {
                    "total_issues": len(broken_links) + len(duplicate_slugs) + len(missing_metadata) + len(missing_categories),
                    "has_broken_links": len(broken_links) > 0,
                    "has_duplicate_slugs": len(duplicate_slugs) > 0,
                    "has_missing_metadata": len(missing_metadata) > 0,
                    "has_missing_categories": len(missing_categories) > 0,
                }
            }

    # ------------------------------------------------------------------
    # Intelligence: Orphan Detection
    # ------------------------------------------------------------------

    def find_orphans(self) -> Dict[str, Any]:
        """Find orphan and weakly connected notes.

        Returns:
            orphans: Notes with no incoming or outgoing links
            weakly_connected: Notes with only 1 link direction
            isolated_clusters: Groups of disconnected notes
        """
        with self._lock:
            orphans = []
            weakly_connected = []

            for slug, note in self._notes.items():
                outgoing = len(note.links)
                incoming = len(note.backlinks)

                if outgoing == 0 and incoming == 0:
                    orphans.append({
                        "slug": slug,
                        "title": note.title,
                    })
                elif min(outgoing, incoming) <= 1:
                    weakly_connected.append({
                        "slug": slug,
                        "title": note.title,
                        "outgoing": outgoing,
                        "incoming": incoming,
                    })

            # Find isolated clusters (connected components)
            visited: Set[str] = set()
            clusters = []

            for slug in self._notes:
                if slug in visited:
                    continue
                cluster = self._find_cluster(slug, visited)
                if cluster:
                    clusters.append({
                        "size": len(cluster),
                        "notes": [self._notes[s].title for s in cluster if s in self._notes],
                    })

            isolated_clusters = [c for c in clusters if c["size"] == 1]

            return {
                "total_orphans": len(orphans),
                "total_weakly_connected": len(weakly_connected),
                "total_clusters": len(clusters),
                "total_isolated_clusters": len(isolated_clusters),
                "orphans": orphans,
                "weakly_connected": weakly_connected[:50],
                "isolated_clusters": isolated_clusters[:20],
            }

    def _find_cluster(self, start_slug: str, visited: Set[str]) -> Set[str]:
        """Find connected component using BFS."""
        cluster: Set[str] = set()
        queue = [start_slug]

        while queue:
            slug = queue.pop(0)
            if slug in cluster or slug in visited:
                continue
            cluster.add(slug)
            visited.add(slug)

            note = self._notes.get(slug)
            if not note:
                # Try slugified
                for k in self._notes:
                    if k == slug or k == slug.lower().replace(" ", "-"):
                        note = self._notes[k]
                        slug = k
                        break
            if not note:
                continue

            # Add neighbors
            for link in note.links:
                target = link.lower().replace(" ", "-")
                if target in self._notes and target not in cluster:
                    queue.append(target)
            for bl in note.backlinks:
                if bl in self._notes and bl not in cluster:
                    queue.append(bl)

        return cluster

    # ------------------------------------------------------------------
    # Analytics: Enhanced Stats
    # ------------------------------------------------------------------

    def get_enhanced_stats(self) -> Dict[str, Any]:
        """Return detailed vault analytics."""
        with self._lock:
            all_tags: Counter = Counter()
            all_categories: Counter = Counter()
            tag_to_notes: Dict[str, Set[str]] = {}
            note_sizes: List[Tuple[str, str, int]] = []
            note_link_counts: List[Tuple[str, str, int, int]] = []
            growth_by_month: Dict[str, int] = {}

            for slug, note in self._notes.items():
                for tag in note.tags:
                    all_tags[tag] += 1
                    tag_to_notes.setdefault(tag, set()).add(slug)
                all_categories[note.category] += 1
                note_sizes.append((slug, note.title, len(note.body)))
                note_link_counts.append((slug, note.title, len(note.links), len(note.backlinks)))

                # Growth by month from last_modified
                if note.last_modified:
                    dt = datetime.fromtimestamp(note.last_modified)
                    month_key = dt.strftime("%Y-%m")
                    growth_by_month[month_key] = growth_by_month.get(month_key, 0) + 1

            # Most common tags
            most_common_tags = all_tags.most_common(20)

            # Unused tags (tags with only 1 note)
            unused_tags = [tag for tag, count in all_tags.items() if count <= 1]

            # Category distribution
            category_distribution = dict(all_categories.most_common())

            # Largest notes
            largest_notes = sorted(note_sizes, key=lambda x: -x[2])[:20]

            # Most connected notes (by total links)
            most_connected = sorted(note_link_counts, key=lambda x: -(x[2] + x[3]))[:20]

            # Average links per note
            total_links = sum(nlc[2] for nlc in note_link_counts)
            avg_links = total_links / len(note_link_counts) if note_link_counts else 0

            # Growth over time (sorted by month)
            growth = dict(sorted(growth_by_month.items()))

            return {
                "total_notes": len(self._notes),
                "total_tags": len(all_tags),
                "total_categories": len(all_categories),
                "vault_path": str(self._vault_path) if self._vault_path else "",
                "last_scan": self._last_scan,
                "indexed_terms": len(self._term_index),
                "avg_doc_len": round(self._avg_doc_len, 1),
                "backend": "fts5" if self._use_fts5 else "in-memory",
                "most_common_tags": [{"tag": t, "count": c} for t, c in most_common_tags],
                "unused_tags": unused_tags,
                "category_distribution": category_distribution,
                "average_note_size_chars": round(sum(n[2] for n in note_sizes) / len(note_sizes), 0) if note_sizes else 0,
                "average_links_per_note": round(avg_links, 2),
                "largest_notes": [{"slug": s, "title": t, "size_chars": sz} for s, t, sz in largest_notes],
                "most_connected_notes": [{"slug": s, "title": t, "outgoing": o, "incoming": i} for s, t, o, i in most_connected],
                "growth_by_month": growth,
            }

    # ------------------------------------------------------------------
    # Analytics: Graph Analytics
    # ------------------------------------------------------------------

    def get_graph_analytics(self) -> Dict[str, Any]:
        """Compute graph analytics on the vault's link graph."""
        with self._lock:
            # Build adjacency list
            graph: Dict[str, Set[str]] = {}
            for slug in self._notes:
                graph[slug] = set()

            for slug, note in self._notes.items():
                for link in note.links:
                    target = link.lower().replace(" ", "-")
                    if target in self._notes:
                        graph[slug].add(target)
                    if link in self._notes:
                        graph[slug].add(link)

            # Degree centrality (in + out)
            degree: Dict[str, int] = {}
            for slug in self._notes:
                in_degree = sum(1 for k, v in graph.items() if slug in v)
                out_degree = len(graph.get(slug, set()))
                degree[slug] = in_degree + out_degree

            # PageRank (simplified)
            pagerank = self._compute_pagerank(graph)

            # Connected components
            visited: Set[str] = set()
            components = []
            for slug in self._notes:
                if slug not in visited:
                    component = self._find_cluster(slug, visited)
                    if len(component) > 1:
                        components.append({
                            "size": len(component),
                            "notes": [self._notes[s].title for s in component if s in self._notes],
                        })

            # Isolated clusters (single-node components)
            isolated = [c for c in components if c["size"] == 1]

            # Largest connected component
            largest_component = max(components, key=lambda c: c["size"]) if components else None

            # Top by degree centrality
            top_degree = sorted(degree.items(), key=lambda x: -x[1])[:20]

            return {
                "total_nodes": len(self._notes),
                "total_edges": sum(len(v) for v in graph.values()),
                "degree_centrality": [{"slug": s, "title": self._notes[s].title, "degree": d} for s, d in top_degree],
                "pagerank": [{"slug": s, "title": self._notes[s].title, "score": r} for s, r in sorted(pagerank.items(), key=lambda x: -x[1])[:20]],
                "connected_components": len(components) + len(isolated),
                "isolated_components": len(isolated),
                "isolated_nodes": [{"slug": s, "title": self._notes[s].title} for s, d in top_degree if d == 0][:50],
                "largest_connected_component": {
                    "size": largest_component["size"] if largest_component else 0,
                    "notes": largest_component["notes"] if largest_component else [],
                } if largest_component else None,
            }

    def _compute_pagerank(self, graph: Dict[str, Set[str]], iterations: int = 10, damping: float = 0.85) -> Dict[str, float]:
        """Compute PageRank for the link graph."""
        n = len(graph)
        if n == 0:
            return {}

        rank = {slug: 1.0 / n for slug in graph}

        for _ in range(iterations):
            new_rank = {}
            for slug in graph:
                incoming = [k for k, v in graph.items() if slug in v]
                pr = (1.0 - damping) / n
                pr += damping * sum(rank[k] / max(len(graph[k]), 1) for k in incoming)
                new_rank[slug] = pr
            rank = new_rank

        return rank


# ---------------------------------------------------------------------------
# Link and tag extraction
# ---------------------------------------------------------------------------

def extract_wiki_links(content: str) -> List[str]:
    """Extract wiki-link targets from markdown content."""
    return [m.group(1) for m in WIKI_LINK_RE.finditer(content)]


def extract_tags(content: str) -> List[str]:
    """Extract Obsidian-style tags (#tag) from markdown content."""
    return list({m.group(1) for m in TAG_RE.finditer(content)})