"""APInex web search + extract via the APInex tools API (https://api.apinex.bond).

Env: ``APINEX_API_KEY`` + ``APINEX_API_KEY_2`` ... ``_9`` (round-robin pool,
see ``keypool``). Both methods are sync-first; ``extract`` is async.

Upstream endpoints (measured 2026-09-09):
    POST /v1/tools/web/search     {"query", "count" 1-100, "offset", "freshness", ...}
    POST /v1/tools/web/contents   {"urls": [max 20], "formats": ["markdown"|"html"]}
Response shapes:
    search   -> {"results": {"web": [{url, title, description, snippets, page_age, ...}]}, "usage"}
    contents -> {"results": [{url, markdown, html, title, metadata}], "usage"}

Failure policy (Skappa, 2026-09-11; Docker uninstalled, no local stack):
- Round-robin pool rotation per request; HTTP 429 -> next key immediately;
  401/403 -> quarantine that key 10 min, next key. A full pool round of 429s
  sleeps a short backoff (1,2,3,4,5s) and retries, up to 1 + 5 rounds, to
  ride out the 60s rate-limit window instead of failing at once.
- Network/timeout/5xx/other-4xx are not key problems -> immediate fallback.
- Fallback is Exa only (keyed or keyless, the remaining built-in path).
  Disable via ``web.apinex_fallback: false``.
- Every APInex HTTP attempt is metered into the shared SQLite meter (tracker)
  for the dashboard (per-key rolling usage vs the per-minute limits).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from plugins.web._common import (
    BaseWebSearchProvider, document, page_error, provider_env, run_search,
    search_fail, search_ok, setup_schema, web_hit,
)

logger = logging.getLogger(__name__)

_MISSING_KEY = "APINEX_API_KEY environment variable not set. Create a key at https://apinex.bond"
_DEFAULT_BASE = "https://api.apinex.bond/v1"
_SEARCH_TIMEOUT = 30.0
_CONTENTS_TIMEOUT = 120.0
_DESC_CAP = 1500  # keep per-hit description payloads lean

# Pool-exhausted retry: 1 initial round + 5 backoff retries (Skappa 2026-09-11).
# Sleeps sit between full-pool rounds; worst case adds ~15s to one call.
_RETRY_ROUNDS = 6
_RETRY_BACKOFF_S = (1.0, 2.0, 3.0, 4.0, 5.0)

_ENDPOINT_BY_PATH = {
    "/tools/web/search": "search",
    "/tools/web/contents": "contents",
    "/tools/web/research": "research",
}


def _base_url() -> str:
    return (provider_env("APINEX_BASE_URL") or _DEFAULT_BASE).rstrip("/")


def _fallback_enabled() -> bool:
    """``web.apinex_fallback`` (default: enabled)."""
    try:
        from hermes_cli.config import load_config_readonly

        web_cfg = (load_config_readonly().get("web") or {})
        return bool(web_cfg.get("apinex_fallback", True))
    except Exception as exc:  # noqa: BLE001 — config layer optional
        logger.debug("apinex_fallback config read failed: %s", exc)
        return True


def _error_detail(resp: Any) -> str:
    # APInex errors: {"error": {"message": ..., "type": ...}} — surface the message when present.
    try:
        err = (resp.json() or {}).get("error") or {}
        return str(err.get("message") or "") if isinstance(err, dict) else str(err)
    except Exception:  # noqa: BLE001 — body may not be JSON
        return ""


def _summarize_request(path: str, payload: Dict[str, Any]) -> str:
    """One-line human summary of what was asked (stored in the meter)."""
    try:
        if path == "/tools/web/search":
            return f"q={str(payload.get('query') or '')[:200]} n={payload.get('count')}"
        if path == "/tools/web/contents":
            urls = payload.get("urls") or []
            first = str(urls[0])[:150] if urls else "-"
            return f"{len(urls)} url(s): {first}"
        if path == "/tools/web/research":
            return f"[{payload.get('research_effort')}] {str(payload.get('input') or '')[:200]}"
    except Exception:  # noqa: BLE001
        pass
    return str(path)


def _summarize_response(path: str, data: Dict[str, Any]) -> Tuple[Optional[int], int]:
    """``(result_count, resp_bytes)`` for the meter."""
    try:
        raw = json.dumps(data, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        raw = ""
    count: Optional[int] = None
    try:
        if path == "/tools/web/search":
            count = len(((data.get("results") or {}).get("web")) or [])
        elif path == "/tools/web/contents":
            count = len(data.get("results") or [])
        elif path == "/tools/web/research":
            count = len((data.get("output") or {}).get("sources") or [])
    except Exception:  # noqa: BLE001
        count = None
    return count, len(raw)


def _apinex_post(path: str, payload: Dict[str, Any], timeout: float) -> Tuple[Dict[str, Any], int]:
    """POST JSON to an APInex tools endpoint through the key pool.

    Returns ``(data, key_number)``. Raises :class:`ValueError` when no key is
    configured (verbatim config error, no fallback) and :class:`RuntimeError`
    for anything else (callers fall back to Exa).
    """
    from plugins.web.apinex import keypool as _pool
    from plugins.web.apinex import tracker as _meter

    endpoint = _ENDPOINT_BY_PATH.get(path, path.strip("/").replace("/", "_") or "?")
    profile = _pool.profile_name()
    pool_n = _pool.pool_size()
    if pool_n == 0:
        raise ValueError(_MISSING_KEY)

    last_err = "unknown error"
    summary = _summarize_request(path, payload)
    for round_i in range(_RETRY_ROUNDS):
        round_had_429 = False
        for _ in range(max(_pool.pool_size(), 1)):
            try:
                key_no, api_key = _pool.next_key()
            except _pool.NoKeysAvailable:
                raise ValueError(_MISSING_KEY)
            fp = _pool.key_fingerprint(api_key)
            t0 = time.monotonic()
            try:
                resp = httpx.post(
                    f"{_base_url()}{path}",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=timeout,
                )
            except httpx.RequestError as exc:
                _meter.log_request(profile=profile, key_no=key_no, key_fp=fp,
                                   endpoint=endpoint, ok=False, status=None,
                                   latency_ms=(time.monotonic() - t0) * 1000,
                                   req_summary=summary)
                raise RuntimeError(f"could not reach APInex: {exc}") from exc
            latency_ms = (time.monotonic() - t0) * 1000
            remaining, reset = _meter.parse_limit_headers(resp.headers)
            if resp.status_code < 400:
                try:
                    data = resp.json()
                except Exception as exc:  # noqa: BLE001
                    _meter.log_request(profile=profile, key_no=key_no, key_fp=fp,
                                       endpoint=endpoint, ok=False, status=resp.status_code,
                                       latency_ms=latency_ms)
                    raise RuntimeError(f"could not parse APInex response as JSON: {exc}") from exc
                rcount, rbytes = _summarize_response(path, data)
                _meter.log_request(profile=profile, key_no=key_no, key_fp=fp,
                                   endpoint=endpoint, ok=True, status=resp.status_code,
                                   latency_ms=latency_ms,
                                   limit_remaining=remaining, limit_reset_s=reset,
                                   req_summary=summary,
                                   resp_bytes=rbytes, result_count=rcount)
                return data, key_no
            detail = _error_detail(resp)
            last_err = f"APInex returned HTTP {resp.status_code}{' — ' + detail[:200] if detail else ''}"
            if resp.status_code in (401, 403):
                _pool.mark_bad(key_no, detail or last_err)
                _meter.log_request(profile=profile, key_no=key_no, key_fp=fp,
                                   endpoint=endpoint, ok=False, status=resp.status_code,
                                   latency_ms=latency_ms,
                                   req_summary=summary)
                continue  # next key, no sleep: this key is unusable, not limited
            if resp.status_code == 429:
                round_had_429 = True
                _meter.log_request(profile=profile, key_no=key_no, key_fp=fp,
                                   endpoint=endpoint, ok=False, status=429,
                                   latency_ms=latency_ms,
                                   limit_remaining=remaining, limit_reset_s=reset,
                                   req_summary=summary)
                continue  # next key immediately; sleep only between rounds
            _meter.log_request(profile=profile, key_no=key_no, key_fp=fp,
                               endpoint=endpoint, ok=False, status=resp.status_code,
                               latency_ms=latency_ms,
                               req_summary=summary)
            raise RuntimeError(last_err)  # not key-related -> fallback path now
        # Full pool round done. Only 429-storms earn another round after a nap;
        # anything else (all keys quarantined, ...) fails over immediately.
        if round_had_429 and round_i < _RETRY_ROUNDS - 1:
            nap = _RETRY_BACKOFF_S[min(round_i, len(_RETRY_BACKOFF_S) - 1)]
            logger.warning(
                "APInex rate-limited on all %d key(s); retry round %d/%d after %.0fs",
                pool_n, round_i + 2, _RETRY_ROUNDS, nap,
            )
            time.sleep(nap)
            continue
        break
    raise RuntimeError(f"{last_err} (pool of {pool_n} key(s) exhausted after {_RETRY_ROUNDS} rounds)")


class ApinexWebSearchProvider(BaseWebSearchProvider):
    """APInex search + extract provider: key pool + retry, Exa fallback."""

    NAME = "apinex"
    DISPLAY_NAME = "APInex"
    KEY_ENV = "APINEX_API_KEY"
    EXTRACT = True
    KEYLESS = False

    def is_available(self) -> bool:
        """Available when at least one pool key is configured."""
        from plugins.web.apinex import keypool as _pool

        return _pool.pool_size() > 0

    # ---- search ----------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        return run_search("APInex", logger, lambda: self._search_body(query, limit))

    def _search_body(self, query: str, limit: int) -> Dict[str, Any]:
        from plugins.web.apinex import keypool as _pool

        logger.info("APInex search: '%s' (limit=%d)", query, limit)
        try:
            data, key_no = _apinex_post(
                "/tools/web/search",
                {"query": query, "count": max(1, min(int(limit), 100))},
                _SEARCH_TIMEOUT,
            )
        except ValueError:
            raise  # missing key: verbatim, no fallback (config error, not outage)
        except Exception as exc:
            return self._fallback_search(query, limit, str(exc))

        hits_raw = ((data.get("results") or {}).get("web")) or []
        hits = []
        for i, r in enumerate(hits_raw):
            desc = str(r.get("description") or "")
            if not desc:
                snippets = r.get("snippets") or []
                desc = snippets[0] if snippets else ""
            hits.append(web_hit(
                str(r.get("url") or ""),
                str(r.get("title") or ""),
                desc[:_DESC_CAP],
                i + 1,
            ))
        resp = search_ok(hits)
        resp["data"]["apinex_key"] = _pool.fingerprint_of(key_no)
        return resp

    def _fallback_search(self, query: str, limit: int, apinex_error: str) -> Dict[str, Any]:
        """Serve this call via the Exa provider (keyed or keyless) — the built-in fallback."""
        if not _fallback_enabled():
            return search_fail(f"APInex search failed: {apinex_error}")
        logger.warning(
            "APInex search failed (%s); falling back to Exa for this call",
            apinex_error[:200],
        )
        try:
            from plugins.web.exa.provider import ExaWebSearchProvider

            resp = ExaWebSearchProvider().search(query, limit)
        except Exception as exc:  # noqa: BLE001 — fallback is best-effort
            return search_fail(
                f"APInex search failed: {apinex_error} (Exa fallback also failed: {exc})"
            )
        if resp.get("success"):
            resp.setdefault("data", {}).setdefault("fallback_from", "apinex")
            resp["data"]["backend_error"] = (
                f"APInex failed this call ({apinex_error[:300]}); result served by the Exa fallback."
            )
        return resp

    # ---- extract ---------------------------------------------------------

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        from plugins.web.apinex import keypool as _pool
        from tools.interrupt import is_interrupted

        if is_interrupted():
            return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]
        format = kwargs.get("format")
        formats = [format] if format in ("markdown", "html") else ["markdown"]
        logger.info("APInex extract: %d URL(s)", len(urls))
        try:
            body, key_no = await asyncio.to_thread(
                _apinex_post,
                "/tools/web/contents",
                {"urls": list(urls), "formats": formats},
                _CONTENTS_TIMEOUT,
            )
        except ValueError:
            raise  # missing key: verbatim, no fallback
        except Exception as exc:
            return await self._fallback_extract(urls, format, str(exc))

        fp = _pool.fingerprint_of(key_no)
        results_raw = body.get("results") or []
        by_url: Dict[str, Dict[str, Any]] = {}
        for r in results_raw:
            if not isinstance(r, dict):
                continue
            content = str(r.get("markdown") or r.get("html") or "")
            entry = document(str(r.get("url") or ""), str(r.get("title") or ""), content)
            meta = entry.setdefault("metadata", {})
            if isinstance(meta, dict):
                meta["apinex_key"] = fp
            by_url[str(r.get("url") or "")] = entry
        results = []
        for u in urls:
            entry = by_url.get(u)
            if entry is None or not (entry.get("content") or entry.get("raw_content")):
                results.append(page_error(u, "APInex returned no content for this URL"))
            else:
                results.append(entry)

        # Whole-batch failure = outage, not per-page problems → try Exa.
        if results and all(r.get("error") for r in results):
            logger.warning("APInex extract failed all %d URL(s); falling back to Exa", len(urls))
            return await self._fallback_extract(urls, format, "all URLs failed")

        # Patch partial failures per-URL via Exa (best-effort, keeps successes).
        failed_idx = [i for i, r in enumerate(results) if r.get("error")]
        if failed_idx and _fallback_enabled():
            rescued = await self._exa_extract([urls[i] for i in failed_idx], format)
            if rescued and not all(r.get("error") for r in rescued):
                for pos, i in enumerate(failed_idx):
                    if not rescued[pos].get("error"):
                        meta = rescued[pos].setdefault("metadata", {})
                        if isinstance(meta, dict):
                            meta["fallback_from"] = "apinex"
                        results[i] = rescued[pos]
        return results

    async def _fallback_extract(
        self, urls: List[str], format: Optional[str], apinex_error: str
    ) -> List[Dict[str, Any]]:
        if not _fallback_enabled():
            from plugins.web._common import extract_fail

            return extract_fail(urls, f"APInex extract failed: {apinex_error}")
        logger.warning(
            "APInex extract failed (%s); falling back to Exa for this call",
            apinex_error[:200],
        )
        rescued = await self._exa_extract(urls, format)
        for r in rescued:
            if not r.get("error"):
                meta = r.setdefault("metadata", {})
                if isinstance(meta, dict):
                    meta["fallback_from"] = "apinex"
                    meta["backend_error"] = (
                        f"APInex failed this call ({apinex_error[:300]}); served by the Exa fallback."
                    )
        return rescued

    @staticmethod
    async def _exa_extract(urls: List[str], format: Optional[str]) -> List[Dict[str, Any]]:
        from plugins.web._common import extract_fail

        try:
            from plugins.web.exa.provider import ExaWebSearchProvider

            # Exa's extract is sync — thread it so the event loop never blocks.
            return await asyncio.to_thread(ExaWebSearchProvider().extract, urls, format=format)
        except Exception as exc:  # noqa: BLE001 — fallback is best-effort
            return extract_fail(urls, f"Exa fallback failed: {exc}")

    # ---- picker ---------------------------------------------------------

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "APInex", "api-key",
            "Web search + clean page extraction via APInex tools API (fast; renders SPAs; free web-tools tier). "
            "Round-robin key pool with retry; automatic fallback to Exa on failure.",
            "APINEX_API_KEY", "APInex API key (sk-apx...)", "https://apinex.bond",
            web_tier="paid",
        )


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
# Names external plugins imported from this module before the Sep 2026 decomposition.
# Internal code MUST NOT use these (scripts/check_compat_pointers.py fails CI if it does).
# The whole block is removed by reverting the commit that added it.
from typing import NoReturn  # noqa: F401,E402  (mirror sibling providers)


_PLUGIN_COMPAT_LAZY: Dict[str, tuple] = {
    'WebSearchProvider': ('agent.web_search_provider', 'WebSearchProvider'),
}


def __getattr__(name):  # PEP 562 — lazy so no import cycles
    target = _PLUGIN_COMPAT_LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {name!r} has no attribute {name!r}")
    import importlib
    from hermes_cli.plugin_compat import warn_once
    warn_once(__name__, name, *target)
    return getattr(importlib.import_module(target[0]), target[1])
# ---- END PLUGIN-COMPAT ----
