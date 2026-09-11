"""Docker rescue layer for the APInex→Firecrawl local fallback path.

Skappa's setup (2026-09-10): Docker Desktop does NOT auto-start with Windows
(``AutoStart: false``); the local Firecrawl stack (localhost:13002) is only
needed as the last-resort extract fallback when APInex is down. This module
brings the stack up on demand:

    1. Probe the Firecrawl API (http://localhost:13002) — healthy → done.
    2. Probe the Docker engine (``docker info``) — healthy → compose up only.
    3. Start Docker Desktop (``Docker Desktop.exe``) → poll the engine
       (patience ~180s; cold start measured 20-40s, WSL2 worst case longer).
    4. ``docker compose up -d`` in the Firecrawl project dir → poll the API
       (patience ~120s).

Guardrails:
- ``web.apinex_docker_rescue: false`` disables the whole layer (fail fast).
- One rescue attempt per cooldown window (default 300s): repeated fallback
  failures during an outage must not spawn parallel Docker startups.
- All steps are idempotent (probes first, start only when down).
- Windows-only by construction (Docker Desktop path); non-Windows hosts
  with a running engine still get the compose-up path.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

_DOCKER_DESKTOP = r"C:\Program Files\Docker\Docker\Docker Desktop.exe"
_FIRECRAWL_DIR = r"C:\Users\Ha Trung\firecrawl"
_API_URL = "http://localhost:13002"
_API_KEY_ENV = "FIRECRAWL_API_KEY"

_ENGINE_POLL_S = 5.0        # interval between docker-engine polls
_ENGINE_PATIENCE_S = 240.0  # cold start patience (WSL2 slow boots)
_API_POLL_S = 3.0           # interval between API polls
_API_PATIENCE_S = 150.0     # compose up → API healthy patience
_COOLDOWN_S = 300.0         # min seconds between rescue attempts
_PROBE_TIMEOUT_S = 10.0     # per-probe subprocess timeout

_lock = threading.Lock()
_last_attempt: float = 0.0


def _rescue_enabled() -> bool:
    """``web.apinex_docker_rescue`` from config (default: enabled)."""
    try:
        from hermes_cli.config import load_config_readonly

        web_cfg = (load_config_readonly().get("web") or {})
        return bool(web_cfg.get("apinex_docker_rescue", True))
    except Exception as exc:  # noqa: BLE001 — config layer optional
        logger.debug("apinex_docker_rescue config read failed: %s", exc)
        return True


def _api_healthy() -> bool:
    """Cheap TCP-level probe of the Firecrawl API port."""
    import httpx

    try:
        # 401/404 both prove the API is serving; only connection errors mean down.
        httpx.get(_API_URL, timeout=_PROBE_TIMEOUT_S)
        return True
    except httpx.RequestError:
        return False


def _engine_healthy() -> bool:
    """``docker info`` exit 0 → the Linux engine is up."""
    try:
        r = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT_S,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001 — timeout / docker missing
        return False


def _start_docker_desktop() -> None:
    """Launch Docker Desktop (no-op when already running or path missing)."""
    if not os.path.exists(_DOCKER_DESKTOP):
        raise RuntimeError(f"Docker Desktop not found at {_DOCKER_DESKTOP}")
    subprocess.Popen(
        [_DOCKER_DESKTOP],
        cwd=os.path.dirname(_DOCKER_DESKTOP),
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200),
        close_fds=True,
    )


def _compose_up(attempts: int = 2) -> None:
    """``docker compose up -d`` in the Firecrawl project dir, with one retry.

    Timeout 420s: on a fresh engine, compose must create the whole stack and
    wait out health-check dependencies. Cold-boot transients (a dependency
    racing its own healthcheck) clear on the second attempt.
    """
    last_tail = ""
    for attempt in range(1, attempts + 1):
        r = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=_FIRECRAWL_DIR, capture_output=True, text=True, timeout=420,
        )
        if r.returncode == 0:
            return
        # Warnings about unset env vars are noise; the tail holds the real error.
        last_tail = ((r.stderr or "") + (r.stdout or "")).strip()[-500:]
        logger.warning("docker compose up attempt %d/%d failed: %s", attempt, attempts, last_tail[:200])
        if attempt < attempts:
            time.sleep(10)
    raise RuntimeError(f"docker compose up failed: {last_tail}")


def _wait_engine(patience: float = _ENGINE_PATIENCE_S) -> bool:
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        if _engine_healthy():
            return True
        time.sleep(_ENGINE_POLL_S)
    return False


def _wait_api(patience: float = _API_PATIENCE_S) -> bool:
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        if _api_healthy():
            return True
        time.sleep(_API_POLL_S)
    return False


def ensure_firecrawl_local(wait: bool = True) -> bool:
    """Make sure the local Firecrawl API is reachable; bring the stack up if not.

    Returns True when the API answers by the end (or already did). Thread-safe;
    concurrent callers join the in-flight rescue instead of racing it.
    """
    global _last_attempt

    if _api_healthy():
        return True

    if not _rescue_enabled():
        logger.info("Firecrawl local is down; docker rescue disabled via web.apinex_docker_rescue")
        return False

    with _lock:
        now = time.monotonic()
        if now - _last_attempt < _COOLDOWN_S:
            # Inside the cooldown: someone already tried recently. If a previous
            # rescue is mid-flight (lock held), the API check below follows it.
            if not _api_healthy():
                logger.info("Docker rescue in cooldown window; skipping (last attempt %.0fs ago)", now - _last_attempt)
                return False
            return True
        _last_attempt = now

        if _api_healthy():  # re-check under the lock
            return True

        logger.warning("Firecrawl local (%s) is down; starting rescue (Docker Desktop → compose)", _API_URL)
        try:
            if not _engine_healthy():
                _start_docker_desktop()
                if not _wait_engine():
                    raise RuntimeError("Docker engine did not become healthy in time")
                logger.info("Docker engine healthy; bringing up Firecrawl stack")
            _compose_up()
            ok = _wait_api()
            if ok:
                logger.info("Firecrawl local rescue complete — API healthy at %s", _API_URL)
            else:
                logger.warning("Firecrawl rescue: API still unhealthy after compose up")
            return ok
        except Exception as exc:  # noqa: BLE001 — rescue is best-effort
            logger.warning("Docker rescue failed: %s", exc)
            return False


def _fc_api_key() -> str:
    """Local Firecrawl key (config-aware env lookup)."""
    from agent.web_search_provider import get_provider_env

    return get_provider_env(_API_KEY_ENV)
