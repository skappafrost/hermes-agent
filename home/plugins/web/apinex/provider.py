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


def _apinex_post(path: str, payload: Dict[str, Any], timeout: float,
                 call_id: Optional[int] = None) -> Tuple[Dict[str, Any], int]:
    """POST JSON to an APInex tools endpoint through the key pool.

    Returns ``(data, key_number)``. Raises :class:`ValueError` when no key is
    configured (verbatim config error, no fallback) and :class:`RuntimeError`
    for anything else (callers fall back to Exa). ``call_id`` ties every
    attempt of one logical call together for the meter dashboard.
    """
    from . import keypool as _pool
    from . import tracker as _meter

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
                                   req_summary=summary, call_id=call_id)
                raise RuntimeError(f"could not reach APInex: {exc}") from exc
            latency_ms = (time.monotonic() - t0) * 1000
            remaining, reset = _meter.parse_limit_headers(resp.headers)
            if resp.status_code < 400:
                try:
                    data = resp.json()
                except Exception as exc:  # noqa: BLE001
                    _meter.log_request(profile=profile, key_no=key_no, key_fp=fp,
                                       endpoint=endpoint, ok=False, status=resp.status_code,
                                       latency_ms=latency_ms, call_id=call_id)
                    raise RuntimeError(f"could not parse APInex response as JSON: {exc}") from exc
                rcount, rbytes = _summarize_response(path, data)
                _meter.log_request(profile=profile, key_no=key_no, key_fp=fp,
                                   endpoint=endpoint, ok=True, status=resp.status_code,
                                   latency_ms=latency_ms,
                                   limit_remaining=remaining, limit_reset_s=reset,
                                   req_summary=summary,
                                   resp_bytes=rbytes, result_count=rcount, call_id=call_id)
                return data, key_no
            detail = _error_detail(resp)
            last_err = f"APInex returned HTTP {resp.status_code}{' — ' + detail[:200] if detail else ''}"
            if resp.status_code in (401, 403):
                _pool.mark_bad(key_no, detail or last_err)
                _meter.log_request(profile=profile, key_no=key_no, key_fp=fp,
                                   endpoint=endpoint, ok=False, status=resp.status_code,
                                   latency_ms=latency_ms,
                                   req_summary=summary, call_id=call_id)
                continue  # next key, no sleep: this key is unusable, not limited
            if resp.status_code == 429:
                round_had_429 = True
                _meter.log_request(profile=profile, key_no=key_no, key_fp=fp,
                                   endpoint=endpoint, ok=False, status=429,
                                   latency_ms=latency_ms,
                                   limit_remaining=remaining, limit_reset_s=reset,
                                   req_summary=summary, call_id=call_id)
                continue  # next key immediately; sleep only between rounds
            _meter.log_request(profile=profile, key_no=key_no, key_fp=fp,
                               endpoint=endpoint, ok=False, status=resp.status_code,
                               latency_ms=latency_ms,
                               req_summary=summary, call_id=call_id)
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
        from . import keypool as _pool

        return _pool.pool_size() > 0

    # ---- search ----------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        return run_search("APInex", logger, lambda: self._search_body(query, limit))

    def _search_body(self, query: str, limit: int) -> Dict[str, Any]:
        from . import keypool as _pool
        from . import tracker as _meter

        logger.info("APInex search: '%s' (limit=%d)", query, limit)
        with _meter.ensure_call_scope() as call_id:
            try:
                data, key_no = _apinex_post(
                    "/tools/web/search",
                    {"query": query, "count": max(1, min(int(limit), 100))},
                    _SEARCH_TIMEOUT,
                    call_id=call_id,
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

    # ---- fallback chain --------------------------------------------------
    #
    # APInex fails -> walk the tiers in order; the first vendor that serves
    # wins. ``web.apinex_fallback_tiers`` (config.yaml) overrides the default
    # "searchx,unsearch,exa"; each vendor's own provider class meters its
    # attempt under ``route=<vendor>``, so the dashboard sees the full chain.
    # Tiers without a configured key are skipped instantly (no HTTP attempt).

    _DEFAULT_TIERS = ("searchx", "unsearch", "exa")

    def _tier_names(self) -> List[str]:
        try:
            from hermes_cli.config import load_config_readonly

            raw = (load_config_readonly().get("web") or {}).get("apinex_fallback_tiers")
            if raw:
                return [str(t).strip() for t in str(raw).split(",") if str(t).strip()]
        except Exception as exc:  # noqa: BLE001 — config layer optional
            logger.debug("apinex_fallback_tiers config read failed: %s", exc)
        return list(self._DEFAULT_TIERS)

    def _fallback_search(self, query: str, limit: int, apinex_error: str) -> Dict[str, Any]:
        """Serve this call through the fallback chain (tier order per config)."""
        if not _fallback_enabled():
            return search_fail(f"APInex search failed: {apinex_error}")
        errors = [f"apinex: {apinex_error[:160]}"]
        for vendor in self._tier_names():
            resp = self._try_tier_search(vendor, query, limit, apinex_error, errors)
            if resp is not None:
                return resp
        return search_fail("APInex search failed and every fallback tier failed: " + " | ".join(errors))

    def _try_tier_search(self, vendor: str, query: str, limit: int,
                         apinex_error: str, errors: List[str]) -> Dict[str, Any]:
        """One chain tier: returns a served response, or None to continue walking.

        Metering rule: if the tier provider self-metered its attempt
        (searchx/unsearch do), the ambient attempt counter moves and we add
        nothing; otherwise (exa and any future un-metered tier) we log one
        row under ``route=<vendor>`` here so EVERY chain hop is visible on
        the dashboard.
        """
        from . import tracker as _meter

        provider = self._instantiate_tier(vendor, errors)
        if provider is None:
            return None
        t0 = time.monotonic()
        attempts_before = _meter.attempt_count()
        try:
            resp = provider.search(query, limit)
        except Exception as exc:  # noqa: BLE001 — fallback tiers are best-effort
            resp, err_text = None, str(exc)
        else:
            err_text = str(resp.get("error") or "") if not resp.get("success") else ""
        ok = bool(resp and resp.get("success"))
        if _meter.attempt_count() == attempts_before:
            hits = ((resp.get("data") or {}).get("web")) or [] if ok else []
            _meter.log_request(key_no=0, key_fp=vendor, endpoint="search", ok=ok,
                               status=None if ok else (-1 if resp is None else resp.get("status")),
                               latency_ms=(time.monotonic() - t0) * 1000,
                               req_summary=f"q={query[:200]} n={limit}",
                               resp_bytes=len(json.dumps(resp or {}, default=str)) if ok else None,
                               result_count=len(hits) if ok else None, route=vendor)
        if not ok:
            errors.append(f"{vendor}: {(err_text or 'no results')[:160]}")
            logger.warning("APInex fallback tier '%s' failed (%s); continuing chain",
                           vendor, err_text[:160])
            return None
        resp.setdefault("data", {})["fallback_from"] = "apinex"
        resp["data"]["served_by"] = vendor
        resp["data"]["backend_error"] = (
            f"APInex failed this call ({apinex_error[:300]}); result served by the {vendor} fallback."
        )
        return resp

    def _instantiate_tier(self, vendor: str, errors: List[str]):
        """Provider instance for a chain tier, or None when it is unusable
        (unknown name, no key, import failure — each recorded in *errors*)."""
        _TIERS = {
            # A leading dot resolves against this package: searchx and unsearch live
            # inside it now, and are no longer importable as ``plugins.web.*`` once the
            # plugin is loaded from ~/.hermes/plugins under a synthetic namespace.
            "searchx": (".searchx.provider", "SearchxWebSearchProvider", "SEARCHX_API_KEY"),
            "unsearch": (".unsearch.provider", "UnsearchWebSearchProvider", "UNSEARCH_API_KEY"),
            "exa": ("plugins.web.exa.provider", "ExaWebSearchProvider", None),
        }
        spec = _TIERS.get(vendor)
        if spec is None:
            logger.debug("unknown apinex fallback tier '%s' (skipped)", vendor)
            return None
        module, cls_name, key_env = spec
        try:
            if key_env:
                from plugins.web._common import provider_env

                if not provider_env(key_env):
                    errors.append(f"{vendor}: no API key configured")
                    return None
            import importlib

            mod = importlib.import_module(
                module, __package__ if module.startswith(".") else None)
            provider = getattr(mod, cls_name)()
            if not provider.supports_search():
                errors.append(f"{vendor}: search unsupported")
                return None
            return provider
        except Exception as exc:  # noqa: BLE001 — a broken tier must not kill the chain
            errors.append(f"{vendor}: import failed ({exc})")
            logger.warning("APInex fallback tier '%s' cannot load: %s", vendor, exc)
            return None

    # ---- extract ---------------------------------------------------------

    async def extract(self, urls: List[str], **kwargs: Any) -> List[Dict[str, Any]]:
        from . import keypool as _pool
        from . import tracker as _meter
        from tools.interrupt import is_interrupted

        if is_interrupted():
            return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]
        format = kwargs.get("format")
        formats = [format] if format in ("markdown", "html") else ["markdown"]
        logger.info("APInex extract: %d URL(s)", len(urls))
        with _meter.ensure_call_scope():
            try:
                body, key_no = await asyncio.to_thread(
                    _apinex_post,
                    "/tools/web/contents",
                    {"urls": list(urls), "formats": formats},
                    _CONTENTS_TIMEOUT,
                )
            except ValueError:
                raise  # missing key: verbatim, no fallback (config error, not outage)
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

        # Whole-batch failure = outage, not per-page problems -> walk the chain.
        if results and all(r.get("error") for r in results):
            logger.warning("APInex extract failed all %d URL(s); entering fallback chain", len(urls))
            return await self._fallback_extract(urls, format, "all URLs failed")

        # Patch partial failures per-URL via the chain (best-effort, keeps successes).
        failed_idx = [i for i, r in enumerate(results) if r.get("error")]
        if failed_idx and _fallback_enabled():
            rescued = await self._chain_extract([urls[i] for i in failed_idx], format)
            if rescued and not all(r.get("error") for r in rescued):
                for pos, i2 in enumerate(failed_idx):
                    if not rescued[pos].get("error"):
                        meta = rescued[pos].setdefault("metadata", {})
                        if isinstance(meta, dict):
                            meta["fallback_from"] = "apinex"
                        results[i2] = rescued[pos]
        return results

    async def _fallback_extract(
        self, urls: List[str], format: Optional[str], apinex_error: str,
    ) -> List[Dict[str, Any]]:
        from plugins.web._common import extract_fail

        if not _fallback_enabled():
            return extract_fail(urls, f"APInex extract failed: {apinex_error}")
        logger.warning(
            "APInex extract failed (%s); entering the fallback chain for this call",
            apinex_error[:200],
        )
        rescued = await self._chain_extract(urls, format)
        for r in rescued:
            if not r.get("error"):
                meta = r.setdefault("metadata", {})
                if isinstance(meta, dict):
                    meta["fallback_from"] = "apinex"
                    meta["backend_error"] = (
                        f"APInex failed this call ({apinex_error[:300]}); "
                        f"served by the {meta.get('served_by', 'fallback')} fallback."
                    )
        return rescued

    async def _chain_extract(self, urls: List[str], format: Optional[str]) -> List[Dict[str, Any]]:
        """Walk the fallback tiers with the sync extract(); first responder wins.

        Same metering rule as the search chain: self-metered tiers (searchx,
        unsearch) log their own row; exa (silent provider) is logged here
        under ``route='exa'`` so every hop lands on the dashboard.
        """
        from plugins.web._common import extract_fail
        from . import tracker as _meter

        errors: List[str] = []
        for vendor in self._tier_names():
            provider = self._instantiate_tier_extract(vendor, errors)
            if provider is None:
                continue
            t0 = time.monotonic()
            attempts_before = _meter.attempt_count()
            try:
                # All tier extracts are sync — thread them so the loop never blocks.
                results = await asyncio.to_thread(provider.extract, urls, format=format)
            except Exception as exc:  # noqa: BLE001 — fallback tiers are best-effort
                results, err_text = None, str(exc)
            else:
                err_text = "all URLs failed" if results and all(r.get("error") for r in results) else ""
            served = sum(1 for r in (results or []) if not r.get("error"))
            ok = served > 0
            if _meter.attempt_count() == attempts_before:
                first = str(urls[0])[:150] if urls else "-"
                _meter.log_request(key_no=0, key_fp=vendor, endpoint="contents", ok=ok,
                                   status=None if ok else -1,
                                   latency_ms=(time.monotonic() - t0) * 1000,
                                   req_summary=f"{len(urls)} url(s): {first}",
                                   resp_bytes=sum(len(str(r.get("content") or "")) for r in (results or [])) if ok else None,
                                   result_count=served if ok else None, route=vendor)
            if not ok:
                errors.append(f"{vendor}: {(err_text or 'no content')[:160]}")
                logger.warning("APInex extract fallback tier '%s' failed (%s); continuing chain",
                               vendor, err_text[:160])
                continue
            for r in results:
                if not r.get("error"):
                    meta = r.setdefault("metadata", {})
                    if isinstance(meta, dict):
                        meta["served_by"] = vendor
            return results
        return extract_fail(urls, "APInex extract failed and the fallback chain exhausted: "
                                   + " | ".join(errors[:4]))

    def _instantiate_tier_extract(self, vendor: str, errors: List[str]):
        """Like _instantiate_tier but gated on supports_extract() instead."""
        provider = self._instantiate_tier(vendor, errors)
        if provider is None:
            return None
        if not provider.supports_extract():
            errors.append(f"{vendor}: extract unsupported")
            return None
        return provider

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
