"""Backend half of hermes-gemini-live, mounted by the Hermes web server.

Landed at ``/api/plugins/hermes-gemini-live/`` by
``hermes_cli/web_server.py::_mount_plugin_api_routes``, which imports THIS FILE BY PATH.
There is no parent package at that point, so the plugin root goes on ``sys.path`` and
the package is imported by absolute name — the same shim hermes-talk's dashboard half
uses, and the reason this plugin keeps its code in one prefixed package.

Two invariants:

- **The key never leaves this process.** It is read per call, spent only inside
  :mod:`hermes_gemini_live.live_client`, and never enters a response body or a log line.
- **The WebSocket authorizes itself.** Starlette's HTTP middleware never runs for a WS
  upgrade, so the runtime "plugin is disabled" gate cannot protect this endpoint; it
  calls core's canonical upgrade gate rather than inventing a second answer.
"""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from hermes_gemini_live import agent_lane, config, relay  # noqa: E402

logger = logging.getLogger(__name__)
router = APIRouter()


def _upgrade_authorized(ws: WebSocket) -> bool:
    """Core's own gate, so this endpoint can never drift from dashboard auth.

    ``import_module`` is deliberate: ``from hermes_cli import web_server_chat`` resolves
    through the package attribute and would keep serving the real module even when a
    harness replaced it in ``sys.modules``.

    Two ways this can break must fail CLOSED, not open, because the route mints a live
    provider session: core renaming or removing ``_ws_auth_ok``, or the module being
    present but the call raising. A bare-FastAPI harness where the import itself fails is
    the only permissive case, and that process holds no dashboard auth to bypass.
    """
    try:
        gate = importlib.import_module("hermes_cli.web_server_chat")
    except ModuleNotFoundError:
        return True
    except Exception as exc:
        logger.warning("hermes-gemini-live: cannot load the dashboard auth gate (%s); "
                       "refusing the upgrade", type(exc).__name__)
        return False
    authorize = getattr(gate, "_ws_auth_ok", None)
    if not callable(authorize):
        logger.warning("hermes-gemini-live: the host no longer exposes _ws_auth_ok; "
                       "refusing the upgrade rather than guessing")
        return False
    try:
        return bool(authorize(ws))
    except Exception as exc:
        logger.warning("hermes-gemini-live: auth gate raised %s; refusing", type(exc).__name__)
        return False


def _profile_of(ws: WebSocket) -> str:
    """Named profile for multiplexed hosts: the route's own ``?profile=``, never env."""
    return (ws.query_params.get("profile") or "").strip()


@router.get("/status")
async def status(profile: str = "") -> dict:
    """Readiness for the panel. Key material is never reported, only presence.

    Logged on every read: when the panel's button goes dark, the question is whether this
    line appears in the backend log at all. Absent here means core's per-home gate
    refused the request before the plugin's router ever ran.

    The agent lane is reported as *configured*, never as *reachable*: probing it is a
    network round trip and this handler runs on the web server's event loop.
    """
    payload = config.status_dict()
    payload["lane"] = {
        "configured": bool(agent_lane.key()),
        "url": agent_lane.base_url(),
        "profile": config.serving_profile() or "default",
        "scope": config.api_server_session_key() or "default",
    }
    logger.info(
        "hermes-gemini-live: /status profile=%s ok=%s model=%s lane=%s detail=%r",
        profile or "-",
        payload["ok"],
        payload["model"],
        payload["lane"]["configured"],
        payload["detail"],
    )
    return payload


@router.websocket("/relay")
async def live_relay(ws: WebSocket) -> None:
    if not _upgrade_authorized(ws):
        logger.warning("hermes-gemini-live: refused an unauthorized /relay upgrade")
        await ws.close(code=4401)
        return
    if _profile_of(ws):
        logger.debug("hermes-gemini-live: relay serving profile %s", _profile_of(ws))
    await ws.accept()
    try:
        # No client-supplied profile reaches the lane: relay derives it from the home this
        # process actually serves. The query value is log-only, to spot a mis-pooled backend.
        await relay.run_relay(ws, session_key=config.api_server_session_key())
    except WebSocketDisconnect:
        logger.debug("hermes-gemini-live: browser went away mid-call")
    finally:
        try:
            await ws.close()
        except Exception:
            pass
