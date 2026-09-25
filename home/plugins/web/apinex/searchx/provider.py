"""SearchX (searchx.dev) — hybrid web search + page extraction, keyless-free tier.

Env: ``SEARCHX_API_KEY`` (https://searchx.dev — 3,000 req/day, 60 req/min free).
API note: accepts BOTH ``Authorization: Bearer`` and ``X-API-Key`` (verified
2026-09-15); we send X-API-Key. The extract endpoint is per-URL, so batches
loop one request per URL and a whole-batch failure raises so the APInex
chain can move on to the next tier. Search snippets carry
``<span class="searchmatch">`` highlight markup — stripped before display.

Every attempt is metered into the shared pool dashboard under
``route='searchx'`` when the tracker is importable.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List

import httpx

from plugins.web._common import (
    BaseWebSearchProvider, document, extract_fail, page_error, provider_env,
    run_extract, run_search, search_fail, search_ok, setup_schema, web_hit,
)
from .. import meter as _meter

logger = logging.getLogger(__name__)

_MISSING_KEY = "SEARCHX_API_KEY environment variable not set. Create a free key at https://searchx.dev"
_BASE = "https://searchx.dev/api/v1"
_SEARCH_TIMEOUT = 20.0
_EXTRACT_TIMEOUT = 30.0
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return _TAG_RE.sub("", str(text or "")).strip()


def _headers() -> Dict[str, str]:
    return {"X-API-Key": provider_env("SEARCHX_API_KEY") or ""}


class SearchxWebSearchProvider(BaseWebSearchProvider):
    """SearchX search + extract provider (tier-2 fallback, self-metered)."""

    NAME = "searchx"
    DISPLAY_NAME = "SearchX"
    KEY_ENV = "SEARCHX_API_KEY"
    EXTRACT = True

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        def _body() -> Dict[str, Any]:
            logger.info("SearchX search: '%s' (limit=%d)", query, limit)
            t0 = time.monotonic()
            try:
                resp = httpx.get(
                    f"{_BASE}/search",
                    params={"q": query, "per_page": max(1, min(int(limit), 100))},
                    headers=_headers(),
                    timeout=_SEARCH_TIMEOUT,
                )
            except httpx.RequestError as exc:
                _meter.log(route="searchx", endpoint="search", ok=False, status=None,
                           latency_ms=(time.monotonic() - t0) * 1000,
                           req_summary=_meter.summarize_search(query, limit))
                return search_fail(f"SearchX unreachable: {exc}")
            latency_ms = (time.monotonic() - t0) * 1000
            remaining = _meter.remaining_from_headers(resp.headers)
            if resp.status_code >= 400:
                detail = _error_detail(resp)
                _meter.log(route="searchx", endpoint="search", ok=False, status=resp.status_code,
                           latency_ms=latency_ms, limit_remaining=remaining,
                           req_summary=_meter.summarize_search(query, limit))
                return search_fail(f"SearchX returned HTTP {resp.status_code}{' — ' + detail[:200] if detail else ''}")
            try:
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                _meter.log(route="searchx", endpoint="search", ok=False, status=resp.status_code,
                           latency_ms=latency_ms, req_summary=_meter.summarize_search(query, limit))
                return search_fail(f"could not parse SearchX response: {exc}")
            hits = [
                web_hit(str(r.get("url") or ""), str(r.get("title") or ""),
                        _clean(r.get("snippet"))[:1500], i + 1)
                for i, r in enumerate((data.get("results") or [])[:limit])
            ]
            _meter.log(route="searchx", endpoint="search", ok=True, status=resp.status_code,
                       latency_ms=latency_ms, limit_remaining=remaining,
                       req_summary=_meter.summarize_search(query, limit),
                       resp_bytes=_meter.size_of(data), result_count=len(hits),
                       key_fp=_meter.key_fingerprint(_headers()["X-API-Key"]))
            return search_ok(hits)

        return run_search("SearchX", logger, _body)

    def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        def _body() -> List[Dict[str, Any]]:
            logger.info("SearchX extract: %d URL(s)", len(urls))
            out: List[Dict[str, Any]] = []
            served = 0
            t0 = time.monotonic()
            for u in urls:
                try:
                    resp = httpx.post(
                        f"{_BASE}/extract",
                        json={"url": u, "formats": ["markdown"]},
                        headers=_headers(),
                        timeout=_EXTRACT_TIMEOUT,
                    )
                except httpx.RequestError as exc:
                    out.append(page_error(u, f"SearchX unreachable: {exc}"))
                    continue
                if resp.status_code >= 400:
                    out.append(page_error(u, f"SearchX returned HTTP {resp.status_code} — {_error_detail(resp)[:200]}"))
                    continue
                try:
                    data = resp.json()
                except Exception as exc:  # noqa: BLE001
                    out.append(page_error(u, f"could not parse SearchX response: {exc}"))
                    continue
                content = str(data.get("markdown") or data.get("text") or "")
                if not content:
                    out.append(page_error(u, "SearchX returned no content for this URL"))
                    continue
                served += 1
                out.append(document(str(data.get("url") or u), str(data.get("title") or ""), content))
            _meter.log(route="searchx", endpoint="contents", ok=served > 0,
                       status=None if served > 0 else -1,
                       latency_ms=(time.monotonic() - t0) * 1000,
                       req_summary=_meter.summarize_urls(urls),
                       resp_bytes=sum(len(str(r.get("content") or "")) for r in out),
                       result_count=served,
                       key_fp=_meter.key_fingerprint(_headers()["X-API-Key"]))
            if served == 0 and out:
                # Whole-batch failure — surface as an error so the APInex
                # chain treats this tier as down and moves to the next.
                raise RuntimeError(f"SearchX extract failed all {len(urls)} URL(s): {out[0].get('error')}")
            return out

        return run_extract("SearchX", logger, urls, _body)

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "SearchX", "custom", "Hybrid keyword + semantic web search with JS-rendered page extraction (3K req/day free tier).",
            "SEARCHX_API_KEY", "SearchX API key", "https://searchx.dev",
        )


def _error_detail(resp: Any) -> str:
    try:
        err = (resp.json() or {}).get("error") or {}
        return str(err.get("message") or err) if isinstance(err, dict) else str(err)
    except Exception:  # noqa: BLE001 — body may not be JSON
        return ""
