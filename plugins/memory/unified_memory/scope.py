"""Scope management for the unified memory provider.

Decides who may see an observation. Default is isolated: an observation
belongs to its owning agent and nobody else. A category becomes visible
across the team (``skappa-hermes-team``) only when it is explicitly tagged
``shared`` in the ``unified_memory`` config schema — no hardcoded category
lists, behavior follows the passed-in config alone.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

TEAM_ID = "skappa-hermes-team"

# Config keys read by ScopeManager (under plugins.unified_memory).
SCOPE_CONFIG_KEYS = ("shared_categories",)

DEFAULT_SCOPE = "isolated"


class ScopeManager:
    """Config-driven visibility rules over (agent_id, category) pairs.

    Config shape (all optional, all under ``plugins.unified_memory``):

        shared_categories: list of category names whose observations are
            visible across the team. Everything else stays private to the
            owning agent.

    Pure logic: reads only the config dict handed to the constructor.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        cfg = config or {}
        self.team_id = TEAM_ID
        self._shared: frozenset = frozenset(
            c for c in (cfg.get("shared_categories") or []) if isinstance(c, str)
        )

    # -- config-driven toggling ------------------------------------------
    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]] = None) -> "ScopeManager":
        """Build a manager from a unified_memory config dict (or refresh one)."""
        return cls(config)

    def reconfigure(self, config: Optional[Dict[str, Any]]) -> None:
        """Swap in new config; behavior changes with no code edits."""
        other = ScopeManager(config)
        self._shared = other._shared

    # -- tagging / resolution ---------------------------------------------
    def tag_scope(self, category: str) -> str:
        """Resolve the scope tag for an observation's category."""
        return "shared" if category in self._shared else DEFAULT_SCOPE

    def scope_tag(self, agent_id: str, category: str) -> Dict[str, Any]:
        """Full scope tag for an observation owned by ``agent_id``."""
        return {
            "scope": self.tag_scope(category),
            "team": self.team_id if category in self._shared else None,
            "owner": agent_id,
        }

    # -- visibility ---------------------------------------------------------
    def is_shared(self, category: str) -> bool:
        return category in self._shared

    def can_read(self, reader_agent_id: str,
                 owner_agent_id: str, category: str) -> bool:
        """Visibility query: may ``reader`` see owner's observation?"""
        if not isinstance(reader_agent_id, str) or not isinstance(owner_agent_id, str):
            return False
        if reader_agent_id == owner_agent_id:
            return True
        return category in self._shared  # team-shared categories only

    def visible_categories(self, reader_agent_id: str, owner_agent_id: str) -> List[str]:
        """Categories of ``owner`` visible to ``reader``."""
        if reader_agent_id == owner_agent_id:
            return ["*"]  # everything
        return sorted(self._shared)


if __name__ == "__main__":
    raise SystemExit("import-only module")
