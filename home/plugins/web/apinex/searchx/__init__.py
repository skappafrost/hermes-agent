"""SearchX web search + extract provider — tier-2 fallback of the APInex chain.

Registered by the parent package; every attempt is auto-metered into the APInex
pool dashboard through the shared ``meter`` module (stdlib only, own DB path).
"""
from __future__ import annotations

from .provider import SearchxWebSearchProvider

__all__ = ["SearchxWebSearchProvider"]
