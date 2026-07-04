"""
Tradier options client — active only when TRADIER_TOKEN is set.

Provides a US-listed options chain (strike, type, open interest, implied vol) for
the GLD/QQQ/DIA proxies, replacing the fragile / rate-limited / slow yfinance
scrape behind GEX + DEX/VEX/CHEX. We pull OI + IV here and let the existing
Black-Scholes engine compute every greek, so the maths stays identical to the
current GEX path — only the *data source* changes.

Free access: a Tradier developer/sandbox token (https://developer.tradier.com)
returns delayed chains with greeks (ORATS) — which is fine for dealer-positioning
analytics. Set TRADIER_TOKEN (and optionally TRADIER_API_BASE for production).

Fails soft: any problem returns None so OptionsExposureEngine falls back to
yfinance and nothing breaks.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

# Sandbox = delayed data on a free dev token. Production (api.tradier.com) needs
# a market-data entitlement. Override via TRADIER_API_BASE.
BASE = os.environ.get("TRADIER_API_BASE", "https://sandbox.tradier.com")


def token() -> str:
    return os.environ.get("TRADIER_TOKEN", "")


def is_configured() -> bool:
    return bool(token())


def _get(path: str, params: Optional[Dict[str, Any]] = None, timeout: float = 12.0) -> Dict[str, Any]:
    r = requests.get(
        f"{BASE}{path}",
        params=params or {},
        timeout=timeout,
        headers={"Authorization": f"Bearer {token()}", "Accept": "application/json"},
    )
    r.raise_for_status()
    return r.json()


def _spot(underlying: str) -> Optional[float]:
    try:
        d = _get("/v1/markets/quotes", {"symbols": underlying})
        q = ((d.get("quotes") or {}).get("quote"))
        if isinstance(q, list):
            q = q[0] if q else None
        if q:
            px = q.get("last") or q.get("close") or q.get("prevclose")
            return float(px) if px else None
    except Exception:
        return None
    return None


def _nearest_expiration(underlying: str) -> Optional[str]:
    try:
        d = _get("/v1/markets/options/expirations", {"symbol": underlying, "includeAllRoots": "true"})
        exps = ((d.get("expirations") or {}).get("date"))
        if isinstance(exps, str):
            exps = [exps]
        if not exps:
            return None
        today = datetime.utcnow().date()
        future = [e for e in exps if datetime.strptime(e, "%Y-%m-%d").date() >= today]
        return future[0] if future else exps[0]
    except Exception:
        return None


def load_chain(underlying: str) -> Optional[Dict[str, Any]]:
    """Return the same shape as OptionsExposureEngine.load_chain, or None to
    signal "fall back to yfinance"."""
    if not is_configured():
        return None
    try:
        spot = _spot(underlying)
        expiry = _nearest_expiration(underlying)
        if not spot or not expiry:
            return None

        d = _get("/v1/markets/options/chains",
                 {"symbol": underlying, "expiration": expiry, "greeks": "true"})
        opts = ((d.get("options") or {}).get("option")) or []
        if isinstance(opts, dict):
            opts = [opts]

        dte = max(1.0, float(
            (datetime.strptime(expiry, "%Y-%m-%d").date() - datetime.utcnow().date()).days))

        rows: List[Dict[str, Any]] = []
        oi_used = False
        for o in opts:
            otype = (o.get("option_type") or "").lower()
            if otype not in ("call", "put"):
                continue
            strike = float(o.get("strike") or 0)
            if strike <= 0:
                continue
            oi = float(o.get("open_interest") or 0)
            vol = float(o.get("volume") or 0)
            greeks = o.get("greeks") or {}
            iv = greeks.get("mid_iv") or greeks.get("smv_vol") or 0
            iv = float(iv) if iv else 0.0
            if iv <= 0:
                iv = 0.25  # neutral fallback so a missing IV never breaks the fit
            weight = oi if oi > 0 else vol
            if oi > 0:
                oi_used = True
            rows.append({
                "strike": strike,
                "type": otype,
                "open_interest": weight,
                "implied_volatility": iv,
                "dte": dte,
            })

        if len(rows) < 4:  # too thin to be trustworthy — let yfinance try
            return None

        return {
            "spot": spot,
            "dte": dte,
            "expiry": expiry,
            "chains": rows,
            "proxy": underlying,
            "weight_source": "open_interest" if oi_used else "volume",
            "source": "tradier",
        }
    except Exception:
        return None
