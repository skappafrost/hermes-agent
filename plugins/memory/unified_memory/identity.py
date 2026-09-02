"""Identity resolution for UnifiedMemoryProvider.

Resolves (AGENT_ID, TEAM_ID, USER_ID) for the active Hermes profile.

Resolution order per field (first non-empty wins):
  1. Prefixed env var:   UNIFIED_MEMORY_AGENT_ID / UNIFIED_MEMORY_TEAM_ID / UNIFIED_MEMORY_USER_ID
  2. Bare env var:       AGENT_ID / TEAM_ID / USER_ID
  3. PROFILE_IDENTITY_MAP[profile] entry
  4. USER_ID-only final fallback: "skappa" (single-operator deployment)

Active profile comes from env var HERMES_PROFILE (primary) or AGENT_PROFILE
(fallback), matching Hermes runtime convention. Unknown profile raises
ValueError instead of returning silent empty strings.

Pure function: pass ``env`` explicitly to test; defaults to os.environ.
Matrix source of truth: task t_c7a361a5 identity_mapping_design.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

PROFILE_ENV_VARS = ("HERMES_PROFILE", "AGENT_PROFILE")

AGENT_ENV_VARS = ("UNIFIED_MEMORY_AGENT_ID", "AGENT_ID")
TEAM_ENV_VARS = ("UNIFIED_MEMORY_TEAM_ID", "TEAM_ID")
USER_ENV_VARS = ("UNIFIED_MEMORY_USER_ID", "USER_ID")

# profile -> (agent_id, team_id); user_id resolved via USER_ID_FALLBACK.
PROFILE_IDENTITY_MAP: dict[str, tuple[str, str]] = {
    "default": ("default", "hermes"),
    "neo_agent": ("neo", "hermes"),
    "nexus_agent": ("nexus", "hermes"),
    "vex_agent": ("vex", "hermes"),
    "zen_agent": ("zen", "hermes"),
}

USER_ID_FALLBACK = "skappa"


@dataclass(frozen=True)
class Identity:
    agent_id: str
    team_id: str
    user_id: str


def _pick(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env.get(name)
        if value and value.strip():
            return value.strip()
    return None


def resolve_identity(env: Mapping[str, str] | None = None) -> Identity:
    """Resolve (agent_id, team_id, user_id). See module docstring."""
    env = os.environ if env is None else env

    agent_id = _pick(env, AGENT_ENV_VARS)
    team_id = _pick(env, TEAM_ENV_VARS)
    user_id = _pick(env, USER_ENV_VARS)

    profile = _pick(env, PROFILE_ENV_VARS)

    if agent_id is None or team_id is None or profile is not None:
        if profile is None:
            raise ValueError(
                "cannot derive identity: no profile env var set "
                f"(expected one of {', '.join(PROFILE_ENV_VARS)})"
            )
        entry = PROFILE_IDENTITY_MAP.get(profile)
        if entry is None:
            raise ValueError(
                f"no identity mapping for profile {profile!r}; "
                f"known profiles: {', '.join(sorted(PROFILE_IDENTITY_MAP))}"
            )
        table_agent_id, team_id_from_table = entry
        if agent_id is None:
            agent_id = table_agent_id
        if team_id is None:
            team_id = team_id_from_table

    if user_id is None:
        user_id = USER_ID_FALLBACK

    return Identity(agent_id=agent_id, team_id=team_id, user_id=user_id)


if __name__ == "__main__":
    # Matrix rows reproduce exactly with empty overrides.
    EMPTY: dict[str, str] = {}
    for profile, (agent_id, team_id) in PROFILE_IDENTITY_MAP.items():
        ident = resolve_identity({"HERMES_PROFILE": profile})
        assert (ident.agent_id, ident.team_id, ident.user_id) == (
            agent_id,
            team_id,
            USER_ID_FALLBACK,
        ), (profile, ident)

    # Env override wins over table.
    assert resolve_identity(
        {"HERMES_PROFILE": "vex_agent", "UNIFIED_MEMORY_AGENT_ID": "custom"}
    ).agent_id == "custom"
    assert resolve_identity(
        {"HERMES_PROFILE": "vex_agent", "TEAM_ID": "other-team"}
    ).team_id == "other-team"
    assert resolve_identity(
        {"HERMES_PROFILE": "vex_agent", "UNIFIED_MEMORY_USER_ID": "alice"}
    ).user_id == "alice"

    # Bare env vars also honored.
    assert resolve_identity(
        {"HERMES_PROFILE": "vex_agent", "AGENT_ID": "bare-agent"}
    ).agent_id == "bare-agent"

    # AGENT_PROFILE fallback profile var.
    assert resolve_identity({"AGENT_PROFILE": "neo_agent"}).agent_id == "neo"

    # Missing profile / unknown profile raise.
    try:
        resolve_identity({})
        raise AssertionError("expected ValueError for missing profile")
    except ValueError as exc:
        assert "HERMES_PROFILE" in str(exc)
    try:
        resolve_identity({"HERMES_PROFILE": "ghost"})
        raise AssertionError("expected ValueError for unknown profile")
    except ValueError as exc:
        assert "ghost" in str(exc)

    # Blank-string env values treated as unset.
    assert resolve_identity(
        {"HERMES_PROFILE": "vex_agent", "AGENT_ID": ""}
    ).agent_id == "vex"

    print("identity.py self-check: all assertions passed")
