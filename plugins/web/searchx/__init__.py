"""SearchX web search + extract plugin — bundled, auto-loaded.

Tier-2 fallback for the APInex chain (Skappa 2026-09-15): 3,000 requests/day
free, hybrid keyword+semantic index, JS-rendered page extraction. Every
attempt is auto-metered into the APInex pool dashboard (same SQLite meter)
when the tracker module is importable — the meter is dependency-free
(stdlib + its own DB path), so this works regardless of the apinex plugin.
"""
from __future__ import annotations
from plugins.web.searchx.provider import SearchxWebSearchProvider


def register(ctx) -> None:
    ctx.register_web_search_provider(SearchxWebSearchProvider())
