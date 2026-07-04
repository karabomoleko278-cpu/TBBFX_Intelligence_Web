"""
Massive (Polygon-compatible) REST client — active only when MASSIVE_API_KEY is set.

We wire ONLY the surface this account is entitled to (verified against the live
key): FX / metal **aggregates** (``C:EURUSD``, ``C:XAUUSD`` … which carry real
tick volume) and **treasury yields**. Options chains, real-time FX quotes and
futures are NOT entitled on the current plan, so those panels keep their existing
free sources. Every call fails soft (returns ``None``) so the terminal degrades
gracefully to the yfinance fallback whenever Massive is unset or hiccups.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests

BASE = os.environ.get("MASSIVE_API_BASE_URL", "https://api.massive.com")


def api_key() -> str:
    return os.environ.get("MASSIVE_API_KEY") or os.environ.get("POLYGON_API_KEY") or ""


def is_configured() -> bool:
    return bool(api_key())


# Platform instrument -> entitled Massive FX/metal ticker. US30 / USTEC are index
# CFDs (not FX, and not entitled here), so they are absent -> callers fall back.
_FX_TICKER = {
    "EURUSD": "C:EURUSD",
    "GBPUSD": "C:GBPUSD",
    "USDJPY": "C:USDJPY",
    "XAUUSD": "C:XAUUSD",
}


def fx_ticker(symbol: str) -> Optional[str]:
    return _FX_TICKER.get(symbol.upper())


def _get(path: str, params: Optional[Dict[str, Any]] = None, timeout: float = 20.0) -> Dict[str, Any]:
    q = dict(params or {})
    q["apiKey"] = api_key()
    r = requests.get(f"{BASE}{path}", params=q, timeout=timeout,
                     headers={"User-Agent": "TBBFX-FeatureFactory/1.0"})
    r.raise_for_status()
    return r.json()


def fx_minute_bars(symbol: str, days: int = 5) -> Optional[List[Dict[str, float]]]:
    """Recent 15-minute FX/metal bars (close + real volume) for a platform symbol.

    15-minute granularity keeps the response small (~hundreds of bars, ~1s) — a
    1-minute pull over the same window is ~11k bars / ~0.8MB and reliably blows
    the request timeout, which silently dropped us to the yfinance fallback.
    15-minute is plenty of resolution for a volume-by-price profile.

    Returns ``None`` when the symbol isn't an entitled FX/metal ticker, the key
    is unset, or the call fails — the caller then uses the yfinance fallback.
    """
    tk = fx_ticker(symbol)
    if not tk or not is_configured():
        return None
    to = datetime.utcnow().date()
    frm = to - timedelta(days=days + 3)  # pad for weekends/holidays
    try:
        d = _get(
            f"/v2/aggs/ticker/{tk}/range/15/minute/{frm:%Y-%m-%d}/{to:%Y-%m-%d}",
            {"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        rows = d.get("results") or []
        out = [
            {"c": float(b.get("c", 0)), "v": float(b.get("v", 0) or 0)}
            for b in rows if b.get("c")
        ]
        return out or None
    except Exception:
        return None


def latest_treasury_10y() -> Optional[float]:
    """Most-recent US 10-year treasury yield (percent), or None if unavailable."""
    if not is_configured():
        return None
    try:
        d = _get("/fed/v1/treasury-yields", {"limit": 250})
        rows = d.get("results") or []
        if not rows:
            return None
        latest = max(rows, key=lambda x: x.get("date", ""))
        for field in ("yield_10_year", "yield_10y", "10_year", "y_10_year"):
            if latest.get(field) is not None:
                return float(latest[field])
        return None
    except Exception:
        return None
