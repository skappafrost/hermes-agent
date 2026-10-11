"""USD -> VND spot rate, fetched from a keyless public feed and cached on disk.

Hermes prices models in dollars and the vilao ledger is denominated in dong, so every dollar figure
this surface shows has to be converted somewhere to be readable in dong. ``open.er-api.com`` (the free
ExchangeRate-API endpoint, no key, no account) republishes once a day, so the cache is refreshed on a
6-hour TTL: four reads a day at most, and the rate is never more than a few hours behind.

The cache file lives under ``plugin-data/fx/`` -- outside either plugin's own directory and shared by
both, because provider-usage and the Usage page convert the same money and must not disagree about the
rate they printed. A read-only caller never writes.

When the fetch fails the previous value keeps being served; a stale rate on a cosmetic conversion
beats falling back to dollars on a page whose whole point is dong. With no cache at all the hardcoded
:data:`BOOTSTRAP_USD_VND` is used and ``source`` says so, so the UI can be honest about which number it
printed. ``VILAO_USD_VND`` (and optionally ``VILAO_CNY_VND``) pins the rate outright, feed skipped.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

TTL_S = 6 * 3600.0
ENDPOINT = "https://open.er-api.com/v6/latest/USD"
# Fallbacks for a machine that cannot reach the endpoint at all. The dong figures here are the user's
# own hard-set numbers (2026-10-08) -- pinned on purpose, because a dong display that quietly reverts to
# dollars whenever one host is down is worse than a rate a percent off. Setting ``VILAO_USD_VND`` pins
# the rate through the environment instead, without touching this file.
BOOTSTRAP_USD_VND = 26200.0
BOOTSTRAP_CNY_VND = 3900.0


def cache_path() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "plugin-data" / "fx" / "usd-vnd.json"
    except Exception:
        return None


def _stored(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("usd_vnd") else None


def _fetch() -> Optional[dict[str, Any]]:
    import httpx

    resp = httpx.get(ENDPOINT, timeout=10.0)
    resp.raise_for_status()
    body = resp.json() or {}
    rates = body.get("rates")
    if not isinstance(rates, dict):
        return None
    usd = rates.get("VND")
    cny = rates.get("CNY")
    if not usd:
        return None
    return {
        "usd_vnd": float(usd),
        "cny_vnd": round(float(usd) / float(cny), 2) if cny else None,
        "at": time.time(),
        "source": "open.er-api.com",
        "quote_time": body.get("time_last_update_utc"),
    }


def _pinned() -> Optional[dict[str, Any]]:
    """A hand-set rate wins over any feed: ``VILAO_USD_VND``, optionally ``VILAO_CNY_VND``."""
    try:
        usd = float(os.environ.get("VILAO_USD_VND") or 0)
    except ValueError:
        return None
    if usd <= 0:
        return None
    try:
        cny = float(os.environ.get("VILAO_CNY_VND") or 0) or None
    except ValueError:
        cny = None
    return {"usd_vnd": usd, "cny_vnd": cny, "at": time.time(), "source": "VILAO_USD_VND", "pinned": True}


def rates(force: bool = False) -> Optional[dict[str, Any]]:
    """The rate table, refreshed when the cache is older than :data:`TTL_S`.

    Returns ``None`` only when there is nothing to say: no cache, no fetch, no bootstrap.
    """
    pinned = _pinned()
    if pinned:
        return pinned
    path = cache_path()
    cached = _stored(path) if path else None
    if cached and not force and time.time() - float(cached.get("at") or 0) < TTL_S:
        return cached
    fresh = None
    try:
        fresh = _fetch()
    except Exception:
        fresh = None
    if fresh:
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(path.suffix + ".%d.tmp" % os.getpid())
                tmp.write_text(json.dumps(fresh), encoding="utf-8")
                os.replace(tmp, path)
            except OSError:
                pass
        return fresh
    if cached:
        out = dict(cached)
        out["stale"] = True
        return out
    now = time.time()
    return {
        "usd_vnd": BOOTSTRAP_USD_VND,
        "cny_vnd": BOOTSTRAP_CNY_VND,
        "at": now,
        "source": "hardcode 2026-10-08",
        "stale": True,
    }


def to_vnd(amount: Any, cur: Any, table: Optional[dict[str, Any]] = None) -> Optional[float]:
    """Convert an amount to dong. ``None`` when the currency is unknown or there is no rate.

    Dong amounts pass through: the ledger is already the unit every figure on these surfaces is
    compared against, and rounding a value that was billed in dong is the display's business only.
    """
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    code = str(cur or "USD").upper()
    if code == "VND":
        return value
    table = table if table is not None else rates()
    if not table:
        return None
    rate = table.get("usd_vnd") if code == "USD" else table.get("cny_vnd") if code == "CNY" else None
    return value * float(rate) if rate else None
