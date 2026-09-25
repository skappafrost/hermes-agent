"""APInex web tools plugin — search/extract chain plus the ``web_research`` tool.

Registers three web providers from one package (Skappa 2026-09-11 / 09-15):

- ``apinex``   tier 1: round-robin key pool + backoff retry.
- ``searchx``  tier 2: 3,000 requests/day free, JS-rendered page extraction.
- ``unsearch`` tier 3: dormant until ``UNSEARCH_API_KEY`` serves a working backend.

They are one package rather than three plugins because the fallback chain
instantiates the lower tiers directly (``ApinexWebSearchProvider._instantiate_tier``)
and shares the dependency-free SQLite meter in ``meter.py`` — neither of which is
importable across separate ``~/.hermes/plugins`` packages.
"""
from __future__ import annotations

from .provider import ApinexWebSearchProvider
from .searchx.provider import SearchxWebSearchProvider
from .tools import WEB_RESEARCH_SCHEMA, _check_apinex_available, _handle_web_research
from .unsearch.provider import UnsearchWebSearchProvider


def register(ctx) -> None:
    for provider in (ApinexWebSearchProvider(), SearchxWebSearchProvider(),
                     UnsearchWebSearchProvider()):
        ctx.register_web_search_provider(provider)
    ctx.register_tool(
        name="web_research",
        toolset="web",
        schema=WEB_RESEARCH_SCHEMA,
        handler=_handle_web_research,
        check_fn=_check_apinex_available,
        emoji="🔬",
        is_async=True,
    )
    from .pool_reporter import post_tool_call

    ctx.register_hook("post_tool_call", post_tool_call)
