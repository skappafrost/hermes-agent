"""hermes-gemini-live — Gemini Live voice relay for Hermes Desktop.

Hermes loads this file with ``submodule_search_locations`` (``plugins_loader.py:644``),
but a test runner or a linter may exec it as a loose module with no parent package,
where ``from .x import y`` dies. The absolute import below resolves identically in both
worlds because the shim puts this directory on ``sys.path`` — the same shim
``dashboard/plugin_api.py`` needs for the same reason.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hermes_gemini_live import config  # noqa: E402

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Fail fast on a misread key rather than at the first user click.

    Nothing is registered on the agent: the voice lane lives in the plugin's own routes,
    so an absent key must be loud here, not silent in the Desktop panel. The home is
    logged because one process serves many profiles, and "which home did this backend
    bind?" is the first question any route-404 report needs an answer to.
    """
    from hermes_constants import display_hermes_home

    home = display_hermes_home()
    try:
        config.resolve_key()
    except config.ConfigError as exc:
        logger.warning("hermes-gemini-live [%s]: %s", home, exc)
        return
    logger.info(
        "hermes-gemini-live [%s]: ready (model=%s voice=%s)",
        home,
        config.model(),
        config.voice(),
    )
