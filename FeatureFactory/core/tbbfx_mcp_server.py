"""Local read-only MCP server for TBBFX market intelligence.

The server speaks newline-delimited JSON-RPC 2.0 over stdio so local agents can
discover and call TBBFX data tools without stuffing large telemetry arrays into
their system prompt. All tools are read-only by design: they expose cached GEX,
order-flow, and macro context, but they never mutate execution parameters.

Run from the FeatureFactory directory:

    python -m core.tbbfx_mcp_server
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.config import settings
from core.macro_intelligence import build_macro_geopolitical_intelligence
from core.openbb_quant import calculate_quant_feature_pack
from core.portfolio_risk_engine import PortfolioRiskEngine
from core.state_db import get_state_db
from core.tbbfx_object import is_tbbfx_object, make_tbbfx_object, unwrap_tbbfx_results


SERVER_NAME = "tbbfx-mcp-server"
SERVER_VERSION = "1.0.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
DEFAULT_FEATURE_FACTORY_URL = os.getenv("TBBFX_FEATURE_FACTORY_URL", "http://127.0.0.1:8000")
FALLBACK_FEATURE_FACTORY_URLS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:5000",
    "http://localhost:5000",
]

IMMUTABLE_SYMBOL_RISK_TIERS: Dict[str, float] = {
    "EURUSD": 0.15,
    "GBPUSD": 0.12,
    "XAUUSD": 0.15,
    "US30": 0.12,
    "USTEC": 0.12,
    "USDJPY": 0.15,
}

STRATEGY_BOUNDARIES: Dict[str, Any] = {
    "read_only": True,
    "mutation_allowed": False,
    "target_r": 4.0,
    "minimum_frequency": "at least 1 qualified trade per day when market quality permits",
    "stop_policy": "H1 unmitigated FVG outer edge or 78.6% OTE boundary plus 2.0 pips breathing room",
    "immutable_symbol_risk_tiers": IMMUTABLE_SYMBOL_RISK_TIERS,
}

MCP_SERVER_DESCRIPTOR: Dict[str, Any] = {
    "id": SERVER_NAME,
    "name": "TBBFX Local Market Intelligence MCP Server",
    "transport": "stdio",
    "command": "python",
    "args": ["-m", "core.tbbfx_mcp_server"],
    "read_only": True,
    "description": (
        "Local JSON-RPC tool provider for historical GEX, live order-flow, "
        "and macro calendar context. It cannot modify risk tiers or trade settings."
    ),
}


def _clean_symbol(symbol: str) -> str:
    raw = str(symbol or "").upper().strip()
    if raw.endswith("M") and raw[:-1] in IMMUTABLE_SYMBOL_RISK_TIERS:
        raw = raw[:-1]
    return raw


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    try:
        if hasattr(value, "item"):
            return value.item()
    except Exception:
        pass
    return str(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _utc_iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except Exception:
        return None


def _feature_factory_candidates() -> List[str]:
    """Return local FeatureFactory API candidates, with env override first."""
    seen = set()
    candidates: List[str] = []
    for raw in [DEFAULT_FEATURE_FACTORY_URL, *FALLBACK_FEATURE_FACTORY_URLS]:
        base = str(raw or "").strip().rstrip("/")
        if not base or base in seen:
            continue
        seen.add(base)
        candidates.append(base)
    return candidates


_working_feature_factory_url: Optional[str] = None


def _http_json(path: str, timeout: float = 1.25) -> Tuple[str, Dict[str, Any]]:
    global _working_feature_factory_url
    errors: List[str] = []
    
    candidates = _feature_factory_candidates()
    if _working_feature_factory_url and _working_feature_factory_url in candidates:
        # Move the last known working URL to the front of the candidate list
        candidates.remove(_working_feature_factory_url)
        candidates.insert(0, _working_feature_factory_url)

    for base in candidates:
        url = f"{base}/{path.lstrip('/')}"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "tbbfx-mcp-server/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - local operator endpoint
                payload = response.read().decode("utf-8")
            _working_feature_factory_url = base
            parsed = json.loads(payload) if payload else {}
            return base, unwrap_tbbfx_results(parsed)
        except Exception as exc:  # noqa: BLE001 - try the next local candidate
            errors.append(f"{base}: {type(exc).__name__}({exc})")
    raise RuntimeError("FeatureFactory endpoints unavailable. Tried " + "; ".join(errors))


def _svi_smile_coordinates(row: Optional[Dict[str, Any]]) -> List[Dict[str, float]]:
    if not row:
        return []

    a = _safe_float(row.get("a"))
    b = _safe_float(row.get("b"))
    rho = _safe_float(row.get("rho"))
    m = _safe_float(row.get("m"))
    sigma = max(_safe_float(row.get("sigma"), 0.1), 1e-6)
    forward = max(_safe_float(row.get("forward"), 0.0), 1e-9)
    dte_years = max(_safe_float(row.get("dte"), 30.0) / 365.0, 1e-6)

    coords: List[Dict[str, float]] = []
    for step in range(-10, 11):
        log_moneyness = step * 0.025
        total_variance = a + b * (rho * (log_moneyness - m) + math.sqrt((log_moneyness - m) ** 2 + sigma**2))
        implied_vol = math.sqrt(max(total_variance, 0.0) / dte_years)
        coords.append(
            {
                "log_moneyness": round(log_moneyness, 4),
                "strike": round(forward * math.exp(log_moneyness), 5),
                "implied_volatility": round(implied_vol, 6),
            }
        )
    return coords


def fetch_historical_gex_matrix(symbol: str, lookback_hours: int = 24) -> Dict[str, Any]:
    """Return historical GEX, gamma-flip migration, and cached SVI data."""
    sym = _clean_symbol(symbol)
    hours = max(1, min(int(lookback_hours or 24), 24 * 30))
    # Use a generous limit so sparse persisted stores still return useful history.
    limit = max(24, min(hours * 12, 2500))
    cutoff = time.time() - (hours * 3600)

    db = get_state_db()
    raw_history = db.get_gex_history(sym, limit=limit)
    history = [row for row in raw_history if _safe_float(row.get("ts")) >= cutoff]
    if not history:
        history = raw_history[: min(len(raw_history), 50)]

    svi_latest = db.get_latest_svi_parameters(sym)
    svi_history = db.get_svi_history(sym, limit=min(100, limit))
    latest = history[0] if history else None

    return {
        "tool": "fetch_historical_gex_matrix",
        "symbol": sym,
        "lookback_hours": hours,
        "source": "sqlite_state_db",
        "database_path": settings.DB_PATH,
        "snapshot_count": len(history),
        "latest": latest,
        "net_gex_array": [
            {
                "ts": row.get("ts"),
                "utc": _utc_iso(row.get("ts")),
                "net_gex": _safe_float(row.get("net_gex")),
            }
            for row in reversed(history)
        ],
        "gamma_flip_migration": [
            {
                "ts": row.get("ts"),
                "utc": _utc_iso(row.get("ts")),
                "spot": _safe_float(row.get("spot")),
                "gamma_flip": _safe_float(row.get("gamma_flip")),
                "regime": row.get("regime"),
            }
            for row in reversed(history)
        ],
        "svi_parameters": svi_latest,
        "svi_history_count": len(svi_history),
        "svi_volatility_smile": _svi_smile_coordinates(svi_latest),
        "strategy_boundaries": STRATEGY_BOUNDARIES,
    }


def fetch_live_orderflow_telemetry(symbol: str) -> Dict[str, Any]:
    """Return current CVD, OBI, microprice, and related live telemetry."""
    sym = _clean_symbol(symbol)
    payload: Dict[str, Any] = {
        "tool": "fetch_live_orderflow_telemetry",
        "symbol": sym,
        "source": "feature_factory_http",
        "feature_factory_url": _feature_factory_candidates()[0],
        "attempted_feature_factory_urls": _feature_factory_candidates(),
        "read_only": True,
        "strategy_boundaries": STRATEGY_BOUNDARIES,
    }

    try:
        feature_url, features = _http_json(f"/api/features/{urllib.parse.quote(sym)}")
        payload["feature_factory_url"] = feature_url
        payload["telemetry"] = {
            "cvd": _safe_float(features.get("cvd")),
            "cvd_trajectory": [
                {
                    "ts": time.time(),
                    "utc": _utc_iso(time.time()),
                    "cvd": _safe_float(features.get("cvd")),
                }
            ],
            "obi": _safe_float(features.get("obi")),
            "microprice": _safe_float(features.get("microprice")),
            "spread_weighted_microprice": _safe_float(features.get("microprice")),
            "footprint_count": int(features.get("footprint_count") or 0),
            "raw": features,
        }
        payload["status"] = "online"
    except Exception as exc:  # noqa: BLE001 - degraded telemetry is still useful to the agent
        payload["status"] = "unavailable"
        payload["telemetry_error"] = f"{type(exc).__name__}: {exc}"
        payload["telemetry"] = {
            "cvd": 0.0,
            "cvd_trajectory": [],
            "obi": 0.0,
            "microprice": 0.0,
            "spread_weighted_microprice": 0.0,
            "footprint_count": 0,
        }

    try:
        momentum_url, momentum = _http_json(f"/api/momentum/{urllib.parse.quote(sym)}")
        payload["momentum"] = momentum
        payload["momentum_url"] = momentum_url
    except Exception as exc:  # noqa: BLE001
        payload["momentum_error"] = f"{type(exc).__name__}: {exc}"
        payload["momentum"] = {}

    try:
        exposure_url, exposure = _http_json(f"/api/exposure/{urllib.parse.quote(sym)}")
        payload["exposure_url"] = exposure_url
        payload["gamma_context"] = {
            "net_gex": _safe_float(exposure.get("net_gex")),
            "gamma_flip": _safe_float(exposure.get("gamma_flip")),
            "regime": exposure.get("regime"),
            "underlying_price": _safe_float(exposure.get("underlying_price")),
            "dex": _safe_float(exposure.get("dex")),
            "vex": _safe_float(exposure.get("vex")),
            "chex": _safe_float(exposure.get("chex")),
            "source": exposure.get("source") or exposure.get("weight_source"),
        }
    except Exception as exc:  # noqa: BLE001
        payload["gamma_context_error"] = f"{type(exc).__name__}: {exc}"
        payload["gamma_context"] = {}

    return payload


def _static_macro_watchlist(symbol: str) -> List[Dict[str, Any]]:
    sym = _clean_symbol(symbol)
    shared = [
        {"event": "FOMC Rate Decision", "impact": "high", "region": "USD", "why": "Rates reprice USD, indices, gold, and risk assets."},
        {"event": "US Non-Farm Payrolls", "impact": "high", "region": "USD", "why": "Employment shocks often expand spread and momentum regimes."},
        {"event": "US CPI Inflation", "impact": "high", "region": "USD", "why": "Inflation shocks can move yields, dollar, gold, and equity indices."},
    ]
    symbol_specific = {
        "EURUSD": [{"event": "ECB Rate Decision", "impact": "high", "region": "EUR"}],
        "GBPUSD": [{"event": "BoE Rate Decision", "impact": "high", "region": "GBP"}],
        "USDJPY": [{"event": "BoJ Rate Decision", "impact": "high", "region": "JPY"}],
        "XAUUSD": [{"event": "US 10Y Treasury Yield Shock", "impact": "high", "region": "USD"}],
        "US30": [{"event": "US Equity Earnings / Fed Speakers", "impact": "medium-high", "region": "USD"}],
        "USTEC": [{"event": "Mega-cap Earnings / Fed Speakers", "impact": "medium-high", "region": "USD"}],
    }
    events = symbol_specific.get(sym, []) + shared
    return [
        {
            **event,
            "symbol": sym,
            "source": "static_macro_watchlist",
            "next_check_window": "monitor broker/news calendar before London and New York sessions",
        }
        for event in events
    ]


def _yahoo_macro_news(symbol: str, limit: int = 5) -> List[Dict[str, Any]]:
    query = f"{symbol} FOMC NFP CPI interest rate macro"
    url = "https://query1.finance.yahoo.com/v1/finance/search?" + urllib.parse.urlencode(
        {"q": query, "newsCount": limit, "quotesCount": 0}
    )
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 tbbfx-mcp-server/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=2.0) as response:  # noqa: S310 - public market news endpoint
        data = json.loads(response.read().decode("utf-8"))

    items: List[Dict[str, Any]] = []
    for item in data.get("news", [])[:limit]:
        publish_time = item.get("providerPublishTime")
        items.append(
            {
                "title": item.get("title"),
                "publisher": item.get("publisher"),
                "url": item.get("link"),
                "provider_publish_time": publish_time,
                "provider_publish_utc": _utc_iso(publish_time),
                "source": "yahoo_finance_search_proxy",
                "impact": "context",
            }
        )
    return items


def fetch_macroeconomic_calendar(symbol: str) -> Dict[str, Any]:
    """Return high-impact macro context and local macro proxy readings."""
    sym = _clean_symbol(symbol)
    payload: Dict[str, Any] = {
        "tool": "fetch_macroeconomic_calendar",
        "symbol": sym,
        "source": "macro_proxy_with_yahoo_news_fallback",
        "read_only": True,
        "strategy_boundaries": STRATEGY_BOUNDARIES,
        "events": _static_macro_watchlist(sym),
    }

    try:
        macro_url, macro_proxy = _http_json("/api/macro")
        payload["macro_proxy"] = macro_proxy
        payload["macro_proxy_url"] = macro_url
    except Exception as exc:  # noqa: BLE001
        payload["macro_proxy_error"] = f"{type(exc).__name__}: {exc}"
        payload["macro_proxy"] = {}

    try:
        payload["yahoo_macro_news"] = _yahoo_macro_news(sym)
    except Exception as exc:  # noqa: BLE001
        payload["yahoo_macro_news_error"] = f"{type(exc).__name__}: {exc}"
        payload["yahoo_macro_news"] = []

    return payload


def fetch_macro_geopolitical_intelligence(symbol: str, limit: int = 40) -> Dict[str, Any]:
    """Return read-only geographic macro/geopolitical intelligence for the Macro Map."""
    sym = _clean_symbol(symbol)
    safe_limit = max(1, min(int(limit or 40), 250))
    payload = build_macro_geopolitical_intelligence(symbol=sym, limit=safe_limit)
    payload["tool"] = "fetch_macro_geopolitical_intelligence"
    payload["strategy_boundaries"] = STRATEGY_BOUNDARIES
    payload["read_only"] = True
    return make_tbbfx_object(
        payload,
        provider="macro_intelligence_router",
        route="mcp.fetch_macro_geopolitical_intelligence",
        warnings=list(payload.get("warnings") or []),
    ).to_dict()


def fetch_quantitative_feature_pack(symbol: str, timeframe: str = "M5", count: int = 240) -> Dict[str, Any]:
    """Return OpenBB-style quantitative features for recent candles."""
    sym = _clean_symbol(symbol)
    tf = str(timeframe or "M5").upper()
    limit = max(32, min(int(count or 240), 1200))
    candles: List[Dict[str, Any]] = []
    warnings: List[str] = []

    for path in (
        f"/api/candles/{urllib.parse.quote(sym)}/{urllib.parse.quote(tf)}?count={limit}",
        f"/api/market/candles/{urllib.parse.quote(sym)}/{urllib.parse.quote(tf)}?count={limit}",
    ):
        try:
            base, payload = _http_json(path)
            candles = list((payload or {}).get("candles") or [])
            if candles:
                pack = calculate_quant_feature_pack(
                    candles,
                    symbol=sym,
                    route="mcp.fetch_quantitative_feature_pack",
                ).to_dict()
                pack["warnings"].extend(warnings)
                pack["extra"]["feature_factory_url"] = base
                pack["extra"]["timeframe"] = tf
                pack["extra"]["candle_count"] = len(candles)
                pack["extra"]["strategy_boundaries"] = STRATEGY_BOUNDARIES
                return pack
        except Exception as exc:  # noqa: BLE001 - try the next candle endpoint
            warnings.append(f"{path}: {type(exc).__name__}: {exc}")

    return make_tbbfx_object(
        {
            "tool": "fetch_quantitative_feature_pack",
            "symbol": sym,
            "timeframe": tf,
            "sample_count": 0,
            "features": {},
            "strategy_boundaries": STRATEGY_BOUNDARIES,
        },
        provider="unavailable",
        route="mcp.fetch_quantitative_feature_pack",
        warnings=warnings or ["No candle endpoint returned data."],
    ).to_dict()


def fetch_portfolio_var_matrix(symbol: Optional[str] = None, account_balance_zar: Optional[float] = None) -> Dict[str, Any]:
    """Return read-only parametric VaR boundaries for one symbol or the full watchlist."""
    symbols = [_clean_symbol(symbol)] if symbol else list(settings.WATCHLIST)
    engine = PortfolioRiskEngine()
    matrix = engine.calculate_matrix(symbols=symbols, account_balance_zar=account_balance_zar)
    matrix["tool"] = "fetch_portfolio_var_matrix"
    matrix["strategy_boundaries"] = STRATEGY_BOUNDARIES
    matrix["read_only"] = True
    return make_tbbfx_object(
        matrix,
        provider="sqlite_state_db",
        route="mcp.fetch_portfolio_var_matrix",
        warnings=[
            warning
            for row in matrix.get("symbols", [])
            for warning in list(row.get("warnings") or [])
        ],
    ).to_dict()


def verify_governance_audit_integrity(limit: int = 1000) -> Dict[str, Any]:
    """Verify that the immutable governance audit ledger has not been tampered with."""
    safe_limit = max(1, min(int(limit or 1000), 10000))
    verification = get_state_db().verify_governance_audit_integrity(limit=safe_limit)
    verification["tool"] = "verify_governance_audit_integrity"
    verification["strategy_boundaries"] = STRATEGY_BOUNDARIES
    verification["read_only"] = True
    return make_tbbfx_object(
        verification,
        provider="sqlite_state_db",
        route="mcp.verify_governance_audit_integrity",
        warnings=[] if verification.get("status") in ("valid", "empty") else ["Governance audit hash mismatch detected."],
    ).to_dict()


def get_tool_definitions() -> List[Dict[str, Any]]:
    return [
        {
            "name": "fetch_historical_gex_matrix",
            "description": (
                "Read-only SQLite query for historical GEX arrays, gamma-flip migration, "
                "and cached SVI volatility smile coordinates."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Instrument symbol, e.g. XAUUSD or EURUSD."},
                    "lookback_hours": {
                        "type": "integer",
                        "description": "Historical lookback window in hours.",
                        "default": 24,
                        "minimum": 1,
                        "maximum": 720,
                    },
                },
                "required": ["symbol"],
            },
        },
        {
            "name": "fetch_live_orderflow_telemetry",
            "description": (
                "Read-only live CVD, OBI, microprice, momentum, and gamma context "
                "from the running FeatureFactory/Bytewax cache."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Instrument symbol, e.g. GBPUSD or US30."},
                },
                "required": ["symbol"],
            },
        },
        {
            "name": "fetch_macroeconomic_calendar",
            "description": (
                "Read-only macro session context using local macro proxy data plus Yahoo Finance news search fallback."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Instrument symbol needing macro context."},
                },
                "required": ["symbol"],
            },
        },
        {
            "name": "fetch_macro_geopolitical_intelligence",
            "description": (
                "Read-only geographic macro/geopolitical intelligence packet for the Macro Map, "
                "including hotspots, symbol impact, event feed, and map telemetry points."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Watchlist instrument symbol, e.g. XAUUSD or US30."},
                    "limit": {
                        "type": "integer",
                        "description": "Maximum calendar/feed records to return.",
                        "default": 40,
                        "minimum": 1,
                        "maximum": 250,
                    },
                },
                "required": ["symbol"],
            },
        },
        {
            "name": "fetch_quantitative_feature_pack",
            "description": (
                "Read-only OpenBB-style quantitative/technical feature pack "
                "for recent candles: rolling Sharpe/Sortino, variance, skew, "
                "kurtosis, and Hurst proxy."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Instrument symbol, e.g. XAUUSD."},
                    "timeframe": {
                        "type": "string",
                        "description": "Timeframe such as M5, M15, H1, H4, D1, or W1.",
                        "default": "M5",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of candles to analyze.",
                        "default": 240,
                        "minimum": 32,
                        "maximum": 1200,
                    },
                },
                "required": ["symbol"],
            },
        },
        {
            "name": "fetch_portfolio_var_matrix",
            "description": (
                "Read-only parametric portfolio VaR matrix with rolling standard deviation, "
                "95% VaR, and 99% VaR boundaries in ZAR. Does not alter risk tiers."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Optional instrument symbol. Omit to return the full watchlist matrix.",
                    },
                    "account_balance_zar": {
                        "type": "number",
                        "description": "Optional account balance override for analysis only.",
                    },
                },
            },
        },
        {
            "name": "verify_governance_audit_integrity",
            "description": (
                "Read-only SHA-256 audit-ledger integrity verification for governance decisions."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Maximum recent audit rows to verify.",
                        "default": 1000,
                        "minimum": 1,
                        "maximum": 10000,
                    },
                },
            },
        },
    ]


_TOOL_HANDLERS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "fetch_historical_gex_matrix": fetch_historical_gex_matrix,
    "fetch_live_orderflow_telemetry": fetch_live_orderflow_telemetry,
    "fetch_macroeconomic_calendar": fetch_macroeconomic_calendar,
    "fetch_macro_geopolitical_intelligence": fetch_macro_geopolitical_intelligence,
    "fetch_quantitative_feature_pack": fetch_quantitative_feature_pack,
    "fetch_portfolio_var_matrix": fetch_portfolio_var_matrix,
    "verify_governance_audit_integrity": verify_governance_audit_integrity,
}


def execute_tool(name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Execute an MCP tool directly for in-process agent integrations."""
    if name not in _TOOL_HANDLERS:
        raise ValueError(f"Unknown MCP tool: {name}")
    args = arguments or {}
    payload = _TOOL_HANDLERS[name](**args)
    if is_tbbfx_object(payload):
        return payload
    source = str(payload.get("source") or payload.get("tool") or "tbbfx_mcp_server")
    return make_tbbfx_object(
        payload,
        provider=source,
        route=f"mcp.{name}",
        warnings=list(payload.get("warnings") or []),
    ).to_dict()


def _mcp_tool_result(payload: Dict[str, Any], is_error: bool = False) -> Dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=True, default=_json_default),
            }
        ],
        "isError": is_error,
    }


def _jsonrpc_success(request_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    error: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _handle_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params") or {}

    if not method:
        return _jsonrpc_error(request_id, -32600, "Invalid Request: missing method")

    if method.startswith("notifications/"):
        return None

    if method == "initialize":
        return _jsonrpc_success(
            request_id,
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Use these read-only TBBFX tools to fetch market intelligence on demand. "
                    "Do not request mutation of risk tiers, Target R, stops, or trade execution state."
                ),
            },
        )

    if method == "ping":
        return _jsonrpc_success(request_id, {})

    if method == "tools/list":
        return _jsonrpc_success(request_id, {"tools": get_tool_definitions()})

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if name not in _TOOL_HANDLERS:
            return _jsonrpc_error(request_id, -32602, f"Unknown tool: {name}")
        try:
            return _jsonrpc_success(request_id, _mcp_tool_result(execute_tool(name, arguments)))
        except Exception as exc:  # noqa: BLE001 - convert tool faults into MCP-safe payloads
            payload = {
                "tool": name,
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
                "strategy_boundaries": STRATEGY_BOUNDARIES,
            }
            return _jsonrpc_success(request_id, _mcp_tool_result(payload, is_error=True))

    return _jsonrpc_error(request_id, -32601, f"Method not found: {method}")


def serve_stdio() -> None:
    """Run the JSON-RPC stdio server loop."""
    print(f"[{SERVER_NAME}] ready on stdio", file=sys.stderr, flush=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            response = _jsonrpc_error(None, -32700, "Parse error", str(exc))
        else:
            try:
                response = _handle_request(message)
            except Exception as exc:  # noqa: BLE001
                response = _jsonrpc_error(
                    message.get("id"),
                    -32603,
                    "Internal error",
                    {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(limit=4)},
                )
        if response is None:
            continue
        sys.stdout.write(json.dumps(response, ensure_ascii=True, default=_json_default) + "\n")
        sys.stdout.flush()


def main() -> None:
    serve_stdio()


if __name__ == "__main__":
    main()
