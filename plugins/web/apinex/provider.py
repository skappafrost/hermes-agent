"""APInex web search + extract via the APInex tools API (https://api.apinex.bond).

Env: ``APINEX_API_KEY`` (sk-apx..., from https://apinex.bond). Both methods are sync-first;
``extract`` is async (delegates to Firecrawl's async extract on fallback).

Upstream endpoints (measured 2026-09-09):
    POST /v1/tools/web/search     {"query", "count" 1-100, "offset", "freshness", ...}
    POST /v1/tools/web/contents   {"urls": [max 20], "formats": ["markdown"|"html"]}
Response shapes:
    search   -> {"results": {"web": [{url, title, description, snippets, page_age, ...}]}, "usage"}
    contents -> {"results": [{url, markdown, html, title, metadata}], "usage"}

Failure policy (Skappa, 2026-09-10): APInex is the primary backend; on any failure
(network/timeout/5xx/4xx incl. auth) calls fall back to the previous combo —
Exa for search, Firecrawl for extract — before giving up. Disable via
``web.apinex_fallback: false``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

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


def _apinex_post(path: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """POST JSON to an APInex tools endpoint; raises RuntimeError on any failure shape."""
    api_key = provider_env("APINEX_API_KEY")
    if not api_key:
        raise ValueError(_MISSING_KEY)
    try:
        resp = httpx.post(
            f"{_base_url()}{path}",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        raise RuntimeError(f"could not reach APInex: {exc}") from exc
    if resp.status_code >= 400:
        # APInex errors: {"error": {"message": ..., "type": ...}} — surface the message when present.
        detail = ""
        try:
            err = (resp.json() or {}).get("error") or {}
            detail = str(err.get("message") or "") if isinstance(err, dict) else str(err)
        except Exception:  # noqa: BLE001 — body may not be JSON
            pass
        raise RuntimeError(f"APInex returned HTTP {resp.status_code}{' — ' + detail[:200] if detail else ''}")
    try:
        return resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not parse APInex response as JSON: {exc}") from exc


class ApinexWebSearchProvider(BaseWebSearchProvider):
    """APInex search + extract provider with automatic fallback to the legacy combo."""

    NAME = "apinex"
    DISPLAY_NAME = "APInex"
    KEY_ENV = "APINEX_API_KEY"
    EXTRACT = True
    KEYLESS = False

    # ---- search ----------------------------------------------------------

    def search(self, query: str, limit: int = 5) -> Dict[str, Any]:
        return run_search("APInex", logger, lambda: self._search_body(query, limit))

    def _search_body(self, query: str, limit: int) -> Dict[str, Any]:
        logger.info("APInex search: '%s' (limit=%d)", query, limit)
        try:
            data = _apinex_post(
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
        return search_ok(hits)

    def _fallback_search(self, query: str, limit: int, apinex_error: str) -> Dict[str, Any]:
        """Serve this call via the Exa provider (keyed or keyless, same as pre-APInex setup)."""
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
        from tools.interrupt import is_interrupted

        if is_interrupted():
            return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]
        format = kwargs.get("format")
        formats = [format] if format in ("markdown", "html") else ["markdown"]
        logger.info("APInex extract: %d URL(s)", len(urls))
        try:
            body = await asyncio.to_thread(
                _apinex_post,
                "/tools/web/contents",
                {"urls": list(urls), "formats": formats},
                _CONTENTS_TIMEOUT,
            )
        except ValueError:
            raise  # missing key: verbatim, no fallback
        except Exception as exc:
            return await self._fallback_extract(urls, format, str(exc))

        results_raw = body.get("results") or []
        by_url: Dict[str, Dict[str, Any]] = {}
        for r in results_raw:
            if not isinstance(r, dict):
                continue
            content = str(r.get("markdown") or r.get("html") or "")
            by_url[str(r.get("url") or "")] = document(
                str(r.get("url") or ""), str(r.get("title") or ""), content,
            )
        results = []
        for u in urls:
            entry = by_url.get(u)
            if entry is None or not (entry.get("content") or entry.get("raw_content")):
                results.append(page_error(u, "APInex returned no content for this URL"))
            else:
                results.append(entry)

        # Whole-batch failure = outage, not per-page problems → try Firecrawl.
        if results and all(r.get("error") for r in results):
            logger.warning("APInex extract failed all %d URL(s); falling back to Firecrawl", len(urls))
            return await self._fallback_extract(urls, format, "all URLs failed")

        # Patch partial failures per-URL via Firecrawl (best-effort, keeps successes).
        failed_idx = [i for i, r in enumerate(results) if r.get("error")]
        if failed_idx and _fallback_enabled():
            rescued = await self._fc_extract([urls[i] for i in failed_idx], format)
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
            "APInex extract failed (%s); falling back to Firecrawl for this call",
            apinex_error[:200],
        )
        rescued = await self._fc_extract(urls, format)
        for r in rescued:
            if not r.get("error"):
                meta = r.setdefault("metadata", {})
                if isinstance(meta, dict):
                    meta["fallback_from"] = "apinex"
                    meta["backend_error"] = (
                        f"APInex failed this call ({apinex_error[:300]}); served by the Firecrawl fallback."
                    )
        return rescued

    @staticmethod
    async def _fc_extract(urls: List[str], format: Optional[str]) -> List[Dict[str, Any]]:
        from plugins.web._common import extract_fail, provider_env as _penv

        # Docker rescue: when Firecrawl local is the target and it is down,
        # bring the stack up (Docker Desktop → compose) before delegating.
        api_url = (_penv("FIRECRAWL_API_URL") or "").strip()
        if "localhost" in api_url or "127.0.0.1" in api_url:
            from plugins.web.apinex.docker_rescue import ensure_firecrawl_local

            healthy = await asyncio.to_thread(ensure_firecrawl_local)
            if not healthy:
                return extract_fail(
                    urls,
                    "Firecrawl local is down and the Docker rescue could not bring it up "
                    "(see web.apinex_docker_rescue; check Docker Desktop / the firecrawl stack)",
                )
        try:
            from plugins.web.firecrawl.provider import FirecrawlWebSearchProvider

            return await FirecrawlWebSearchProvider().extract(urls, format=format)
        except Exception as exc:  # noqa: BLE001 — fallback is best-effort
            return extract_fail(urls, f"Firecrawl fallback failed: {exc}")

    # ---- picker ---------------------------------------------------------

    def get_setup_schema(self) -> Dict[str, Any]:
        return setup_schema(
            "APInex", "api-key",
            "Web search + clean page extraction via APInex tools API (fast; renders SPAs; free web-tools tier). "
            "Automatic fallback to Exa/Firecrawl on failure.",
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
