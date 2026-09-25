"""UnSearch (unsearch.dev) provider — tier-3 fallback candidate, DORMANT.

The account + API key exist (see apinex-meter/accounts.json) but their public
backend is not serving yet: ``/api/v1/search`` proxies to a cold container
(``container_unavailable``) and the deployed worker rejects the dashboard
``unsk_`` key. Until ``UNSEARCH_API_KEY`` is set in .env this provider is
unavailable and the chain skips it instantly (zero latency cost). When UnSearch
heals, add the key and the tier activates with no code change.
"""
from __future__ import annotations

from .provider import UnsearchWebSearchProvider
