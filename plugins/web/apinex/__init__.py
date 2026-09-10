"""APInex web search + extract plugin — bundled, auto-loaded.

Falls back to the previous combo (exa for search, firecrawl for extract) when
APInex fails, per Skappa's request 2026-09-10.
"""
from __future__ import annotations
from plugins.web.apinex.provider import ApinexWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(ApinexWebSearchProvider())
