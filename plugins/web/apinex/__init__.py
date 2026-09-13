"""APInex web tools plugin — bundled, auto-loaded.

- Web search/extract provider: round-robin key pool + backoff retry, Exa
  fallback (Skappa 2026-09-11; Docker uninstalled, no local stack).
- ``web_research`` tool: multi-step research with cited sources via APInex's
  free research endpoint.
"""
from __future__ import annotations
from plugins.web.apinex.provider import ApinexWebSearchProvider
from plugins.web.apinex.tools import WEB_RESEARCH_SCHEMA, _check_apinex_available, _handle_web_research


def register(ctx) -> None:
    ctx.register_web_search_provider(ApinexWebSearchProvider())
    ctx.register_tool(
        name="web_research",
        toolset="web",
        schema=WEB_RESEARCH_SCHEMA,
        handler=_handle_web_research,
        check_fn=_check_apinex_available,
        emoji="🔬",
        is_async=True,
    )
