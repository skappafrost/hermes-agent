"""Read-only views of what the vilao console knows, so the account can be run from Hermes.

Transport is injected: the caller hands in ``usage_bill._get`` (one authenticated GET, absolute URLs
allowed) rather than this module holding a second copy of the secret resolution and the retry rules.

Everything here is a GET. The token is full-scope (vilao issues one unscoped token per account, including
``wallet:write``), so no write endpoint is ever named in this file, and the surface is deliberately
limited to what the console shows about the account itself.

Inventory probed on 2026-10-09, because the v2 console routes are undocumented and several plausible ones
simply do not exist:

- v2 answers: ``/account/balance``, ``/account/me``, ``/llm/keys``, ``/llm/keys/{id}``,
  ``/llm/keys/{id}/subscriptions``, ``/wallet/transactions``, ``/llm/usage``, ``/llm/usage/export``.
- v2 returns 404 for: ``/llm/models``, ``/llm/pricing``, ``/llm/endpoints``, ``/llm/subscriptions``,
  ``/llm/usage/stats``, ``/account/{plans,budgets,limits,invoices}``, ``/requests/{id}``.
- The model catalogue is NOT under v2. It lives on the public v1 marketplace
  (``/api/v1/llm/marketplace/models``, 786 models, no auth), which is also the only place per-model
  prices are published. Pricing unit, verified against a measured charge: **dong per 1,000,000 tokens**
  (haiku catalog 150 and promo 105 against 0.10962đ billed for 1044 input tokens).
- Two facts worth knowing before drawing a meter: the key LIST answers budget reset times with
  ``0001-01-01`` while the key DETAIL answers real times, and no route anywhere reports the current
  period's spend against a budget cap -- the cap amount itself is absent from every PAT-readable route.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote

BASE = "https://vilao.ai/api/v2"
MARKET = "https://vilao.ai/api/v1/llm/marketplace"
CATALOG_TTL_S = 6 * 3600.0


def _num(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# What the walk looks like, measured on 2026-10-09 against the live endpoint: a row is an OFFER
# (provider × model), not a model -- 822 rows hold 287 distinct model ids, because the same model is sold
# by many providers. `limit` is honoured up to 100 and then collapses back to 20 (limit=200 answers 20),
# `page_size`/`per_page`/`size` are ignored entirely, and paging by 100 across 9 pages returns all 822
# rows with zero repeats. Deduplicating by model_id -- which the first version of this walk did -- threw
# away 541 offers, which is why the Models tab looked short.
CATALOG_PAGE = 100
CATALOG_MAX_PAGES = 20


def cache_dir() -> Optional[Path]:
    """The shared store's directory: five homes reading one catalogue should mean five requests a day,
    not forty, and the answer is the same for all of them."""
    try:
        from hermes_constants import get_default_hermes_root

        return get_default_hermes_root() / "plugin-data" / "resetwatch"
    except Exception:
        return None


def offer_key(row: dict[str, Any]) -> str:
    """The identity of one catalogue row: a model as a given provider sells it."""
    return "%s/%s" % (row.get("provider_prefix") or "?", row.get("model_id") or "?")


# Model ids on this marketplace are ASCII slugs, but the values arrive over the network and end up inside
# an href in a desktop renderer. Anything outside this set gets no link rather than a link the page has to
# trust -- a "?x=" or a "../" in an id would otherwise steer the click, and the path segments are quoted
# anyway so an id cannot escape its own slot.
_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def offer_url(prefix: Any, model_id: Any) -> Optional[str]:
    """The console page for one offer, or None when either segment is not a plain slug."""
    if not _SAFE_SEGMENT.match(str(prefix or "")) or not _SAFE_SEGMENT.match(str(model_id or "")):
        return None
    return "https://vilao.ai/model/%s/%s" % (quote(str(prefix), safe=""), quote(str(model_id), safe=""))


def catalog_pages(get) -> list[dict[str, Any]]:
    """Every offer page of the public catalogue, keyed by (provider, model) rather than by model."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, CATALOG_MAX_PAGES + 1):
        data = get(MARKET + "/models", {"page": page, "limit": CATALOG_PAGE})
        if not isinstance(data, list) or not data:
            break
        fresh = 0
        for m in data:
            if not isinstance(m, dict):
                continue
            key = offer_key(m)
            if key in seen:
                continue
            seen.add(key)
            out.append(m)
            fresh += 1
        if not fresh:
            break
    return out


def catalog(get: Callable[..., Any], force: bool = False) -> dict[str, Any]:
    """The public model catalogue, cached on disk for six hours.

    This is the answer to "what could I route to, what is its context, what does it cost" -- the one thing
    the console shows that the plugin could not. It is keyless and changes slowly, so it is read at most
    four times a day and kept next to the store.
    """
    path = (cache_dir() / "catalog.json") if cache_dir() else None
    if path and not force:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - float(cached.get("at") or 0) < CATALOG_TTL_S:
                return cached
        except (OSError, ValueError):
            pass
    rows: list[dict[str, Any]] = []
    try:
        data = catalog_pages(get)
    except Exception as exc:
        return {"at": 0.0, "error": "catalog unreachable: %s" % str(exc)[:120], "models": [], "count": 0}
    for m in data:
        if not isinstance(m, dict):
            continue
        promo = m.get("promo") if isinstance(m.get("promo"), dict) else {}
        options = m.get("options") if isinstance(m.get("options"), dict) else {}
        prefix = m.get("provider_prefix") or "?"
        model_id = m.get("model_id") or "?"
        rows.append({
            "key": "%s/%s" % (prefix, model_id),
            "id": model_id,
            "name": m.get("display_name") or model_id,
            "provider": prefix,
            "provider_name": m.get("provider_name"),
            # The console's own page for this model, route read from the live site's DOM (not guessed):
            # https://vilao.ai/model/<provider prefix>/<model id>. None when a segment is not a plain
            # slug, so a network-supplied value never becomes an href on its own.
            "url": offer_url(prefix, model_id),
            "seller": m.get("seller_name") or m.get("seller_slug"),
            "kind": m.get("type"),
            "mode": m.get("pricing_mode"),
            "context": int(m.get("max_context_length") or 0) or None,
            "in": _num(m.get("price_per_input_token")),
            "cache_read": _num(m.get("price_per_cached_read_token")
                               or m.get("price_per_cached_input_token")),
            "cache_write": _num(m.get("price_per_cache_creation_token")),
            "out": _num(m.get("price_per_output_token")),
            "floor": _num(m.get("min_price_per_request")),
            "active": m.get("is_active"),
            "recommended": bool(m.get("is_recommended")),
            "tools": bool(options.get("supports_tools")),
            "vision": bool(options.get("supports_vision")),
            "requests": int(m.get("total_requests") or 0) or None,
            "success_rate": _num(m.get("success_rate")),
            "tps": _num(m.get("avg_tps")),
            "latency_ms": _num(m.get("health_check_latency_ms")),
            "score": _num(m.get("last_test_score")),
            # Vilao's own SVG-render check is what tells a reader whether the offer serves the real model
            # or a lookalike, so the three fields that describe the check travel with the score: which
            # offers are enrolled, whether the run finished, and when. Absent means "never tested", which
            # is a different fact from "tested and scored 0" and must not be collapsed into it.
            "svg_test": m.get("has_svg_test"),
            "test_status": m.get("last_test_status"),
            "test_at": m.get("last_test_at"),
            "failed_count": int(m.get("failed_count") or 0) or None,
            "rating": _num(m.get("avg_rating")),
            "verified": bool(m.get("provider_verified")),
            "promo_off": _num(promo.get("percent_off")),
            "promo_in": _num(promo.get("promo_input_price") or promo.get("price_per_input_token")),
            "promo_out": _num(promo.get("promo_output_price") or promo.get("price_per_output_token")),
        })
    out = {"at": time.time(), "count": len(rows), "models": rows, "source": MARKET + "/models"}
    if path:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name("catalog.json.tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass
    return out


def keys(get) -> list[dict[str, Any]]:
    """Every key on the account, with the totals the list view already carries."""
    data = get(BASE + '/llm/keys') or []
    out = []
    for k in data if isinstance(data, list) else []:
        if not isinstance(k, dict):
            continue
        out.append({
            "id": str(k.get("id") or ""), "name": k.get("name"), "prefix": k.get("key_prefix"),
            "active": bool(k.get("active")), "favorite": bool(k.get("favorite")),
            "rpm": k.get("rate_limit_rpm"), "requests": k.get("total_requests"),
            "spent": _num(k.get("total_spent")), "created_at": k.get("created_at"),
            "expires_at": k.get("expires_at"), "revoked_at": k.get("revoked_at"),
            "models": len(k.get("subscriptions") or []),
            "content_filtered_last_at": k.get("content_filtered_last_at"),
        })
    return out


def key_detail(get, key_id: str) -> dict[str, Any]:
    """One key's real budget reset times.

    The list view answers these with ``0001-01-01``; a reset countdown -- the whole point of a quota
    widget -- only exists on the detail route.
    """
    data = get("%s/llm/keys/%s" % (BASE, key_id)) or {}
    if not isinstance(data, dict):
        return {}
    return {
        "id": data.get("id") or key_id, "name": data.get("name"), "prefix": data.get("key_prefix"),
        "active": bool(data.get("active")), "rpm": data.get("rate_limit_rpm"),
        "requests": data.get("total_requests"), "spent": _num(data.get("total_spent")),
        "daily_budget": _num(data.get("daily_budget_limit")),
        "monthly_budget": _num(data.get("monthly_budget_limit")),
        "daily_period_start": data.get("daily_budget_period_start"),
        "daily_next_reset": data.get("daily_budget_next_reset"),
        "monthly_period_start": data.get("monthly_budget_period_start"),
        "monthly_next_reset": data.get("monthly_budget_next_reset"),
        "created_at": data.get("created_at"), "expires_at": data.get("expires_at"),
        "revoked_at": data.get("revoked_at"),
    }


def key_models(get, key_id: str) -> list[dict[str, Any]]:
    """The key's own price book: what it may call, what each model costs it, what it has already spent."""
    data = get("%s/llm/keys/%s/subscriptions" % (BASE, key_id)) or []
    out = []
    for s in data if isinstance(data, list) else []:
        if not isinstance(s, dict):
            continue
        out.append({
            "model": s.get("model_id") or s.get("actual_model"), "provider": s.get("provider_prefix"),
            "in": _num(s.get("price_per_input_token")), "out": _num(s.get("price_per_output_token")),
            "cache_read": _num(s.get("price_per_cached_input_token")),
            "floor": _num(s.get("min_price_per_request")),
            "promo_off": _num(s.get("promo_percent_off")),
            "requests": s.get("total_requests"), "spent": _num(s.get("total_spent")),
            "tokens": s.get("total_tokens"), "active": s.get("model_active"),
            "health": s.get("provider_health_status"), "score": _num(s.get("model_test_score")),
        })
    return out


def transactions(get, pages: int = 2) -> list[dict[str, Any]]:
    """Top-up history -- the audit trail behind the ledger invariant the whole page trusts."""
    out: list[dict[str, Any]] = []
    for page in range(1, max(1, int(pages)) + 1):
        data = get(BASE + "/wallet/transactions", {"page": page, "page_size": 50}) or []
        if not isinstance(data, list) or not data:
            break
        for t in data:
            if isinstance(t, dict):
                out.append({
                    "id": t.get("id"), "status": t.get("status"),
                    "expected": _num(t.get("amount_expected")), "paid": _num(t.get("amount_paid")),
                    "currency": t.get("currency"), "method": t.get("payment_provider"),
                    "created_at": t.get("created_at"), "paid_at": t.get("paid_at"),
                    "expires_at": t.get("expires_at"),
                })
    return out


def me(get) -> dict[str, Any]:
    data = get(BASE + '/account/me') or {}
    if not isinstance(data, dict):
        return {}
    return {k: data.get(k) for k in ("username", "email", "role", "active", "created_at",
                                     "last_activity_at")}
