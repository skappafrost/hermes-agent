"""UnSearch (unsearch.dev) — free-tier web search + research (Tavily/Exa-compatible).

Env: ``UNSEARCH_API_KEY`` (https://unsearch.dev — 5,000 calls/month free).
Dormant by design until their public backend works (see ``__init__.py``):
without the key every method fails fast, so the APInex chain skips this
tier with zero latency cost.

Search: ``POST /api/v1/search {"query", "max_results"}`` (Tavily-shaped
response). Extract: ``POST /api/v1/extract {"urls": [...]}``. Auth via
``X-API-Key``. Every attempt is metered into the pool dashboard under
``route='unsearch'``.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List

import httpx

from plugins.web._common import (
    BaseWebSearchProvider, document, page_error, provider_env,
    run_extract, run_search, search_fail, search_ok, setup_schema, web_hit,
)
from .. import meter as _meter

logger = logging.getLogger(__name__)

_MISSING_KEY = "UNSEARCH_API_KEY environment variable not set. Create a key at https://unsearch.dev"
_BASE = "https://api.unsearch.dev/api/v1"
_TIMEOUT = 25.0


def _headers() -> Dict[str, str]:
    return {"X-API-Key": provider_env("UNSEARCH_API_KEY") or ""}


def _error_detail(resp: Any) -> str:
    try:
        body = resp.json() or {}
        err = body.get("error") or body.get("message") or ""
        return str(err.get("message") if isinstance(err, dict) else err)
    except Exception:  # noqa: BLE001
        return ""


class UnsearchWebSearchProvider(BaseWebSearchProvider):
    """UnSearch search + extract provider (tier-3 fallback, self-metered)."""

    NAME = "unsearch"
    DISPLAY_NAME = "UnSearch"
    KEY_ENV = "UNSEARCH_API_KEY"
    EXTRACT = True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        def _body() -> Dict[str, Any]:
            logger.info("UnSearch search: '%s' (limit=%d)", query, limit)
            t0 = time.monotonic()
            try:
                resp = httpx.post(
                    f"{_BASE}/search",
                    json={"query": query, "max_results": max(1, min(int(limit), 20))},
                    headers=_headers(),
                    timeout=_TIMEOUT,
                )
            except httpx.RequestError as exc:
                _meter.log(route="unsearch", endpoint="search", ok=False, status=None,
                           latency_ms=(time.monotonic() - t0) * 1000,
                           req_summary=_meter.summarize_search(query, limit))
                return search_fail(f"UnSearch unreachable: {exc}")
            latency_ms = (time.monotonic() - t0) * 1000
            if resp.status_code >= 400:
                _meter.log(route="unsearch", endpoint="search", ok=False, status=resp.status_code,
                           latency_ms=latency_ms,
                           req_summary=_meter.summarize_search(query, limit))
                return search_fail(f"UnSearch returned HTTP {resp.status_code} — {_error_detail(resp)[:200]}")
            try:
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                _meter.log(route="unsearch", endpoint="search", ok=False, status=resp.status_code,
                           latency_ms=latency_ms, req_summary=_meter.summarize_search(query, limit))
                return search_fail(f"could not parse UnSearch response: {exc}")
            # Tavily shape: {results: [{title,url,content}], ...}
            hits = [
                web_hit(str(r.get("url") or ""), str(r.get("title") or ""),
                        str(r.get("content") or r.get("snippet") or "")[:1500], i + 1)
                for i, r in enumerate((data.get("results") or data.get("data") or [])[:limit])
            ]
            _meter.log(route="unsearch", endpoint="search", ok=True, status=resp.status_code,
                       latency_ms=latency_ms, req_summary=_meter.summarize_search(query, limit),
                       resp_bytes=_meter.size_of(data), result_count=len(hits),
                       key_fp=_meter.key_fingerprint(_headers()["X-API-Key"]))
            return search_ok(hits)

        return run_search("UnSearch", logger, _body)

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        def _body() -> List[Dict[str, Any]]:
            logger.info("UnSearch extract: %d URL(s)", len(urls))
            t0 = time.monotonic()
            try:
                resp = httpx.post(
                    f"{_BASE}/extract",
                    json={"urls": list(urls)},
                    headers=_headers(),
                    timeout=_TIMEOUT,
                )
            except httpx.RequestError as exc:
                _meter.log(route="unsearch", endpoint="contents", ok=False, status=None,
                           latency_ms=(time.monotonic() - t0) * 1000,
                           req_summary=_meter.summarize_urls(urls))
                raise RuntimeError(f"UnSearch unreachable: {exc}") from exc
            latency_ms = (time.monotonic() - t0) * 1000
            if resp.status_code >= 400:
                _meter.log(route="unsearch", endpoint="contents", ok=False, status=resp.status_code,
                           latency_ms=latency_ms, req_summary=_meter.summarize_urls(urls))
                raise RuntimeError(f"UnSearch returned HTTP {resp.status_code} — {_error_detail(resp)[:200]}")
            try:
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                _meter.log(route="unsearch", endpoint="contents", ok=False, status=resp.status_code,
                           latency_ms=latency_ms, req_summary=_meter.summarize_urls(urls))
                raise RuntimeError(f"could not parse UnSearch extract response: {exc}") from exc
            out: List[Dict[str, Any]] = []
            by_url = {}
            for r in data.get("results") or []:
                if isinstance(r, dict):
                    by_url[str(r.get("url") or "")] = r
            for u in urls:
                r = by_url.get(u)
                content = str((r or {}).get("raw_content") or (r or {}).get("content") or "")
                if not content:
                    out.append(page_error(u, "UnSearch returned no content for this URL"))
                else:
                    out.append(document(u, str((r or {}).get("title") or ""), content))
            served = sum(1 for r in out if not r.get("error"))
            _meter.log(route="unsearch", endpoint="contents", ok=served > 0,
                       status=None if served > 0 else -1, latency_ms=latency_ms,
                       req_summary=_meter.summarize_urls(urls),
                       resp_bytes=sum(len(str(r.get("content") or "")) for r in out),
                       result_count=served,
                       key_fp=_meter.key_fingerprint(_headers()["X-API-Key"]))
            if served == 0 and out:
                raise RuntimeError(f"UnSearch extract failed all {len(urls)} URL(s): {out[0].get('error')}")
            return out

        return run_extract("UnSearch", logger, urls, _body)

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "UnSearch", "custom", "Web search + research API (Tavily/Exa-compatible shapes, 5K calls/month free).",
            "UNSEARCH_API_KEY", "UnSearch API key", "https://unsearch.dev",
        )
