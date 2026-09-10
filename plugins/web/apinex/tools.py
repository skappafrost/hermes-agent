"""APInex web_research tool — multi-step research with cited sources.

Calls ``POST /v1/tools/web/research`` (FREE tier, 12 RPM) on the same APInex key
as the search/extract provider. Response: ``{"output": {"content", "content_type",
"sources"}, "warnings", "usage"}``.

Registered into the existing ``web`` toolset so every surface that already has
``web_search`` / ``web_extract`` gets ``web_research`` automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_RESEARCH_TIMEOUT = 300.0  # deep effort measured at ~27s; generous ceiling
_MAX_OUTPUT_CHARS = 30000  # keep one research answer within a sane context slice


def _check_apinex_available() -> bool:
    """Tool gate: APInex key present (same env as the web provider)."""
    from agent.web_search_provider import get_provider_env

    return bool(get_provider_env("APINEX_API_KEY"))


async def _handle_web_research(query: str, effort: str = "lite", task_id: Optional[str] = None) -> str:
    """Run a multi-step web research query via APInex.

    Returns JSON ``{"success": true, "content": ..., "sources": [...], "effort": ...}``
    or ``{"success": false, "error": ...}``.
    """
    from plugins.web.apinex.provider import _apinex_post

    effort = (effort or "lite").strip().lower()
    if effort not in ("lite", "standard", "deep"):
        effort = "lite"

    try:
        body = await asyncio.to_thread(
            _apinex_post,
            "/tools/web/research",
            {"input": str(query), "research_effort": effort},
            _RESEARCH_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — surface as failure shape
        logger.warning("web_research failed: %s", exc)
        return json.dumps({"success": False, "error": f"APInex research failed: {exc}"}, ensure_ascii=False)

    output = body.get("output") or {}
    content = str(output.get("content") or "")
    sources = output.get("sources") or []
    if not content:
        return json.dumps(
            {"success": False, "error": "APInex research returned an empty answer"},
            ensure_ascii=False,
        )

    # Compact the sources list to title + url (drop thumbnails/duplicates noise).
    slim_sources = []
    seen_urls = set()
    for s in sources:
        if not isinstance(s, dict):
            continue
        url = str(s.get("url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        slim_sources.append({"title": str(s.get("title") or "")[:120], "url": url})

    truncated = len(content) > _MAX_OUTPUT_CHARS
    payload: Dict[str, Any] = {
        "success": True,
        "effort": effort,
        "content": content[:_MAX_OUTPUT_CHARS],
        "sources": slim_sources,
        "sources_count": len(slim_sources),
        "usage": body.get("usage") or {},
    }
    if truncated:
        payload["note"] = f"answer truncated at {_MAX_OUTPUT_CHARS} chars"
    return json.dumps(payload, ensure_ascii=False, indent=2)


WEB_RESEARCH_SCHEMA = {
    "name": "web_research",
    "description": (
        "Run a multi-step web research question through APInex: it performs searches, reads pages, "
        "and returns a synthesized answer with numbered citations [[1]] [[2]] plus a sources list. "
        "FREE tier (rate-limited ~12 calls/min). Use for open questions needing synthesis across "
        "multiple sources ('what is the current state of X', 'compare A vs B for my use case'); "
        "use web_search for simple lookups and web_extract to read a known URL."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Research question or instruction (any language; answers match the question's language).",
            },
            "effort": {
                "type": "string",
                "enum": ["lite", "standard", "deep"],
                "description": "Depth/cost tier: lite = fast single-pass (~30s), standard = balanced (~10-30s), deep = most thorough (~30-60s). Default lite.",
            },
        },
        "required": ["query"],
    },
}
