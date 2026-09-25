"""Round-robin APInex key pool with per-key 401-cooldown and 429 rotation.

Env (auto-discovered, order-stable):
    APINEX_API_KEY      key #1 (kept for backward compatibility)
    APINEX_API_KEY_2    key #2
    ... up to APINEX_API_KEY_9 (add as many as needed — no code change)

Keys are addressed by their 1-based NUMBER (stable even if the pool grows),
so ``mark_bad()`` cooldowns survive pool edits. Thread-safe: the gateway
dispatches concurrent tool calls, so every mutation rides one lock.

Backend-agnostic: this module only hands out keys; HTTP + retry policy lives
in ``provider._apinex_post``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_KEYS = 9
_BAD_COOLDOWN_S = 600.0  # a 401/403 parks the key for 10 min, then re-admitted

_lock = threading.Lock()
_cursor = 0
_bad_until: dict[int, float] = {}


class NoKeysAvailable(RuntimeError):
    """Raised when the pool has no usable key (none configured)."""


def _read_key(no: int) -> str:
    from plugins.web._common import provider_env

    name = "APINEX_API_KEY" if no == 1 else f"APINEX_API_KEY_{no}"
    return provider_env(name)


def discover_keys() -> List[Tuple[int, str]]:
    """All configured keys as ``[(number, key)]`` in numeric order.

    Re-read every call: the tool layer hot-reloads ``.env`` per call, so a
    newly added ``APINEX_API_KEY_5`` is picked up without a restart.
    """
    found = []
    for no in range(1, _MAX_KEYS + 1):
        key = _read_key(no)
        if key:
            found.append((no, key))
    return found


def pool_size() -> int:
    return len(discover_keys())


def key_fingerprint(key: str) -> str:
    """Masked id for logs/metadata (first 10 + last 6 chars; unrecoverable)."""
    key = key or ""
    if len(key) <= 16:
        return (key[:4] + "…") if key else "?"
    return f"{key[:10]}…{key[-6:]}"


def fingerprint_of(no: int) -> str:
    for n, k in discover_keys():
        if n == no:
            return key_fingerprint(k)
    return f"key#{no}?"


def _quarantined(no: int, now: float) -> bool:
    return _bad_until.get(no, 0.0) > now


def next_key() -> Tuple[int, str]:
    """Next round-robin key, skipping quarantined ones.

    When EVERY key is quarantined, quarantines are cleared (a bad key that
    healed must not wedge the pool forever) and rotation resumes.
    Raises :class:`NoKeysAvailable` when nothing is configured at all.
    """
    global _cursor
    with _lock:
        pool = discover_keys()
        if not pool:
            raise NoKeysAvailable(
                "APINEX_API_KEY environment variable not set. "
                "Create a key at https://apinex.bond"
            )
        now = time.monotonic()
        usable = [(n, k) for n, k in pool if not _quarantined(n, now)]
        if not usable:
            logger.warning(
                "APInex key pool: all %d key(s) quarantined; clearing quarantines for a fresh attempt",
                len(pool),
            )
            _bad_until.clear()
            usable = pool
        _cursor_start = _cursor % len(usable)
        chosen = usable[_cursor_start]
        _cursor += 1
        logger.debug("APInex key pool: serving key #%d (%s)", chosen[0], key_fingerprint(chosen[1]))
        return chosen


def mark_bad(no: int, reason: str) -> None:
    """Quarantine key ``no`` for :data:`_BAD_COOLDOWN_S` (401/403 path)."""
    with _lock:
        _bad_until[no] = time.monotonic() + _BAD_COOLDOWN_S
    logger.warning(
        "APInex key pool: key #%d quarantined for %.0fs (%s)",
        no, _BAD_COOLDOWN_S, (reason or "")[:150],
    )


def profile_name() -> str:
    """Current Hermes profile (for meter attribution): ``main`` or e.g. ``vex_agent``."""
    import os

    home = (os.environ.get("HERMES_HOME") or "").replace("\\", "/").rstrip("/")
    if not home:
        return "?"
    parts = home.split("/")
    if len(parts) >= 2 and parts[-2] == "profiles":
        return parts[-1] or "?"
    return "main"
