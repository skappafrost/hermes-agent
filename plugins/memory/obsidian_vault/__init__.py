"""obsidian_vault — Obsidian Vault memory provider for Hermes.

Markdown-based long-term knowledge storage with YAML frontmatter
indexing, full-text search, wiki-link context expansion, and
tag-based retrieval. Integrates with the Hermes MemoryProvider
ABC for seamless context injection and persistent recall.

Config in config.yaml:
  memory:
    provider: obsidian_vault
  plugins:
    obsidian_vault:
      vault_path: /path/to/vault
      index_on_write: true
      max_notes: 10000
      search_mode: both       # frontmatter | content | both
      tags_as_categories: true
      link_context_depth: 2
      auto_extract_entities: true

The vault_path is the only required setting. All others have
sensible defaults.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from hermes_cli.config import cfg_get

from . import vault as vault_module
from .config_schema import CONFIG_SCHEMA, ProviderConfigSchema, ProviderField

logger = logging.getLogger(__name__)


def _json_serialize(obj: Any) -> Any:
    """Custom JSON serializer for date/datetime and other non-serializable types."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _load_plugin_config() -> dict:
    """Load obsidian_vault config from config.yaml."""
    try:
        from hermes_cli.config import load_config_readonly
        all_config = load_config_readonly()
        return (
            cfg_get(all_config, "plugins", "obsidian_vault", default={}) or {}
        )
    except Exception:
        return {}


def _config_bool(value: Any, default: bool = True) -> bool:
    """Coerce a config value (bool or 'true'/'false' string) to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return default


def _resolve_vault_path(config: dict, hermes_home: str) -> Optional[Path]:
    """Resolve the vault path from config, with env-var expansion."""
    vault_path = config.get("vault_path", "")
    if not vault_path:
        return None
    # Expand environment variables
    vault_path = os.path.expandvars(vault_path)
    # Expand ~
    vault_path = os.path.expanduser(vault_path)
    path = Path(vault_path)
    if not path.is_absolute():
        # Relative paths are relative to hermes_home
        path = Path(hermes_home) / path
    return path


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class ObsidianVaultProvider(MemoryProvider):
    """MemoryProvider backed by an Obsidian Markdown vault.

    Indexes all .md files, parses YAML frontmatter, extracts
    wiki-links and tags, and provides full-text search with
    context-aware ranking.
    """

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or _load_plugin_config()
        # Resolved lazily in initialize() via get_shared_index() so all
        # provider/tool instances for the same vault path share ONE index.
        self._index: Optional[vault_module.VaultIndex] = None
        self._vault_path: Optional[Path] = None
        self._initialized = False
        self._hermes_home = ""

    # -- Core properties -----------------------------------------------

    @property
    def name(self) -> str:
        return "obsidian_vault"

    def is_available(self) -> bool:
        """Return True if vault_path is configured and exists."""
        from hermes_constants import get_hermes_home

        vault_path = self._config.get("vault_path", "")
        if not vault_path:
            return False
        path = Path(vault_path)
        if not path.is_absolute():
            hermes_home = str(get_hermes_home())
            path = Path(hermes_home) / path
        return path.is_dir()

    # -- Lifecycle -----------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        """Initialize the vault index.

        This method returns immediately.  If a persistent cache exists it is
        loaded and a quick incremental scan is performed synchronously.  If no
        cache exists (or it is empty/invalid), a full scan is started in a
        background daemon thread so that Hermes agent initialization is not
        blocked.
        """
        self._hermes_home = kwargs.get("hermes_home", "")
        vault_path = _resolve_vault_path(self._config, self._hermes_home)
        if not vault_path:
            logger.warning("obsidian_vault: no vault_path configured, skipping initialization")
            return

        max_notes = int(self._config.get("max_notes", 10000))
        # Use the process-wide shared index for this vault path so writes
        # performed through one provider object are visible to reads through
        # any other (preserves write->read consistency across instances).
        self._index = vault_module.get_shared_index(vault_path)
        self._index._max_notes = max_notes
        # Honor the `tags_as_categories` config on the shared index so that
        # notes tagged #project-alpha are retrievable via `category:project-alpha`.
        self._index.tags_as_categories = _config_bool(self._config.get("tags_as_categories", True))
        # Background scan by default: never block the agent build thread on a
        # full vault re-index.  If a usable cache is present, the incremental
        # portion still runs synchronously but is expected to be fast.
        count = self._index.scan(vault_path, max_notes=max_notes, background=True)
        self._vault_path = vault_path
        self._initialized = True
        # Note: the actual index build happens on a daemon thread; callers of the
        # provider's tools should check ``is_ready`` and report "starting up"
        # until the background scan finishes.
        logger.info(
            "obsidian_vault: async initialization started from %s (immediate notes: %d; background scan state: %s)",
            vault_path,
            count,
            self._index.scan_state,
        )

    def _wait_for_ready(self, timeout: float = 10.0) -> bool:
        """Test helper: block until the background scan is ready."""
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if self._index and self._index.is_ready:
                return True
            _time.sleep(0.05)
        return False

    def system_prompt_block(self) -> str:
        """Return a static system prompt block with vault info.

        Avoids blocking or returning stale data while the index is still being
        built in the background.
        """
        if not self._initialized or not self._index.is_ready or self._index.is_empty():
            return ""
        stats = self._index.get_stats()
        return (
            f"[Obsidian Vault: {stats['total_notes']} notes indexed "
            f"across {stats['total_categories']} categories "
            f"and {stats['total_tags']} tags. "
            f"Vault: {stats['vault_path']}]"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant vault notes for the upcoming turn.

        Returns nothing until the background index build is ready so that the
        first turn never waits on the vault.
        """
        if not self._initialized or not self._index.is_ready or self._index.is_empty():
            return ""

        results = self._index.search(query, limit=5)

        if not results:
            return ""

        blocks = []
        for note in results:
            # Include title and a snippet of body
            snippet = note.body[:300].strip()
            # Strip markdown formatting for cleaner context
            snippet = re.sub(r"^#+\s*", "", snippet, flags=re.MULTILINE)
            snippet = re.sub(r"^\s*[-*]\s+", "", snippet, flags=re.MULTILINE)
            blocks.append(f"### {note.title}\n{snippet}")

        return "\n\n".join(blocks)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Background prefetch — no-op for this provider (index is in-memory)."""
        pass

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """No-op — vault notes are managed externally by the user."""
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas exposed by this provider."""
        return [
            {
                "name": "vault_search",
                "description": (
                    "Search the Obsidian vault for relevant notes. "
                    "Use this to find stored knowledge, project notes, "
                    "meeting notes, or any information the user has "
                    "written in their vault.\n\n"
                    "Advanced query syntax:\n"
                    "  - \"exact phrase\" (quoted)\n"
                    "  - title:foo, tag:bar, category:baz (field-specific)\n"
                    "  - -exclude terms\n"
                    "  - general terms\n"
                    "Supports sorting by relevance, modified date, or title."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query — keywords, phrases, or advanced syntax.",
                        },
                        "category": {
                            "type": "string",
                            "description": "Filter by note category (from frontmatter).",
                        },
                        "tag": {
                            "type": "string",
                            "description": "Filter by wiki-tag (without the # prefix).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum results (default 20).",
                            "minimum": 1,
                            "maximum": 100,
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Pagination offset (default 0).",
                            "minimum": 0,
                            "default": 0,
                        },
                        "sort_by": {
                            "type": "string",
                            "description": "Sort order: relevance, modified, title (default: relevance).",
                            "enum": ["relevance", "modified", "title"],
                            "default": "relevance",
                        },
                        "semantic": {
                            "type": "boolean",
                            "description": "Use semantic search (embedding similarity) in addition to keyword search.",
                            "default": False,
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "vault_get_note",
                "description": "Retrieve a specific vault note by its slug (filename without .md) or path.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": "Note slug — the filename without the .md extension.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Note path — absolute or relative to vault root. Takes precedence over slug if both provided.",
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "vault_note_context",
                "description": (
                    "Get a note plus the notes it links to, "
                    "providing broader context around a topic."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": "Note slug to expand context for.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Note path — absolute or relative to vault root. Takes precedence over slug if both provided.",
                        },
                        "depth": {
                            "type": "integer",
                            "description": "How many link hops to follow (1-5).",
                            "minimum": 1,
                            "maximum": 5,
                            "default": 2,
                        },
                        "include_backlinks": {
                            "type": "boolean",
                            "description": "Also follow backlinks (notes linking to this note).",
                            "default": True,
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "vault_stats",
                "description": "Get vault index statistics.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "vault_graph_export",
                "description": (
                    "Export the vault's link graph as Mermaid or Graphviz "
                    "(DOT) format. Shows wiki-link connections between notes. "
                    "Useful for visualizing knowledge structure."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "format": {
                            "type": "string",
                            "description": "Output format: mermaid or graphviz (default: mermaid).",
                            "enum": ["mermaid", "graphviz"],
                            "default": "mermaid",
                        },
                        "max_depth": {
                            "type": "integer",
                            "description": "Maximum link depth to traverse (default: 2).",
                            "minimum": 1,
                            "maximum": 5,
                            "default": 2,
                        },
                        "filter_tag": {
                            "type": "string",
                            "description": "Only include notes with this tag.",
                        },
                    },
                },
            },
            {
                "name": "vault_dedup",
                "description": (
                    "Find potential duplicate/similar notes in the vault. "
                    "Uses embedding similarity to detect near-duplicates. "
                    "Returns clusters of similar notes with similarity scores."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "threshold": {
                            "type": "string",
                            "description": "Similarity threshold (0.0-1.0). Notes with similarity above this are flagged as duplicates. Default: 0.85.",
                            "default": "0.85",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of duplicate pairs to return.",
                            "minimum": 1,
                            "maximum": 100,
                            "default": 50,
                        },
                    },
                },
            },
            {
                "name": "vault_create_note",
                "description": "Create a new note in the vault.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Note title (used for filename).",
                        },
                        "body": {
                            "type": "string",
                            "description": "Markdown body content.",
                            "default": "",
                        },
                        "frontmatter": {
                            "type": "object",
                            "description": "YAML frontmatter as key-value pairs.",
                            "default": {},
                        },
                        "tags": {
                            "type": "array",
                            "description": "List of tags.",
                            "items": {"type": "string"},
                            "default": [],
                        },
                        "category": {
                            "type": "string",
                            "description": "Note category.",
                        },
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "vault_append_note",
                "description": "Append content to an existing note.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": "Note slug to append to.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Note path — absolute or relative to vault root. Takes precedence over slug if both provided.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Content to append.",
                        },
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "vault_update_note",
                "description": "Update an existing note's content, frontmatter, or metadata.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": "Note slug to update.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Note path — absolute or relative to vault root. Takes precedence over slug if both provided.",
                        },
                        "title": {
                            "type": "string",
                            "description": "New title.",
                        },
                        "body": {
                            "type": "string",
                            "description": "New body content.",
                        },
                        "frontmatter": {
                            "type": "object",
                            "description": "Replace frontmatter.",
                        },
                        "tags": {
                            "type": "array",
                            "description": "New tags list.",
                            "items": {"type": "string"},
                        },
                        "category": {
                            "type": "string",
                            "description": "New category.",
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "vault_related_notes",
                "description": "Find semantically related notes using embedding similarity.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": "Note slug to find related notes for.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Note path — absolute or relative to vault root. Takes precedence over slug if both provided.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum results (default 10).",
                            "minimum": 1,
                            "maximum": 50,
                            "default": 10,
                        },
                        "min_similarity": {
                            "type": "number",
                            "description": "Minimum similarity score (default 0.1).",
                            "default": 0.1,
                        },
                        "exclude_wikilinks": {
                            "type": "boolean",
                            "description": "Exclude notes already linked via wiki-links.",
                            "default": False,
                        },
                    },
                    "required": [],
                },
            },
            {
                "name": "vault_delete_note",
                "description": "Delete a note from the vault by slug or path (case-insensitive).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slug": {
                            "type": "string",
                            "description": "Note slug to delete (case-insensitive)."
                        },
                        "path": {
                            "type": "string",
                            "description": "Note path — absolute or relative to vault root. Takes precedence over slug if both provided.",
                        }
                    },
                    "required": [],
                },
            },
            {
                "name": "vault_validate",
                "description": "Validate the vault for inconsistencies (broken links, duplicate slugs, missing metadata).",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "vault_orphans",
                "description": "Find orphan and weakly connected notes.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "vault_enhanced_stats",
                "description": "Get detailed vault analytics (tags, categories, growth, connections).",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "vault_graph_analytics",
                "description": "Compute graph analytics (PageRank, centrality, connected components).",
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        """Handle vault tool calls."""
        if not self._index.is_ready:
            return json.dumps({"error": "Vault index is still starting up, please wait."})

        if tool_name == "vault_search":
            return self._handle_search(args)
        elif tool_name == "vault_get_note":
            return self._handle_get_note(args)
        elif tool_name == "vault_note_context":
            return self._handle_note_context(args)
        elif tool_name == "vault_stats":
            return self._handle_stats(args)
        elif tool_name == "vault_graph_export":
            return self._handle_graph_export(args)
        elif tool_name == "vault_dedup":
            return self._handle_dedup(args)
        elif tool_name == "vault_delete_note":
            return self._handle_delete_note(args)
        elif tool_name == "vault_create_note":
            return self._handle_create_note(args)
        elif tool_name == "vault_append_note":
            return self._handle_append_note(args)
        elif tool_name == "vault_update_note":
            return self._handle_update_note(args)
        elif tool_name == "vault_related_notes":
            return self._handle_related_notes(args)
        elif tool_name == "vault_validate":
            return self._handle_validate(args)
        elif tool_name == "vault_orphans":
            return self._handle_orphans(args)
        elif tool_name == "vault_enhanced_stats":
            return self._handle_enhanced_stats(args)
        elif tool_name == "vault_graph_analytics":
            return self._handle_graph_analytics(args)
        raise NotImplementedError(f"obsidian_vault does not handle tool {tool_name}")

    def _handle_search(self, args: Dict[str, Any]) -> str:
        if not self._index.is_ready:
            return json.dumps({"error": "Vault index is still starting up, please wait."})

        query = args.get("query", "")
        category = args.get("category")
        tag = args.get("tag")
        limit = int(args.get("limit", 20))
        offset = int(args.get("offset", 0))
        sort_by = args.get("sort_by", "relevance")
        semantic = args.get("semantic", False)

        if not query and not tag and not category:
            return json.dumps({"error": "Provide a 'query', 'tag', or 'category' to search."})

        # Auto-refresh before search
        if self._index._vault_path:
            self._index._check_and_refresh(self._index._vault_path)
        
        tags = [tag] if tag else None
        results = self._index.search(
            query,
            category=category,
            tags=tags,
            limit=limit,
            offset=offset,
            sort_by=sort_by,
            semantic=semantic,
        )
        
        # Handle both old list format and new dict format from HybridSearcher
        if isinstance(results, dict):
            return json.dumps(results, default=_json_serialize)
        else:
            return json.dumps({
                "query": query,
                "count": len(results),
                "offset": offset,
                "sort_by": sort_by,
                "results": [
                    {
                        "slug": n.slug,
                        "title": n.title,
                        "category": n.category,
                        "tags": n.tags,
                        "path": str(n.path),
                        "snippet": n.body[:200].strip(),
                    }
                    for n in results
                ],
            }, default=_json_serialize)

    def _handle_get_note(self, args: Dict[str, Any]) -> str:
        slug = args.get("slug", "")
        path = args.get("path", "")
        note = self._resolve_note(slug=slug if slug else None, path=path if path else None)
        if not note:
            identifier = path if path else slug
            return json.dumps({"error": f"Note '{identifier}' not found in vault."})
        return json.dumps({
            "slug": note.slug,
            "title": note.title,
            "category": note.category,
            "tags": note.tags,
            "path": str(note.path),
            "frontmatter": note.frontmatter,
            "body": note.body,
        }, default=_json_serialize)

    def _handle_note_context(self, args: Dict[str, Any]) -> str:
        slug = args.get("slug", "")
        path = args.get("path", "")
        depth = int(args.get("depth", 2))
        include_backlinks = args.get("include_backlinks", True)
        note = self._resolve_note(slug=slug if slug else None, path=path if path else None)
        if not note:
            identifier = path if path else slug
            return json.dumps({"error": f"Note '{identifier}' not found in vault."})
        context = self._index.get_link_context(note.slug, depth=depth, include_backlinks=include_backlinks)
        return json.dumps({
            "slug": note.slug,
            "title": note.title,
            "body": note.body,
            "linked_notes": [
                {
                    "slug": n.slug,
                    "title": n.title,
                    "category": n.category,
                    "tags": n.tags,
                }
                for n in context
            ],
        }, default=_json_serialize)

    def _handle_stats(self, args: Dict[str, Any]) -> str:
        return json.dumps(self._index.get_stats(), default=_json_serialize)

    def _resolve_note(self, slug: Optional[str] = None, path: Optional[str] = None) -> Optional[VaultNote]:
        """Resolve a note by slug or path. If both provided, path takes precedence."""
        if path:
            # Path can be absolute or relative to vault
            path_obj = Path(path)
            if path_obj.is_absolute():
                if self._vault_path:
                    try:
                        rel_path = path_obj.relative_to(self._vault_path)
                        slug_candidate = rel_path.with_suffix('').as_posix()
                    except ValueError:
                        slug_candidate = path_obj.stem
                else:
                    slug_candidate = path_obj.stem
            else:
                # Relative path without slug - try to derive slug from path
                slug_candidate = path_obj.stem
        elif slug:
            slug_candidate = slug
        else:
            return None

        # Try exact slug match first
        note = self._index.get_note(slug_candidate)
        if note:
            return note

        # Try slugified version
        slugified = slug_candidate.lower().replace(" ", "-")
        note = self._index.get_note(slugified)
        if note:
            return note

        # Try path-based lookup (for nested paths)
        if path:
            for k, n in self._index._notes.items():
                if str(n.path) == str(Path(path).resolve()) or n.path.as_posix().endswith(path):
                    return n
        return None

    def _handle_graph_export(self, args: Dict[str, Any]) -> str:
        fmt = args.get("format", "mermaid")
        max_depth = int(args.get("max_depth", 2))
        filter_tag = args.get("filter_tag")

        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})

        notes = self._index.get_all_notes()
        if filter_tag:
            notes = [n for n in notes if filter_tag in n.tags]

        if fmt == "graphviz":
            lines = ["digraph vault {"]
            lines.append("  rankdir=LR;")
            lines.append('  node [shape=box, style="rounded"];')
            visited = set()
            for note in notes:
                if note.slug not in visited:
                    visited.add(note.slug)
                    safe_slug = note.slug.replace('"', '\\"')
                    safe_title = note.title.replace('"', '\\"')
                    lines.append(f'  "{safe_slug}" [label="{safe_title}"];')
                for link in note.links:
                    if link in self._index._notes or link.lower().replace(" ", "-") in self._index._notes:
                        target = link.lower().replace(" ", "-") if link.lower().replace(" ", "-") in self._index._notes else None
                        if target and target not in visited:
                            visited.add(target)
                            safe_t = self._index._notes[target].title.replace('"', '\\"')
                            lines.append(f'  "{target}" [label="{safe_t}"];')
                        if target:
                            lines.append(f'  "{note.slug}" -> "{target}";')
            lines.append("}")
            return "\n".join(lines)
        else:
            # Mermaid format
            lines = ["graph TD"]
            # Collect nodes
            nodes = {}
            edges = set()
            visited = set()

            def collect(node_slug, depth):
                if depth > max_depth or node_slug in visited:
                    return
                visited.add(node_slug)
                note = self._index._notes.get(node_slug)
                if not note:
                    # Try slugified version
                    for k in self._index._notes:
                        if k == node_slug or k == node_slug.lower().replace(" ", "-"):
                            note = self._index._notes[k]
                            node_slug = k
                            break
                if not note:
                    return
                safe_slug = node_slug.replace(" ", "_").replace("-", "_")
                nodes[safe_slug] = note.title
                for link in note.links:
                    target_slug = link.lower().replace(" ", "-")
                    if target_slug in self._index._notes:
                        safe_target = target_slug.replace(" ", "_").replace("-", "_")
                        edges.add((safe_slug, safe_target))
                        collect(target_slug, depth + 1)

            for note in notes:
                collect(note.slug, 0)

            for slug, title in nodes.items():
                safe_title = title.replace('"', '\\"')
                lines.append(f'    {slug}["{safe_title}"]')
            for src, dst in sorted(edges):
                lines.append(f"    {src} --> {dst}")
            return "\n".join(lines)

    def _handle_dedup(self, args: Dict[str, Any]) -> str:
            threshold = float(args.get("threshold", 0.85))
            limit = int(args.get("limit", 50))

            if not self._initialized:
                return json.dumps({"error": "Index not initialized."})

            notes = list(self._index._notes.values())
            duplicates = []
            checked = set()

            for i, note_a in enumerate(notes):
                if i >= len(notes) - 1:
                    break
                for note_b in notes[i+1:]:
                    # Case-insensitive check for duplicates
                    if note_a.slug.casefold() == note_b.slug.casefold():
                        continue
                    pair_key = (note_a.slug, note_b.slug)
                    if pair_key in checked:
                        continue
                    checked.add(pair_key)

                    if not vault_module._has_embedding(note_a.embedding) or not vault_module._has_embedding(note_b.embedding):
                        # Fall back to title similarity if no embeddings
                        title_sim = 1.0 if note_a.title.casefold() == note_b.title.casefold() else 0.0
                        if title_sim >= threshold:
                            duplicates.append({
                                "note_a": {
                                    "slug": note_a.slug,
                                    "title": note_a.title,
                                    "path": str(note_a.path),
                                },
                                "note_b": {
                                    "slug": note_b.slug,
                                    "title": note_b.title,
                                    "path": str(note_b.path),
                                },
                                "similarity": round(1.0, 4),
                            })
                        continue

                    from plugins.memory.obsidian_vault.vault import cosine_similarity
                    sim = cosine_similarity(note_a.embedding, note_b.embedding)

                    if sim >= threshold:
                        duplicates.append({
                            "note_a": {
                                "slug": note_a.slug,
                                "title": note_a.title,
                                "path": str(note_a.path),
                            },
                            "note_b": {
                                "slug": note_b.slug,
                                "title": note_b.title,
                                "path": str(note_b.path),
                            },
                            "similarity": round(sim, 4),
                        })

                    if len(duplicates) >= limit:
                        break

                if len(duplicates) >= limit:
                    break

            # Sort by similarity descending
            duplicates.sort(key=lambda x: -x["similarity"])

            return json.dumps({
                "threshold": threshold,
                "count": len(duplicates),
                "duplicates": duplicates,
            }, default=_json_serialize)

    def _handle_delete_note(self, args: Dict[str, Any]) -> str:
        slug = args.get("slug", "")
        path = args.get("path", "")
        note = self._resolve_note(slug=slug if slug else None, path=path if path else None)
        if not note:
            identifier = path if path else slug
            return json.dumps({"error": f"Note '{identifier}' not found in vault."})

        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})

        success = self._index.delete_note(note.slug)
        if success:
            return json.dumps({"success": True, "deleted_slug": note.slug})
        return json.dumps({"error": "Failed to delete note"})

    # -- Handler methods for new tools ------------------------------------

    def _handle_create_note(self, args: Dict[str, Any]) -> str:
        title = args.get("title", "")
        body = args.get("body", "")
        frontmatter = args.get("frontmatter", {})
        tags = args.get("tags", [])
        category = args.get("category")
        vault_path = args.get("vault_path")

        if not title:
            return json.dumps({"error": "title is required"})

        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})

        note = self._index.create_note(
            title=title,
            body=body,
            frontmatter=frontmatter,
            tags=tags,
            category=category,
        )

        if note:
            return json.dumps({
                "slug": note.slug,
                "title": note.title,
                "path": str(note.path),
            }, default=_json_serialize)
        return json.dumps({"error": "Failed to create note"})

    def _handle_append_note(self, args: Dict[str, Any]) -> str:
        slug = args.get("slug", "")
        path = args.get("path", "")
        content = args.get("content", "")

        note = self._resolve_note(slug=slug if slug else None, path=path if path else None)
        if not note:
            identifier = path if path else slug
            return json.dumps({"error": f"Note '{identifier}' not found in vault."})

        if not content:
            return json.dumps({"error": "content is required"})

        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})

        success = self._index.append_to_note(note.slug, content)
        return json.dumps({"success": success})

    def _handle_update_note(self, args: Dict[str, Any]) -> str:
        slug = args.get("slug", "")
        path = args.get("path", "")
        title = args.get("title")
        body = args.get("body")
        frontmatter = args.get("frontmatter")
        tags = args.get("tags")
        category = args.get("category")

        note = self._resolve_note(slug=slug if slug else None, path=path if path else None)
        if not note:
            identifier = path if path else slug
            return json.dumps({"error": f"Note '{identifier}' not found in vault."})

        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})

        note = self._index.update_note(
            slug=note.slug,
            title=title,
            body=body,
            frontmatter=frontmatter,
            tags=tags,
            category=category,
        )

        if note:
            return json.dumps({
                "slug": note.slug,
                "title": note.title,
                "path": str(note.path),
            }, default=_json_serialize)
        return json.dumps({"error": "Note not found or failed to update"})

    def _handle_related_notes(self, args: Dict[str, Any]) -> str:
        slug = args.get("slug", "")
        path = args.get("path", "")
        limit = int(args.get("limit", 10))
        min_similarity = float(args.get("min_similarity", 0.1))
        exclude_wikilinks = args.get("exclude_wikilinks", False)

        note = self._resolve_note(slug=slug if slug else None, path=path if path else None)
        if not note:
            identifier = path if path else slug
            return json.dumps({"error": f"Note '{identifier}' not found in vault."})

        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})

        results = self._index.related_notes(
            slug=note.slug,
            limit=limit,
            min_similarity=min_similarity,
            exclude_wikilinks=exclude_wikilinks,
        )

        return json.dumps({
            "count": len(results),
            "results": [
                {
                    "slug": n.slug,
                    "title": n.title,
                    "category": n.category,
                    "tags": n.tags,
                    "similarity": round(sim, 4),
                }
                for n, sim in results
            ],
        }, default=_json_serialize)

    def _handle_validate(self, args: Dict[str, Any]) -> str:
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})

        result = self._index.validate()
        return json.dumps(result, default=_json_serialize)

    def _handle_orphans(self, args: Dict[str, Any]) -> str:
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})

        result = self._index.find_orphans()
        return json.dumps(result, default=_json_serialize)

    def _handle_enhanced_stats(self, args: Dict[str, Any]) -> str:
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})

        result = self._index.get_enhanced_stats()
        return json.dumps(result, default=_json_serialize)

    def _handle_graph_analytics(self, args: Dict[str, Any]) -> str:
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})

        result = self._index.get_graph_analytics()
        return json.dumps(result, default=_json_serialize)

    # -- Optional hooks ------------------------------------------------

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """End of session — save index cache to disk."""
        if self._initialized and self._index:
            self._index.flush()

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        """Session switch — re-index if vault changed."""
        if self._vault_path and self._vault_path.is_dir():
            max_notes = int(self._config.get("max_notes", 10000))
            self._index.scan(self._vault_path, max_notes=max_notes)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror built-in memory writes to the vault if index_on_write is set."""
        if not self._initialized:
            return
        if not self._config.get("index_on_write", True):
            return
        # When the built-in memory tool writes, we add a note to the vault
        # if the vault path is configured. This keeps the vault in sync
        # with the agent's memory.
        _write_memory_note(action, target, content, self._vault_path, metadata)

    def backup_paths(self) -> List[str]:
        """Return vault path for backup inclusion."""
        if self._vault_path and self._vault_path.is_dir():
            return [str(self._vault_path)]
        return []

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Return the declarative config schema as a list of field dicts.

        Derived from ``CONFIG_SCHEMA`` in ``config_schema.py`` so the two never
        diverge (the previous hardcoded 3-field list disagreed with the 7-field
        declarative schema — see audit A10).
        """
        schema_fields: List[Dict[str, Any]] = []
        for f in CONFIG_SCHEMA.fields:
            field_dict: Dict[str, Any] = {
                "key": f.key,
                "label": f.label,
                "kind": f.kind,
                "description": f.description,
                "default": f.default,
                "placeholder": f.placeholder,
                "scope": f.scope,
                "inline": f.inline,
                "group": f.group,
            }
            if f.options:
                field_dict["options"] = [
                    {"value": o.value, "label": o.label, "description": o.description}
                    for o in f.options
                ]
            if f.key == "vault_path":
                field_dict["required"] = True
            schema_fields.append(field_dict)
        return schema_fields

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """No-op — config is stored in config.yaml via the standard mechanism."""
        pass


# ---------------------------------------------------------------------------
# Helper: write memory notes to vault
# ---------------------------------------------------------------------------

def _write_memory_note(
    action: str,
    target: str,
    content: str,
    vault_path: Optional[Path],
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a memory entry as a note in the vault."""
    if not vault_path:
        return
    try:
        mem_dir = vault_path / ".hermes_memories"
        mem_dir.mkdir(exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        note_file = mem_dir / f"memory-{today}.md"

        frontmatter_lines = [
            "---",
            f"title: Memory — {action} {target}",
            f"category: memory",
            f"date: {today}",
        ]
        if metadata:
            for k, v in metadata.items():
                frontmatter_lines.append(f"{k}: {v}")
        frontmatter_lines.append("---")
        frontmatter_lines.append("")

        entry = "\n".join(frontmatter_lines)
        entry += f"- **{action}** {target}: {content}\n"

        if note_file.exists():
            existing = note_file.read_text(encoding="utf-8")
            entry = existing + "\n" + entry

        note_file.write_text(entry, encoding="utf-8")
    except Exception as e:
        logger.debug("Failed to write memory note to vault: %s", e)


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Register the obsidian_vault memory provider with the plugin system."""
    provider = ObsidianVaultProvider(config=_load_plugin_config())
    ctx.register_memory_provider(provider)