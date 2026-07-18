"""Read-only macro/geopolitical intelligence payloads for the Macro Map.

The module keeps the map and MCP tools deterministic when no external macro
feed is available, while exposing provider-ready contracts for future live
news/calendar integrations. It is intentionally read-only and never touches
execution/risk settings.
"""

from __future__ import annotations

import time
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.config import settings


_READ_ONLY_WARNING = (
    "macro_geopolitical_intelligence_is_read_only; "
    "strategy_risk_tiers_and_execution_parameters_are_immutable"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_symbol(symbol: Optional[str]) -> Optional[str]:
    if symbol is None:
        return None
    sym = str(symbol or "").upper().strip()
    if sym.endswith("M") and sym[:-1] in settings.WATCHLIST:
        sym = sym[:-1]
    return sym


def _symbol_matches(row: Dict[str, Any], symbol: Optional[str]) -> bool:
    if not symbol:
        return True
    symbols = [str(s).upper() for s in row.get("symbols", [])]
    return symbol in symbols


def _limit(value: int, default: int = 50) -> int:
    try:
        return max(1, min(int(value or default), 150))
    except (TypeError, ValueError):
        return default


def _numeric_release(value: Any) -> Optional[tuple]:
    """Parse common calendar values such as '+215K' without guessing units."""
    match = re.match(r"^\s*([+-]?\d[\d,]*(?:\.\d+)?)\s*([KMB%]?)\s*$", str(value or ""), re.I)
    if not match:
        return None
    magnitude = float(match.group(1).replace(",", ""))
    suffix = match.group(2).upper()
    return magnitude, suffix


def compute_surprise_delta(metric: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return explicit release surprise metadata while preserving source values."""
    metric = dict(metric or {})
    actual, consensus = _numeric_release(metric.get("actual")), _numeric_release(metric.get("consensus"))
    if not actual or not consensus or actual[1] != consensus[1]:
        return {"surprise_delta": "unavailable", "surprise_delta_value": None, "surprise_direction": "unavailable"}
    delta = actual[0] - consensus[0]
    suffix = actual[1]
    sign = "+" if delta > 0 else ""
    return {
        "surprise_delta": f"{sign}{delta:.2f}{suffix}",
        "surprise_delta_value": delta,
        "surprise_direction": "positive" if delta > 0 else "negative" if delta < 0 else "neutral",
    }


def _with_surprise(row: Dict[str, Any]) -> Dict[str, Any]:
    copy = {**row, "metric": dict(row.get("metric") or {})}
    copy.update(compute_surprise_delta(copy["metric"]))
    return copy


MACRO_EVENTS: List[Dict[str, Any]] = [
    {
        "id": "macro-fomc-washington",
        "title": "FOMC Rate Decision",
        "event": "FOMC Rate Decision",
        "country": "United States",
        "region": "North America",
        "city": "Washington, DC",
        "latitude": 38.9072,
        "longitude": -77.0369,
        "importance": "high",
        "impact_score": 0.92,
        "symbols": ["EURUSD", "GBPUSD", "XAUUSD", "US30", "USTEC", "USDJPY"],
        "metric": {"consensus": "Hold / guidance dependent", "previous": "Policy restrictive"},
        "source": "macro_calendar_proxy",
        "window": "New York session",
    },
    {
        "id": "macro-nfp-washington",
        "title": "US Non-Farm Payrolls",
        "event": "NFP",
        "country": "United States",
        "region": "North America",
        "city": "Washington, DC",
        "latitude": 38.9072,
        "longitude": -77.0369,
        "importance": "high",
        "impact_score": 0.88,
        "symbols": ["XAUUSD", "US30", "USTEC", "USDJPY"],
        "metric": {"actual": "+215K", "consensus": "+180K", "previous": "+165K"},
        "source": "macro_calendar_proxy",
        "window": "Pre-New York cash open",
    },
    {
        "id": "macro-ecb-frankfurt",
        "title": "ECB Rate Decision",
        "event": "ECB",
        "country": "Germany",
        "region": "Europe",
        "city": "Frankfurt",
        "latitude": 50.1109,
        "longitude": 8.6821,
        "importance": "high",
        "impact_score": 0.82,
        "symbols": ["EURUSD", "XAUUSD"],
        "metric": {"focus": "EUR liquidity and rate differential"},
        "source": "macro_calendar_proxy",
        "window": "London session",
    },
    {
        "id": "macro-boe-london",
        "title": "BoE Rate Decision",
        "event": "BoE",
        "country": "United Kingdom",
        "region": "Europe",
        "city": "London",
        "latitude": 51.5074,
        "longitude": -0.1278,
        "importance": "high",
        "impact_score": 0.78,
        "symbols": ["GBPUSD", "XAUUSD"],
        "metric": {"focus": "GBP repricing and gilt volatility"},
        "source": "macro_calendar_proxy",
        "window": "London session",
    },
    {
        "id": "macro-boj-tokyo",
        "title": "BoJ Policy Guidance",
        "event": "BoJ",
        "country": "Japan",
        "region": "Asia",
        "city": "Tokyo",
        "latitude": 35.6762,
        "longitude": 139.6503,
        "importance": "high",
        "impact_score": 0.81,
        "symbols": ["USDJPY", "XAUUSD", "USTEC"],
        "metric": {"focus": "JPY carry unwind sensitivity"},
        "source": "macro_calendar_proxy",
        "window": "Asia session",
    },
    {
        "id": "macro-opec-riyadh",
        "title": "OPEC / Energy Supply Briefing",
        "event": "OPEC",
        "country": "Saudi Arabia",
        "region": "Middle East",
        "city": "Riyadh",
        "latitude": 24.7136,
        "longitude": 46.6753,
        "importance": "medium",
        "impact_score": 0.76,
        "symbols": ["XAUUSD", "US30", "USTEC"],
        "metric": {"focus": "Crude shock -> inflation/yield impulse"},
        "source": "macro_calendar_proxy",
        "window": "Global energy desk",
    },
]


GEOPOLITICAL_FEED: List[Dict[str, Any]] = [
    {
        "id": "geo-hormuz-critical",
        "title": "Iranian navy deploys fast-attack craft to Hormuz chokepoint.",
        "severity": "critical",
        "category": "geopolitical_shock",
        "country": "Iran / Oman",
        "region": "Middle East",
        "latitude": 26.5667,
        "longitude": 56.45,
        "source": "stratfor_core",
        "timestamp": "14:02:11",
        "impact_score": 0.94,
        "symbols": ["XAUUSD", "USDJPY", "US30", "USTEC"],
        "market_vector": "XAUUSD +4.2% volatility | USD assets bullish shift",
        "context": "Naval maneuvers restrict maritime transit. Physical crude vectors compressed.",
    },
    {
        "id": "geo-crude-supply",
        "title": "WTI crude spikes $2.40 on supply disruption fears.",
        "severity": "high_alert",
        "category": "commodity_flows",
        "country": "Saudi Arabia",
        "region": "Middle East",
        "latitude": 24.7136,
        "longitude": 46.6753,
        "source": "energy_flow_proxy",
        "timestamp": "13:58:44",
        "impact_score": 0.86,
        "symbols": ["XAUUSD", "US30", "USTEC"],
        "market_vector": "Energy beta expansion; inflation-sensitive assets repricing",
        "context": "Supply-chain skew is compressing global energy flow liquidity.",
    },
    {
        "id": "geo-jpy-safehaven",
        "title": "Safe haven flows detected: USDJPY short covering initiated.",
        "severity": "market_skew",
        "category": "cross_asset_flow",
        "country": "Japan",
        "region": "Asia",
        "latitude": 35.6762,
        "longitude": 139.6503,
        "source": "fx_flow_proxy",
        "timestamp": "13:55:02",
        "impact_score": 0.74,
        "symbols": ["USDJPY", "XAUUSD"],
        "market_vector": "JPY flow asymmetry; carry baskets de-risking",
        "context": "Cross-asset volatility favors JPY and gold reaction windows.",
    },
    {
        "id": "geo-mof-fx",
        "title": "Japanese Ministry of Finance declines comment on FX volatility.",
        "severity": "neutral",
        "category": "central_bank_milestone",
        "country": "Japan",
        "region": "Asia",
        "latitude": 35.6762,
        "longitude": 139.6503,
        "source": "policy_proxy",
        "timestamp": "13:50:30",
        "impact_score": 0.42,
        "symbols": ["USDJPY"],
        "market_vector": "Intervention risk remains a live tail event",
        "context": "No confirmation, but volatility desk remains alert.",
    },
    {
        "id": "geo-ny-risk",
        "title": "US index futures rebalance around macro event risk.",
        "severity": "market_skew",
        "category": "equity_liquidity",
        "country": "United States",
        "region": "North America",
        "latitude": 40.7128,
        "longitude": -74.0060,
        "source": "ny4_liquidity_proxy",
        "timestamp": "13:44:18",
        "impact_score": 0.68,
        "symbols": ["US30", "USTEC", "XAUUSD"],
        "market_vector": "Index delta hedging concentrated around cash open",
        "context": "Liquidity is deeper but more reflexive near US session open.",
    },
    {
        "id": "geo-eur-swap",
        "title": "Euro area swap desk marks modest rate-volatility steepening.",
        "severity": "medium",
        "category": "rates_flow",
        "country": "Germany",
        "region": "Europe",
        "latitude": 50.1109,
        "longitude": 8.6821,
        "source": "rates_proxy",
        "timestamp": "13:39:10",
        "impact_score": 0.55,
        "symbols": ["EURUSD"],
        "market_vector": "EURUSD inverse skew building into ECB window",
        "context": "Dealer hedging is modestly sensitive to European rates.",
    },
    {
        "id": "geo-london-gbp",
        "title": "Sterling liquidity thins before London close macro repricing.",
        "severity": "medium",
        "category": "fx_liquidity",
        "country": "United Kingdom",
        "region": "Europe",
        "latitude": 51.5074,
        "longitude": -0.1278,
        "source": "london_fx_proxy",
        "timestamp": "13:36:05",
        "impact_score": 0.48,
        "symbols": ["GBPUSD"],
        "market_vector": "GBPUSD neutral volatility with downside skew risk",
        "context": "Liquidity is acceptable; no execution parameter mutation permitted.",
    },
]


SYMBOL_IMPACT: List[Dict[str, Any]] = [
    {"symbol": "XAUUSD", "impact": 0.82, "label": "HIGH", "descriptor": "HIGH CONFLUENCE"},
    {"symbol": "USDJPY", "impact": 0.74, "label": "HIGH", "descriptor": "DOMINANT FLOW"},
    {"symbol": "EURUSD", "impact": -0.41, "label": "MED", "descriptor": "INVERSE SKEW"},
    {"symbol": "US30", "impact": 0.68, "label": "HIGH", "descriptor": "MACRO ADOPTION"},
    {"symbol": "GBPUSD", "impact": 0.12, "label": "LOW", "descriptor": "NEUTRAL VOL"},
    {"symbol": "USTEC", "impact": 0.63, "label": "HIGH", "descriptor": "BETA FLOW"},
    {"symbol": "BTCUSD", "impact": -0.88, "label": "CRIT", "descriptor": "DE-RISK PHASE"},
]


def build_macro_calendar(
    symbol: Optional[str] = None,
    importance: Optional[str] = None,
    country: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    sym = _clean_symbol(symbol)
    imp = str(importance or "").lower().strip() or None
    country_filter = str(country or "").lower().strip() or None
    rows = [
        _with_surprise(row)
        for row in MACRO_EVENTS
        if _symbol_matches(row, sym)
        and (not imp or str(row.get("importance", "")).lower() == imp)
        and (not country_filter or country_filter in str(row.get("country", "")).lower())
    ][:_limit(limit)]
    return {
        "symbol": sym or "GLOBAL",
        "timestamp": _utc_now(),
        "events": rows,
        "count": len(rows),
        "source": "macro_calendar_proxy",
        "warnings": [_READ_ONLY_WARNING],
    }


def build_geopolitical_feed(
    symbol: Optional[str] = None,
    keywords: Optional[str] = None,
    category: Optional[str] = None,
    country: Optional[str] = None,
    source: Optional[str] = None,
    min_latitude: Optional[float] = None,
    max_latitude: Optional[float] = None,
    min_longitude: Optional[float] = None,
    max_longitude: Optional[float] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    sym = _clean_symbol(symbol)
    needle = str(keywords or "").lower().strip() or None
    category_filter = str(category or "").lower().strip() or None
    country_filter = str(country or "").lower().strip() or None
    source_filter = str(source or "").lower().strip() or None

    def _num(value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    lat_min = _num(min_latitude)
    lat_max = _num(max_latitude)
    lon_min = _num(min_longitude)
    lon_max = _num(max_longitude)

    def inside_bbox(row: Dict[str, Any]) -> bool:
        try:
            lat = float(row.get("latitude", 0.0))
            lon = float(row.get("longitude", 0.0))
        except (TypeError, ValueError):
            return False
        if lat_min is not None and lat < lat_min:
            return False
        if lat_max is not None and lat > lat_max:
            return False
        if lon_min is not None and lon < lon_min:
            return False
        if lon_max is not None and lon > lon_max:
            return False
        return True

    rows = [
        {**row}
        for row in GEOPOLITICAL_FEED
        if _symbol_matches(row, sym)
        and (not needle or needle in f"{row.get('title', '')} {row.get('context', '')} {row.get('market_vector', '')}".lower())
        and (not category_filter or category_filter in str(row.get("category", "")).lower())
        and (not country_filter or country_filter in str(row.get("country", "")).lower())
        and (not source_filter or source_filter in str(row.get("source", "")).lower())
        and inside_bbox(row)
    ][:_limit(limit)]
    return {
        "symbol": sym or "GLOBAL",
        "timestamp": _utc_now(),
        "feed": rows,
        "count": len(rows),
        "source": "geopolitical_feed_proxy",
        "warnings": [_READ_ONLY_WARNING],
    }


def _merge_unique(rows: List[Dict[str, Any]], additions: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """Merge resiliently while retaining source data lineage and a hard UI bound."""
    by_id: Dict[str, Dict[str, Any]] = {}
    for row in rows + additions:
        key = str(row.get("id") or f"{row.get('source', 'unknown')}:{row.get('title', '')}")
        by_id.setdefault(key, row)
    return list(by_id.values())[:_limit(limit)]


def _as_hotspot(item: Dict[str, Any], category: str, fallback_symbol: str) -> Dict[str, Any]:
    # Preserve the full source object: the Intel Stream needs the actual,
    # consensus, lineage, location and impact fields after a node is clicked.
    return {
        **item,
        "severity": item.get("severity") or item.get("importance") or "medium",
        "category": item.get("category") or category,
        "symbol": fallback_symbol,
        "symbols": list(item.get("symbols") or ([fallback_symbol] if fallback_symbol != "GLOBAL" else [])),
    }


def _assemble_intelligence(
    symbol: Optional[str],
    calendar: Dict[str, Any],
    feed: Dict[str, Any],
    extra_warnings: Optional[List[str]] = None,
) -> Dict[str, Any]:
    sym = _clean_symbol(symbol) or "GLOBAL"
    hotspots = [_as_hotspot(item, "geopolitical_shock", sym) for item in feed["feed"]]
    event_hotspots = [_as_hotspot(item, "macro_event", sym) for item in calendar["events"]]
    selected_impacts = sorted(
        SYMBOL_IMPACT,
        key=lambda item: 0 if item["symbol"] == sym else 1,
    )
    return {
        "symbol": sym,
        "timestamp": _utc_now(),
        "latency_ms": 12,
        "uptime_pct": 99.9,
        "calendar": calendar["events"],
        "feed": feed["feed"],
        "hotspots": (hotspots + event_hotspots)[:150],
        "symbol_impact": selected_impacts,
        "metrics": {
            "geopolitical_vector": {
                "label": "MIDDLE_EAST_TENSION",
                "status": "CRITICAL",
                "score": 0.91,
            },
            "supply_chain_skew": {
                "label": "ENERGY_FLOWS",
                "status": "COMPRESSED",
                "score": 0.78,
            },
            "liquidity_depth": {
                "label": "GLOBAL_SYNCHRONY",
                "status": "OPTIMAL",
                "score": 0.84,
            },
            "system_status": {
                "node": "NY4",
                "status": "INIT_DIAGNOSTIC",
                "timestamp": time.time(),
            },
        },
        "filters": {
            "regional_filters": True,
            "commodity_flows": True,
            "central_bank_milestones": False,
            "social_sentiment": False,
            "multi_tf_volatility": True,
        },
        "source": "macro_geopolitical_intelligence_proxy",
        "read_only": True,
        "warnings": list(dict.fromkeys([_READ_ONLY_WARNING] + list(extra_warnings or []))),
    }


def build_macro_geopolitical_intelligence(symbol: Optional[str] = None, limit: int = 50) -> Dict[str, Any]:
    """Static deterministic fallback used until the background source pool warms."""
    calendar = build_macro_calendar(symbol=symbol, limit=limit)
    feed = build_geopolitical_feed(symbol=symbol, limit=limit)
    return _assemble_intelligence(symbol, calendar, feed)


def _news_to_calendar_item(item: Dict[str, Any]) -> Dict[str, Any]:
    metric = dict(item.get("metric") or {})
    row = {
        **item,
        "event": item.get("event") or item.get("title"),
        "importance": item.get("importance") or item.get("severity") or "medium",
        "metric": metric,
        "window": item.get("window") or "Live multi-provider release monitor",
    }
    row.update(compute_surprise_delta(metric))
    return row


async def build_macro_calendar_live(
    symbol: Optional[str] = None,
    importance: Optional[str] = None,
    country: Optional[str] = None,
    limit: int = 150,
) -> Dict[str, Any]:
    """Blend public-provider macro reports into the deterministic calendar safely."""
    from core.news_aggregator import get_news_aggregator

    result = build_macro_calendar(symbol=symbol, importance=importance, country=country, limit=limit)
    snapshot = await get_news_aggregator().snapshot(symbol=_clean_symbol(symbol), limit=limit)
    imp = str(importance or "").lower().strip()
    country_filter = str(country or "").lower().strip()
    live_rows = [
        _news_to_calendar_item(item)
        for item in snapshot["items"]
        if item.get("category") == "macro_event"
        and (not imp or str(item.get("severity", "")).lower() == imp)
        and (not country_filter or country_filter in str(item.get("country", "")).lower())
    ]
    result["events"] = _merge_unique(live_rows, result["events"], limit)
    result["count"] = len(result["events"])
    result["source"] = "multi_provider_macro_calendar"
    result["providers"] = snapshot["providers"]
    result["warnings"] = list(dict.fromkeys(result["warnings"] + snapshot["warnings"]))
    return result


async def build_geopolitical_feed_live(
    symbol: Optional[str] = None,
    keywords: Optional[str] = None,
    category: Optional[str] = None,
    country: Optional[str] = None,
    source: Optional[str] = None,
    min_latitude: Optional[float] = None,
    max_latitude: Optional[float] = None,
    min_longitude: Optional[float] = None,
    max_longitude: Optional[float] = None,
    limit: int = 150,
) -> Dict[str, Any]:
    """Blend live classified headlines into the read-only geopolitical stream."""
    from core.news_aggregator import get_news_aggregator

    result = build_geopolitical_feed(symbol, keywords, category, country, source, min_latitude, max_latitude, min_longitude, max_longitude, limit)
    snapshot = await get_news_aggregator().snapshot(symbol=_clean_symbol(symbol), limit=limit)
    needle, category_filter = str(keywords or "").lower().strip(), str(category or "").lower().strip()
    country_filter, source_filter = str(country or "").lower().strip(), str(source or "").lower().strip()

    def in_bounds(item: Dict[str, Any]) -> bool:
        lat, lon = float(item.get("latitude", 0)), float(item.get("longitude", 0))
        return not ((min_latitude is not None and lat < min_latitude) or (max_latitude is not None and lat > max_latitude) or (min_longitude is not None and lon < min_longitude) or (max_longitude is not None and lon > max_longitude))

    live_rows = [
        item for item in snapshot["items"]
        if (not needle or needle in f"{item.get('title', '')} {item.get('context', '')}".lower())
        and (not category_filter or category_filter in str(item.get("category", "")).lower())
        and (not country_filter or country_filter in str(item.get("country", "")).lower())
        and (not source_filter or source_filter in str(item.get("source", "")).lower())
        and in_bounds(item)
    ]
    result["feed"] = _merge_unique(live_rows, result["feed"], limit)
    result["count"] = len(result["feed"])
    result["source"] = "multi_provider_geopolitical_feed"
    result["providers"] = snapshot["providers"]
    result["warnings"] = list(dict.fromkeys(result["warnings"] + snapshot["warnings"]))
    return result


async def build_macro_geopolitical_intelligence_live(symbol: Optional[str] = None, limit: int = 150) -> Dict[str, Any]:
    calendar = await build_macro_calendar_live(symbol=symbol, limit=limit)
    feed = await build_geopolitical_feed_live(symbol=symbol, limit=limit)
    intelligence = _assemble_intelligence(
        symbol,
        calendar,
        feed,
        calendar.get("warnings", []) + feed.get("warnings", []),
    )
    intelligence["providers"] = sorted(
        set(calendar.get("providers", [])) | set(feed.get("providers", []))
    )
    return intelligence
