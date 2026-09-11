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
    # numpy scalars (e.g. float32 similarity scores) -> Python float
    if hasattr(obj, "item") and not isinstance(obj, str):
        try:
            return obj.item()
        except Exception:
            pass
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


def _resolve_path(path_str: str, hermes_home: str) -> Optional[Path]:
    """Resolve a path string, expanding env vars, ~, and relative paths."""
    if not path_str:
        return None
    path_str = os.path.expandvars(path_str)
    path_str = os.path.expanduser(path_str)
    path = Path(path_str)
    if not path.is_absolute():
        path = Path(hermes_home) / path
    return path


def _load_vaults(config: dict, hermes_home: str) -> Dict[str, Path]:
    """Load named vaults from config.

    Supports:
    - New multi-vault: config['vaults'] is a list of dicts with 'name' and 'path'
    - Legacy single-vault: config['vault_path'] creates a 'default' vault

    Returns a dict mapping vault name -> resolved Path.
    """
    vaults: Dict[str, Path] = {}

    # Multi-vault config
    raw_vaults = config.get("vaults", [])
    if isinstance(raw_vaults, list):
        for entry in raw_vaults:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name", "").strip()
            path_str = entry.get("path", "")
            if not name or not path_str:
                continue
            if name in vaults:
                logger.warning("Duplicate vault name '%s' in config, skipping", name)
                continue
            resolved = _resolve_path(path_str, hermes_home)
            if resolved and resolved.is_dir():
                vaults[name] = resolved
            else:
                logger.warning("Vault '%s' path does not exist or is not a directory: %s", name, path_str)

    # Legacy single vault
    legacy_path = _resolve_vault_path(config, hermes_home)
    if legacy_path and legacy_path.is_dir():
        if "default" not in vaults:
            vaults["default"] = legacy_path

    return vaults


def _save_config_to_yaml(config: dict, hermes_home: str) -> bool:
    """Write the obsidian_vault section back to config.yaml.

    Performs an atomic-ish write by writing to a temp file then renaming.
    """
    try:
        from hermes_cli.config import load_config, save_config
    except Exception:
        logger.warning("Cannot import hermes_cli config helpers, skipping save_config")
        return False
    try:
        all_config = load_config()
        if "plugins" not in all_config or not isinstance(all_config["plugins"], dict):
            all_config["plugins"] = {}
        if "obsidian_vault" not in all_config["plugins"] or not isinstance(all_config["plugins"]["obsidian_vault"], dict):
            all_config["plugins"]["obsidian_vault"] = {}

        plugin_config = all_config["plugins"]["obsidian_vault"]
        plugin_config["vaults"] = config.get("vaults", [])
        plugin_config["active_vault"] = config.get("active_vault")
        # Do not overwrite legacy vault_path if it exists and no vaults list yet
        if "vault_path" in config:
            plugin_config["vault_path"] = config["vault_path"]
        save_config(all_config)
        return True
    except Exception as e:
        logger.error("Failed to save obsidian_vault config: %s", e)
        return False


def _pick_active_vault(vaults: Dict[str, Path], config: dict) -> Optional[str]:
    """Pick the active vault name from config or default to first available."""
    active = config.get("active_vault", "").strip()
    if active and active in vaults:
        return active
    if vaults:
        return next(iter(vaults.keys()))
    return None


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

def _mk_schema(name: str, description: str, properties: Optional[Dict[str, Any]] = None, required: Optional[List[str]] = None) -> Dict[str, Any]:
    """Build a tool schema dict."""
    schema: Dict[str, Any] = {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
        },
    }
    if required:
        schema["parameters"]["required"] = required
    return schema


def _get_state_dir(hermes_home: str) -> Path:
    """Return the directory used to persist obsidian_vault runtime state."""
    return Path(hermes_home) / ".obsidian_vault_state"


def _load_active_vault_state(hermes_home: str) -> Optional[str]:
    """Load the previously persisted active vault name."""
    if not hermes_home:
        return None
    state_file = _get_state_dir(hermes_home) / "active_vault.json"
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        return data.get("active_vault")
    except Exception as e:
        logger.warning("Failed to load active vault state: %s", e)
        return None


def _save_active_vault_state(hermes_home: str, active_vault: Optional[str]) -> None:
    """Persist the active vault name to disk."""
    if not hermes_home:
        return
    try:
        state_dir = _get_state_dir(hermes_home)
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / "active_vault.json"
        state_file.write_text(json.dumps({"active_vault": active_vault}), encoding="utf-8")
    except Exception as e:
        logger.warning("Failed to save active vault state: %s", e)


class ObsidianVaultProvider(MemoryProvider):
    """MemoryProvider backed by one or more Obsidian Markdown vaults."""

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or _load_plugin_config()
        self._vaults: Dict[str, vault_module.VaultIndex] = {}
        self._vault_paths: Dict[str, Path] = {}
        self._active_vault: Optional[str] = None
        self._initialized = False
        self._hermes_home = ""

    @property
    def name(self) -> str:
        return "obsidian_vault"

    def is_available(self) -> bool:
        from hermes_constants import get_hermes_home
        return len(_load_vaults(self._config, str(get_hermes_home()))) > 0

    def initialize(self, session_id: str, **kwargs) -> None:
        self._hermes_home = kwargs.get("hermes_home", "")
        vaults = _load_vaults(self._config, self._hermes_home)
        if not vaults:
            logger.warning("obsidian_vault: no vaults configured, skipping initialization")
            return

        max_notes = int(self._config.get("max_notes", 10000))
        active_name = _pick_active_vault(vaults, self._config)

        # Restore previously persisted active vault if still valid
        persisted_active = _load_active_vault_state(self._hermes_home)
        if persisted_active and persisted_active in vaults:
            active_name = persisted_active
            logger.info("obsidian_vault: restored active vault from state: %s", active_name)

        for name, vault_path in vaults.items():
            index = vault_module.get_shared_index(vault_path)
            index._max_notes = max_notes
            index.tags_as_categories = _config_bool(self._config.get("tags_as_categories", True))
            count = index.scan(vault_path, max_notes=max_notes, background=True)
            self._vaults[name] = index
            self._vault_paths[name] = vault_path
            logger.info(
                "obsidian_vault: started async init for vault '%s' at %s (immediate notes: %d; state: %s)",
                name,
                vault_path,
                count,
                index.scan_state,
            )

        self._active_vault = active_name
        self._initialized = True

    def _get_index(self, vault_name: Optional[str] = None) -> Optional[vault_module.VaultIndex]:
        if vault_name:
            return self._vaults.get(vault_name)
        if self._active_vault and self._active_vault in self._vaults:
            return self._vaults[self._active_vault]
        if self._vaults:
            return next(iter(self._vaults.values()))
        return None

    def _get_vault_path(self, vault_name: Optional[str] = None) -> Optional[Path]:
        if vault_name:
            return self._vault_paths.get(vault_name)
        if self._active_vault and self._active_vault in self._vault_paths:
            return self._vault_paths[self._active_vault]
        if self._vault_paths:
            return next(iter(self._vault_paths.values()))
        return None

    def switch_vault(self, name: str) -> bool:
        if name in self._vaults:
            self._active_vault = name
            _save_active_vault_state(self._hermes_home, self._active_vault)
            logger.info("obsidian_vault: switched active vault to '%s'", name)
            return True
        logger.warning("obsidian_vault: cannot switch to unknown vault '%s'", name)
        return False

    def _add_vault(self, name: str, path_str: str, *, activate: bool = True) -> Dict[str, Any]:
        """Register and scan a new vault, optionally writing it to config."""
        if not name:
            return {"error": "Vault name is required."}
        if not path_str:
            return {"error": "Vault path is required."}
        if name in self._vaults:
            return {"error": f"Vault '{name}' already exists."}
        path = _resolve_path(path_str, self._hermes_home)
        if not path:
            return {"error": f"Invalid vault path: {path_str}"}
        if not path.is_dir():
            try:
                path.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                return {"error": f"Failed to create vault directory: {e}"}
        max_notes = int(self._config.get("max_notes", 10000))
        index = vault_module.get_shared_index(path)
        index._max_notes = max_notes
        index.tags_as_categories = _config_bool(self._config.get("tags_as_categories", True))
        index.scan(path, max_notes=max_notes, background=True)
        self._vaults[name] = index
        self._vault_paths[name] = path
        if activate or self._active_vault is None:
            self._active_vault = name
            _save_active_vault_state(self._hermes_home, name)
        return {"success": True, "name": name, "path": str(path)}

    def _remove_vault(self, name: str) -> Dict[str, Any]:
        """Remove a vault from provider state. Does not delete data."""
        if name not in self._vaults:
            return {"error": f"Vault '{name}' not found."}
        index = self._vaults.pop(name, None)
        self._vault_paths.pop(name, None)
        if index and hasattr(index, "flush"):
            try:
                index.flush()
            except Exception as e:
                logger.warning("Failed to flush vault index for '%s': %s", name, e)
        if self._active_vault == name:
            if self._vaults:
                self._active_vault = next(iter(self._vaults.keys()))
            else:
                self._active_vault = None
            _save_active_vault_state(self._hermes_home, self._active_vault)
        return {"success": True, "active_vault": self._active_vault}

    def reload_vaults(self) -> Dict[str, Any]:
        """Reload vault list from config and scan."""
        vaults = _load_vaults(self._config, self._hermes_home)
        if not vaults:
            return {"error": "No vaults configured."}
        max_notes = int(self._config.get("max_notes", 10000))
        active_name = _pick_active_vault(vaults, self._config)
        new_vaults: Dict[str, vault_module.VaultIndex] = {}
        new_paths: Dict[str, Path] = {}
        for name, vault_path in vaults.items():
            index = vault_module.get_shared_index(vault_path)
            index._max_notes = max_notes
            index.tags_as_categories = _config_bool(self._config.get("tags_as_categories", True))
            index.scan(vault_path, max_notes=max_notes, background=True)
            new_vaults[name] = index
            new_paths[name] = vault_path
        self._vaults = new_vaults
        self._vault_paths = new_paths
        self._active_vault = active_name
        self._initialized = True
        return {"success": True, "vaults": list(self._vaults.keys()), "active_vault": self._active_vault}

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> bool:
        """Persist vault configuration to config.yaml."""
        try:
            return _save_config_to_yaml(values, hermes_home)
        except Exception as e:
            logger.error("save_config failed: %s", e)
            return False

    def _wait_for_ready(self, timeout: float = 10.0, vault_name: Optional[str] = None) -> bool:
        import time as _time
        index = self._get_index(vault_name)
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if index and index.is_ready:
                return True
            _time.sleep(0.05)
        return False

    def system_prompt_block(self) -> str:
        index = self._get_index()
        if not self._initialized or not index or not index.is_ready or index.is_empty():
            return ""
        stats = index.get_stats()
        label = self._active_vault or "default"
        return (
            f"[Obsidian Vault '{label}': {stats['total_notes']} notes indexed "
            f"across {stats['total_categories']} categories "
            f"and {stats['total_tags']} tags. "
            f"Vault: {stats['vault_path']}]"
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        index = self._get_index()
        if not self._initialized or not index or not index.is_ready or index.is_empty():
            return ""
        results = index.search(query, limit=5)
        if not results:
            return ""
        blocks = []
        for note in results:
            snippet = note.body[:300].strip()
            snippet = re.sub(r"^#+\s*", "", snippet, flags=re.MULTILINE)
            snippet = re.sub(r"^\s*[-*]\s+", "", snippet, flags=re.MULTILINE)
            blocks.append(f"### {note.title}\n{snippet}")
        return "\n\n".join(blocks)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None) -> None:
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            _mk_schema("vault_list_vaults", "List all configured vaults with names, paths, and scan states."),
            _mk_schema("vault_switch", "Switch the active vault by name.", {"name": {"type": "string", "description": "Name of the vault to activate."}}, required=["name"]),
            _mk_schema("vault_create_vault", "Create a new Obsidian vault directory and initialize its index.", {
                "name": {"type": "string", "description": "Name for the new vault."},
                "path": {"type": "string", "description": "Directory path for the new vault. Created if it does not exist."},
                "activate": {"type": "boolean", "description": "Whether to activate this vault after creation.", "default": True},
            }, required=["name", "path"]),
            _mk_schema("vault_search", "Search the Obsidian vault for relevant notes.", {
                "query": {"type": "string", "description": "Search query — keywords, phrases, or advanced syntax."},
                "category": {"type": "string", "description": "Filter by note category."},
                "tag": {"type": "string", "description": "Filter by wiki-tag (without the # prefix)."},
                "limit": {"type": "integer", "description": "Maximum results (default 20).", "minimum": 1, "maximum": 100},
                "offset": {"type": "integer", "description": "Pagination offset (default 0).", "minimum": 0, "default": 0},
                "sort_by": {"type": "string", "description": "Sort order: relevance, modified, title (default: relevance).", "enum": ["relevance", "modified", "title"], "default": "relevance"},
                "semantic": {"type": "boolean", "description": "Use semantic search.", "default": False},
                "vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."},
            }, required=["query"]),
            _mk_schema("vault_get_note", "Retrieve a specific vault note by its slug or path.", {
                "slug": {"type": "string", "description": "Note slug — the filename without the .md extension."},
                "path": {"type": "string", "description": "Note path — absolute or relative to vault root. Takes precedence over slug if both provided."},
                "vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."},
            }),
            _mk_schema("vault_note_context", "Get a note plus the notes it links to.", {
                "slug": {"type": "string", "description": "Note slug to expand context for."},
                "path": {"type": "string", "description": "Note path — absolute or relative to vault root. Takes precedence over slug."},
                "depth": {"type": "integer", "description": "How many link hops to follow (1-5).", "minimum": 1, "maximum": 5, "default": 2},
                "include_backlinks": {"type": "boolean", "description": "Also follow backlinks.", "default": True},
                "vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."},
            }),
            _mk_schema("vault_stats", "Get vault index statistics.", {"vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."}}),
            _mk_schema("vault_graph_export", "Export the vault's link graph as Mermaid or Graphviz.", {
                "format": {"type": "string", "description": "Output format: mermaid or graphviz.", "enum": ["mermaid", "graphviz"], "default": "mermaid"},
                "max_depth": {"type": "integer", "description": "Maximum link depth.", "minimum": 1, "maximum": 5, "default": 2},
                "filter_tag": {"type": "string", "description": "Only include notes with this tag."},
                "vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."},
            }),
            _mk_schema("vault_dedup", "Find potential duplicate/similar notes.", {
                "threshold": {"type": "string", "description": "Similarity threshold (0.0-1.0).", "default": "0.85"},
                "limit": {"type": "integer", "description": "Maximum duplicate pairs.", "minimum": 1, "maximum": 100, "default": 50},
                "vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."},
            }),
            _mk_schema("vault_create_note", "Create a new note in the vault.", {
                "title": {"type": "string", "description": "Note title (used for filename)."},
                "body": {"type": "string", "description": "Markdown body content.", "default": ""},
                "frontmatter": {"type": "object", "description": "YAML frontmatter.", "default": {}},
                "tags": {"type": "array", "description": "List of tags.", "items": {"type": "string"}, "default": []},
                "category": {"type": "string", "description": "Note category."},
                "vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."},
            }, required=["title"]),
            _mk_schema("vault_append_note", "Append content to an existing note.", {
                "slug": {"type": "string", "description": "Note slug to append to."},
                "path": {"type": "string", "description": "Note path. Takes precedence over slug."},
                "content": {"type": "string", "description": "Content to append."},
                "vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."},
            }, required=["content"]),
            _mk_schema("vault_update_note", "Update an existing note.", {
                "slug": {"type": "string", "description": "Note slug to update."},
                "path": {"type": "string", "description": "Note path. Takes precedence over slug."},
                "title": {"type": "string", "description": "New title."},
                "body": {"type": "string", "description": "New body content."},
                "frontmatter": {"type": "object", "description": "Replace frontmatter."},
                "tags": {"type": "array", "description": "New tags list.", "items": {"type": "string"}},
                "category": {"type": "string", "description": "New category."},
                "vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."},
            }),
            _mk_schema("vault_related_notes", "Find semantically related notes.", {
                "slug": {"type": "string", "description": "Note slug."},
                "path": {"type": "string", "description": "Note path. Takes precedence over slug."},
                "limit": {"type": "integer", "description": "Maximum results.", "minimum": 1, "maximum": 50, "default": 10},
                "min_similarity": {"type": "number", "description": "Minimum similarity score.", "default": 0.1},
                "exclude_wikilinks": {"type": "boolean", "description": "Exclude notes already linked via wiki-links.", "default": False},
                "vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."},
            }),
            _mk_schema("vault_delete_note", "Delete a note from the vault.", {
                "slug": {"type": "string", "description": "Note slug to delete."},
                "path": {"type": "string", "description": "Note path. Takes precedence over slug."},
                "vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."},
            }),
            _mk_schema("vault_validate", "Validate the vault for inconsistencies.", {"vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."}}),
            _mk_schema("vault_orphans", "Find orphan and weakly connected notes.", {"vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."}}),
            _mk_schema("vault_enhanced_stats", "Get detailed vault analytics.", {"vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."}}),
            _mk_schema("vault_graph_analytics", "Compute graph analytics.", {"vault_name": {"type": "string", "description": "Optional vault name. Defaults to the active vault."}}),
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "vault_list_vaults":
            return self._handle_list_vaults(args)
        elif tool_name == "vault_switch":
            return self._handle_switch(args)
        elif tool_name == "vault_create_vault":
            return self._handle_create_vault(args)

        vault_name = args.get("vault_name")
        index = self._get_index(vault_name)
        if index is None:
            return json.dumps({"error": f"Vault '{vault_name or self._active_vault}' not available."})
        if not index.is_ready:
            return json.dumps({"error": "Vault index is still starting up, please wait."})

        if tool_name == "vault_search":
            return self._handle_search(args, index=index)
        elif tool_name == "vault_get_note":
            return self._handle_get_note(args, index=index)
        elif tool_name == "vault_note_context":
            return self._handle_note_context(args, index=index)
        elif tool_name == "vault_stats":
            return self._handle_stats(args, index=index)
        elif tool_name == "vault_graph_export":
            return self._handle_graph_export(args, index=index)
        elif tool_name == "vault_dedup":
            return self._handle_dedup(args, index=index)
        elif tool_name == "vault_delete_note":
            return self._handle_delete_note(args, index=index)
        elif tool_name == "vault_create_note":
            return self._handle_create_note(args, index=index)
        elif tool_name == "vault_append_note":
            return self._handle_append_note(args, index=index)
        elif tool_name == "vault_update_note":
            return self._handle_update_note(args, index=index)
        elif tool_name == "vault_related_notes":
            return self._handle_related_notes(args, index=index)
        elif tool_name == "vault_validate":
            return self._handle_validate(args, index=index)
        elif tool_name == "vault_orphans":
            return self._handle_orphans(args, index=index)
        elif tool_name == "vault_enhanced_stats":
            return self._handle_enhanced_stats(args, index=index)
        elif tool_name == "vault_graph_analytics":
            return self._handle_graph_analytics(args, index=index)
        raise NotImplementedError(f"obsidian_vault does not handle tool {tool_name}")

    def _handle_health(self, args: Dict[str, Any]) -> str:
        """Return health status for all or one vault."""
        vault_name = args.get("vault_name")
        target_names = [vault_name] if vault_name else list(self._vaults.keys())
        results = []
        for name in target_names:
            index = self._vaults.get(name)
            if not index:
                results.append({"name": name, "error": "Vault not loaded"})
                continue
            stats = {}
            try:
                stats = index.get_stats() if index.is_ready else {}
            except Exception as e:
                logger.warning("Failed to get stats for vault '%s': %s", name, e)
            results.append({
                "name": name,
                "ready": index.is_ready,
                "state": index.scan_state,
                "error": index.scan_error,
                "path": str(self._vault_paths.get(name, "")),
                "total_notes": stats.get("total_notes", 0),
                "active": name == self._active_vault,
            })
        return json.dumps({"vaults": results, "active_vault": self._active_vault}, default=_json_serialize)

    def _handle_list_vaults(self, args: Dict[str, Any]) -> str:
        vaults = []
        for name, path in self._vault_paths.items():
            index = self._vaults.get(name)
            stats = {}
            if index and index.is_ready:
                try:
                    stats = index.get_stats()
                except Exception as e:
                    logger.warning("Failed to get stats for vault '%s': %s", name, e)
            vaults.append({
                "name": name,
                "path": str(path),
                "active": name == self._active_vault,
                "ready": index.is_ready if index else False,
                "state": index.scan_state if index else "unknown",
                "total_notes": stats.get("total_notes", 0) if stats else 0,
            })
        return json.dumps({"vaults": vaults, "active_vault": self._active_vault}, default=_json_serialize)

    def _handle_switch(self, args: Dict[str, Any]) -> str:
        name = args.get("name", "").strip()
        if not name:
            return json.dumps({"error": "Vault name is required."})
        if self.switch_vault(name):
            return json.dumps({"success": True, "active_vault": name})
        return json.dumps({"error": f"Vault '{name}' not found."})

    def _handle_create_vault(self, args: Dict[str, Any]) -> str:
        name = args.get("name", "").strip()
        path_str = args.get("path", "")
        activate = args.get("activate", True)
        if not name:
            return json.dumps({"error": "Vault name is required."})
        if not path_str:
            return json.dumps({"error": "Vault path is required."})
        if name in self._vaults:
            return json.dumps({"error": f"Vault '{name}' already exists."})
        path = _resolve_path(path_str, self._hermes_home)
        if not path:
            return json.dumps({"error": f"Invalid vault path: {path_str}"})
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            return json.dumps({"error": f"Failed to create vault directory: {e}"})
        if not path.is_dir():
            return json.dumps({"error": f"Vault path is not a directory: {path}"})
        max_notes = int(self._config.get("max_notes", 10000))
        index = vault_module.get_shared_index(path)
        index._max_notes = max_notes
        index.tags_as_categories = _config_bool(self._config.get("tags_as_categories", True))
        count = index.scan(path, max_notes=max_notes, background=True)
        self._vaults[name] = index
        self._vault_paths[name] = path
        if activate:
            self._active_vault = name
        return json.dumps({"success": True, "name": name, "path": str(path), "notes": count, "active": name == self._active_vault}, default=_json_serialize)

    def _handle_search(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        query = args.get("query", "")
        category = args.get("category")
        tag = args.get("tag")
        limit = int(args.get("limit", 20))
        offset = int(args.get("offset", 0))
        sort_by = args.get("sort_by", "relevance")
        semantic = args.get("semantic", False)
        if not query and not tag and not category:
            return json.dumps({"error": "Provide a 'query', 'tag', or 'category' to search."})
        if index._vault_path:
            index._check_and_refresh(index._vault_path)
        tags = [tag] if tag else None
        results = index.search(query, category=category, tags=tags, limit=limit, offset=offset, sort_by=sort_by, semantic=semantic)
        if isinstance(results, dict):
            return json.dumps(results, default=_json_serialize)
        return json.dumps({
            "query": query,
            "count": len(results),
            "offset": offset,
            "sort_by": sort_by,
            "vault_name": self._active_vault,
            "results": [
                {"slug": n.slug, "title": n.title, "category": n.category, "tags": n.tags, "path": str(n.path), "snippet": n.body[:200].strip()}
                for n in results
            ],
        }, default=_json_serialize)

    def _handle_get_note(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        slug = args.get("slug", "")
        path = args.get("path", "")
        note = self._resolve_note(index, slug=slug if slug else None, path=path if path else None)
        if not note:
            identifier = path if path else slug
            return json.dumps({"error": f"Note '{identifier}' not found in vault."})
        return json.dumps({"slug": note.slug, "title": note.title, "category": note.category, "tags": note.tags, "path": str(note.path), "frontmatter": note.frontmatter, "body": note.body, "vault_name": self._active_vault}, default=_json_serialize)

    def _handle_note_context(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        slug = args.get("slug", "")
        path = args.get("path", "")
        depth = int(args.get("depth", 2))
        include_backlinks = args.get("include_backlinks", True)
        note = self._resolve_note(index, slug=slug if slug else None, path=path if path else None)
        if not note:
            identifier = path if path else slug
            return json.dumps({"error": f"Note '{identifier}' not found in vault."})
        context = index.get_link_context(note.slug, depth=depth, include_backlinks=include_backlinks)
        return json.dumps({"slug": note.slug, "title": note.title, "body": note.body, "linked_notes": [{"slug": n.slug, "title": n.title, "category": n.category, "tags": n.tags} for n in context]}, default=_json_serialize)

    def _handle_stats(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        return json.dumps(index.get_stats(), default=_json_serialize)

    def _handle_graph_export(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        fmt = args.get("format", "mermaid")
        max_depth = int(args.get("max_depth", 2))
        filter_tag = args.get("filter_tag")
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})
        notes = index.get_all_notes()
        if filter_tag:
            notes = [n for n in notes if filter_tag in n.tags]
        if fmt == "graphviz":
            lines = ["digraph vault {", "  rankdir=LR;", '  node [shape=box, style="rounded"];']
            visited = set()
            for note in notes:
                if note.slug not in visited:
                    visited.add(note.slug)
                    safe_slug = note.slug.replace('"', '\\"')
                    safe_title = note.title.replace('"', '\\"')
                    lines.append(f'  "{safe_slug}" [label="{safe_title}"];')
                for link in note.links:
                    if link in index._notes or link.lower().replace(" ", "-") in index._notes:
                        target = link.lower().replace(" ", "-") if link.lower().replace(" ", "-") in index._notes else None
                        if target and target not in visited:
                            visited.add(target)
                            safe_t = index._notes[target].title.replace('"', '\\"')
                            lines.append(f'  "{target}" [label="{safe_t}"];')
                        if target:
                            lines.append(f'  "{note.slug}" -> "{target}";')
            lines.append("}")
            return "\n".join(lines)
        else:
            lines = ["graph TD"]
            nodes = {}
            edges = set()
            visited = set()
            def collect(node_slug, depth):
                if depth > max_depth or node_slug in visited:
                    return
                visited.add(node_slug)
                note = index._notes.get(node_slug)
                if not note:
                    for k in index._notes:
                        if k == node_slug or k == node_slug.lower().replace(" ", "-"):
                            note = index._notes[k]
                            node_slug = k
                            break
                if not note:
                    return
                safe_slug = node_slug.replace(" ", "_").replace("-", "_")
                nodes[safe_slug] = note.title
                for link in note.links:
                    target_slug = link.lower().replace(" ", "-")
                    if target_slug in index._notes:
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

    def _handle_dedup(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        threshold = float(args.get("threshold", 0.85))
        limit = int(args.get("limit", 50))
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})
        notes = list(index._notes.values())
        duplicates = []
        checked = set()
        for i, note_a in enumerate(notes):
            if i >= len(notes) - 1:
                break
            for note_b in notes[i+1:]:
                if note_a.slug.casefold() == note_b.slug.casefold():
                    continue
                pair_key = (note_a.slug, note_b.slug)
                if pair_key in checked:
                    continue
                checked.add(pair_key)
                if not vault_module._has_embedding(note_a.embedding) or not vault_module._has_embedding(note_b.embedding):
                    title_sim = 1.0 if note_a.title.casefold() == note_b.title.casefold() else 0.0
                    if title_sim >= threshold:
                        duplicates.append({
                            "note_a": {"slug": note_a.slug, "title": note_a.title, "path": str(note_a.path)},
                            "note_b": {"slug": note_b.slug, "title": note_b.title, "path": str(note_b.path)},
                            "similarity": round(1.0, 4),
                        })
                    continue
                from plugins.memory.obsidian_vault.vault import cosine_similarity
                sim = cosine_similarity(note_a.embedding, note_b.embedding)
                if sim >= threshold:
                    duplicates.append({
                        "note_a": {"slug": note_a.slug, "title": note_a.title, "path": str(note_a.path)},
                        "note_b": {"slug": note_b.slug, "title": note_b.title, "path": str(note_b.path)},
                        "similarity": round(sim, 4),
                    })
                if len(duplicates) >= limit:
                    break
            if len(duplicates) >= limit:
                break
        duplicates.sort(key=lambda x: -x["similarity"])
        return json.dumps({"threshold": threshold, "count": len(duplicates), "duplicates": duplicates}, default=_json_serialize)

    def _handle_delete_note(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        slug = args.get("slug", "")
        path = args.get("path", "")
        note = self._resolve_note(index, slug=slug if slug else None, path=path if path else None)
        if not note:
            identifier = path if path else slug
            return json.dumps({"error": f"Note '{identifier}' not found in vault."})
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})
        success = index.delete_note(note.slug)
        if success:
            return json.dumps({"success": True, "deleted_slug": note.slug})
        return json.dumps({"error": "Failed to delete note"})

    def _handle_create_note(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        title = args.get("title", "")
        body = args.get("body", "")
        frontmatter = args.get("frontmatter", {})
        tags = args.get("tags", [])
        category = args.get("category")
        if not title:
            return json.dumps({"error": "title is required"})
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})
        # Cross-vault conflict check: if note exists in another vault, warn unless explicit vault_name given
        requested_vault = args.get("vault_name")
        if not requested_vault and len(self._vaults) > 1:
            slug_guess = title.replace(" ", "-").lower()
            conflicts = []
            for vname, vindex in self._vaults.items():
                if vindex is index:
                    continue
                if vindex.get_note(slug_guess):
                    conflicts.append(vname)
            if conflicts:
                return json.dumps({
                    "error": f"Note '{title}' already exists in vault(s): {conflicts}. Use vault_name to disambiguate.",
                    "conflicts": conflicts,
                })
        note = index.create_note(title=title, body=body, frontmatter=frontmatter, tags=tags, category=category)
        if note:
            return json.dumps({"slug": note.slug, "title": note.title, "path": str(note.path), "vault_name": self._active_vault}, default=_json_serialize)
        return json.dumps({"error": "Failed to create note"})

    def _handle_append_note(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        slug = args.get("slug", "")
        path = args.get("path", "")
        content = args.get("content", "")
        note = self._resolve_note(index, slug=slug if slug else None, path=path if path else None)
        if not note:
            identifier = path if path else slug
            return json.dumps({"error": f"Note '{identifier}' not found in vault."})
        if not content:
            return json.dumps({"error": "content is required"})
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})
        success = index.append_to_note(note.slug, content)
        return json.dumps({"success": success})

    def _handle_update_note(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        slug = args.get("slug", "")
        path = args.get("path", "")
        title = args.get("title")
        body = args.get("body")
        frontmatter = args.get("frontmatter")
        tags = args.get("tags")
        category = args.get("category")
        note = self._resolve_note(index, slug=slug if slug else None, path=path if path else None)
        if not note:
            identifier = path if path else slug
            return json.dumps({"error": f"Note '{identifier}' not found in vault."})
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})
        note = index.update_note(slug=note.slug, title=title, body=body, frontmatter=frontmatter, tags=tags, category=category)
        if note:
            return json.dumps({"slug": note.slug, "title": note.title, "path": str(note.path), "vault_name": self._active_vault}, default=_json_serialize)
        return json.dumps({"error": "Note not found or failed to update"})

    def _handle_related_notes(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        slug = args.get("slug", "")
        path = args.get("path", "")
        limit = int(args.get("limit", 10))
        min_similarity = float(args.get("min_similarity", 0.1))
        exclude_wikilinks = args.get("exclude_wikilinks", False)
        note = self._resolve_note(index, slug=slug if slug else None, path=path if path else None)
        if not note:
            identifier = path if path else slug
            return json.dumps({"error": f"Note '{identifier}' not found in vault."})
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})
        results = index.related_notes(slug=note.slug, limit=limit, min_similarity=min_similarity, exclude_wikilinks=exclude_wikilinks)
        return json.dumps({"count": len(results), "results": [{"slug": n.slug, "title": n.title, "category": n.category, "tags": n.tags, "similarity": round(sim, 4)} for n, sim in results]}, default=_json_serialize)

    def _handle_validate(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})
        result = index.validate()
        return json.dumps(result, default=_json_serialize)

    def _handle_orphans(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})
        result = index.find_orphans()
        return json.dumps(result, default=_json_serialize)

    def _handle_enhanced_stats(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})
        result = index.get_enhanced_stats()
        return json.dumps(result, default=_json_serialize)

    def _handle_graph_analytics(self, args: Dict[str, Any], *, index: vault_module.VaultIndex) -> str:
        if not self._initialized:
            return json.dumps({"error": "Index not initialized."})
        result = index.get_graph_analytics()
        return json.dumps(result, default=_json_serialize)

    def _resolve_note(self, index: vault_module.VaultIndex, slug: Optional[str] = None, path: Optional[str] = None) -> Optional[VaultNote]:
        if path:
            path_obj = Path(path)
            if path_obj.is_absolute():
                if index._vault_path:
                    try:
                        rel_path = path_obj.relative_to(index._vault_path)
                        slug_candidate = rel_path.with_suffix('').as_posix()
                    except ValueError:
                        slug_candidate = path_obj.stem
                else:
                    slug_candidate = path_obj.stem
            else:
                slug_candidate = path_obj.stem
        elif slug:
            slug_candidate = slug
        else:
            return None
        note = index.get_note(slug_candidate)
        if note:
            return note
        slugified = slug_candidate.lower().replace(" ", "-")
        note = index.get_note(slugified)
        if note:
            return note
        if path:
            for k, n in index._notes.items():
                if str(n.path) == str(Path(path).resolve()) or n.path.as_posix().endswith(path):
                    return n
        return None

    def on_session_switch(self, new_session_id: str, *, parent_session_id: str = "", reset: bool = False, rewound: bool = False, **kwargs) -> None:
        max_notes = int(self._config.get("max_notes", 10000))
        for name, vault_path in self._vault_paths.items():
            if vault_path and vault_path.is_dir():
                index = self._vaults.get(name)
                if index:
                    index.scan(vault_path, max_notes=max_notes)

    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        if not self._initialized:
            return
        if not self._config.get("index_on_write", True):
            return
        vault_path = self._get_vault_path()
        _write_memory_note(action, target, content, vault_path, metadata)

    def backup_paths(self) -> List[str]:
        return [str(path) for path in self._vault_paths.values() if path and path.is_dir()]

    def get_config_schema(self) -> List[Dict[str, Any]]:
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
                field_dict["required"] = False
            schema_fields.append(field_dict)
        return schema_fields



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