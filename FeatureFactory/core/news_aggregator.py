"""Read-only, multi-provider macro news aggregation for the Macro Map.

This worker deliberately has no dependency on the execution engine.  It pools
public news/RSS sources, assigns watchlist relevance, and exposes a bounded
in-memory cache so a provider outage cannot stall the trading terminal.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import partial
from html import unescape
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


WATCHLIST = ("EURUSD", "GBPUSD", "XAUUSD", "US30", "USTEC", "USDJPY")

_SYMBOL_KEYWORDS = {
    "XAUUSD": ("gold", "bullion", "safe haven", "safe-haven", "hormuz", "geopolitical tension", "geopolitical escalation"),
    "USTEC": ("earnings", "fomc", "federal reserve", "rate cut", "nvidia", "tech", "yields", "nasdaq"),
    "US30": ("earnings", "fomc", "federal reserve", "rate cut", "yields", "dow", "equity"),
    "EURUSD": ("ecb", "euro", "inflation", "cpi", "nfp", "dxy", "eurozone"),
    "GBPUSD": ("boe", "sterling", "british pound", "inflation", "cpi", "nfp", "dxy", "gilt"),
    "USDJPY": ("boj", "yen", "japan", "inflation", "cpi", "nfp", "dxy", "intervention"),
}

_CRITICAL_TERMS = (
    "strait of hormuz", "hormuz", "geopolitical escalation", "war", "military strike",
    "cpi shock", "nfp shock", "emergency rate", "supply disruption", "oil supply",
)
_HIGH_TERMS = ("fomc", "federal reserve", "ecb", "boe", "boj", "cpi", "nfp", "inflation", "rate decision", "yields")
_MACRO_TERMS = _HIGH_TERMS + ("payroll", "gdp", "pmi", "employment", "central bank")

# Conservative regional anchors.  Feed payloads retain this data lineage rather
# than pretending an article has a precise location when it only has a region.
_REGION_RULES: Tuple[Tuple[Tuple[str, ...], str, str, str, float, float], ...] = (
    (("hormuz", "iran", "oman", "persian gulf"), "Iran / Oman", "Middle East", "Strait of Hormuz", 26.5667, 56.4500),
    (("saudi", "opec", "riyadh", "crude", "oil"), "Saudi Arabia", "Middle East", "Riyadh", 24.7136, 46.6753),
    (("ecb", "eurozone", "euro area", "germany", "frankfurt"), "Germany", "Europe", "Frankfurt", 50.1109, 8.6821),
    (("boe", "sterling", "britain", "uk ", "london"), "United Kingdom", "Europe", "London", 51.5074, -0.1278),
    (("boj", "yen", "japan", "tokyo"), "Japan", "Asia", "Tokyo", 35.6762, 139.6503),
    (("fomc", "federal reserve", "nfp", "payroll", "nvidia", "nasdaq", "united states", "u.s."), "United States", "North America", "Washington, DC", 38.9072, -77.0369),
    (("china", "beijing", "shanghai"), "China", "Asia", "Shanghai", 31.2304, 121.4737),
)


def _strip_markup(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(str(value or "")))).strip()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return _utc_now()
    try:
        return parsedate_to_datetime(text).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, IndexError):
        return text


def _location_for(text: str) -> Dict[str, Any]:
    lowered = text.lower()
    for terms, country, region, city, latitude, longitude in _REGION_RULES:
        if any(term in lowered for term in terms):
            return {"country": country, "region": region, "city": city, "latitude": latitude, "longitude": longitude}
    return {"country": "Global", "region": "Global", "city": "Global Markets", "latitude": 20.0, "longitude": 0.0}


def classify_headline(headline: str, summary: str = "", provider: str = "") -> Dict[str, Any]:
    """Return immutable presentation metadata for one read-only news item."""
    text = f"{headline} {summary} {provider}".lower()
    symbols = sorted(symbol for symbol, terms in _SYMBOL_KEYWORDS.items() if any(term in text for term in terms))
    if any(term in text for term in _CRITICAL_TERMS):
        severity, score = "critical", 0.96
        symbols = sorted(set(symbols).union(("XAUUSD", "USDJPY", "US30", "USTEC")))
    elif any(term in text for term in _HIGH_TERMS):
        severity, score = "high", 0.84
    else:
        severity, score = "medium", 0.58

    if any(term in text for term in ("war", "military", "hormuz", "oil", "supply disruption", "geopolitical")):
        category = "geopolitical_shock"
    elif any(term in text for term in _MACRO_TERMS):
        category = "macro_event"
    else:
        category = "market_news"
    return {"symbols": symbols, "severity": severity, "impact_score": score, "category": category, **_location_for(text)}


def _rss_items(xml_text: str, provider: str) -> List[Dict[str, Any]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    rows: List[Dict[str, Any]] = []
    for item in root.findall(".//item"):
        title = _strip_markup(item.findtext("title"))
        if not title:
            continue
        summary = _strip_markup(item.findtext("description") or item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded"))
        published = item.findtext("pubDate") or item.findtext("published") or item.findtext("updated")
        link = _strip_markup(item.findtext("link"))
        metadata = classify_headline(title, summary, provider)
        digest = hashlib.sha256(f"{provider}|{title}|{link}".encode("utf-8")).hexdigest()[:20]
        rows.append({
            "id": f"{provider}-{digest}", "title": title, "headline": title, "context": summary or title,
            "source": provider, "timestamp": _safe_timestamp(published), "link": link,
            "market_vector": f"Affected: {', '.join(metadata['symbols']) or 'GLOBAL'}",
            **metadata,
        })
    return rows


def _fetch_rss(url: str, provider: str) -> List[Dict[str, Any]]:
    response = requests.get(url, headers={"User-Agent": "TBBFX-Macro-Research/1.0"}, timeout=5)
    response.raise_for_status()
    return _rss_items(response.text, provider)


def _fetch_tiingo(api_key: str) -> List[Dict[str, Any]]:
    response = requests.get(
        "https://api.tiingo.com/tiingo/news",
        params={"token": api_key, "limit": 100},
        headers={"User-Agent": "TBBFX-Macro-Research/1.0"},
        timeout=5,
    )
    response.raise_for_status()
    rows: List[Dict[str, Any]] = []
    for item in response.json() or []:
        title = _strip_markup(item.get("title"))
        if not title:
            continue
        summary = _strip_markup(item.get("description"))
        metadata = classify_headline(title, summary, "tiingo_news")
        digest = hashlib.sha256(f"tiingo|{title}|{item.get('url', '')}".encode("utf-8")).hexdigest()[:20]
        rows.append({
            "id": f"tiingo-{digest}", "title": title, "headline": title, "context": summary or title,
            "source": "tiingo_news", "timestamp": _safe_timestamp(item.get("publishedDate")), "link": item.get("url", ""),
            "market_vector": f"Affected: {', '.join(metadata['symbols']) or 'GLOBAL'}", **metadata,
        })
    return rows


class MultiProviderNewsAggregator:
    """Bounded, best-effort public-news cache with no execution-side effects."""

    def __init__(self) -> None:
        self._cache: List[Dict[str, Any]] = []
        self._warnings: List[str] = []
        self._last_refresh = 0.0
        self._last_updated: Optional[str] = None
        self._data_status = "SERVICE_TEMPORARILY_OFFLINE"
        self._refresh_seconds = max(30, int(os.getenv("TBBFX_MACRO_NEWS_REFRESH_SECONDS", "90")))
        self._refresh_lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _worker(self) -> None:
        while not self._stop_event.is_set():
            await self.refresh(force=True)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._refresh_seconds)
            except asyncio.TimeoutError:
                pass

    async def refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_refresh < self._refresh_seconds:
            return
        async with self._refresh_lock:
            if not force and time.monotonic() - self._last_refresh < self._refresh_seconds:
                return
            loop = asyncio.get_running_loop()
            providers: List[Tuple[str, Any, Tuple[Any, ...]]] = [
                ("biztoc_rss", _fetch_rss, ("https://biztoc.com/rss.xml", "biztoc_rss")),
                ("federal_reserve_rss", _fetch_rss, ("https://www.federalreserve.gov/feeds/press_all.xml", "federal_reserve_rss")),
                ("bank_of_england_rss", _fetch_rss, ("https://www.bankofengland.co.uk/rss/news", "bank_of_england_rss")),
            ]
            tiingo_key = os.getenv("TIINGO_API_KEY", "").strip()
            if tiingo_key:
                providers.append(("tiingo_news", _fetch_tiingo, (tiingo_key,)))

            pending = [loop.run_in_executor(None, partial(fetcher, *args)) for _, fetcher, args in providers]
            responses = await asyncio.gather(*pending, return_exceptions=True)
            rows: List[Dict[str, Any]] = []
            warnings: List[str] = []
            for (provider, _, _), response in zip(providers, responses):
                if isinstance(response, Exception):
                    warnings.append(f"{provider} unavailable: {type(response).__name__}")
                else:
                    rows.extend(response)

            deduped: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                deduped.setdefault(str(row["id"]), row)
            refreshed = sorted(
                deduped.values(),
                key=lambda item: str(item.get("timestamp", "")),
                reverse=True,
            )[:150]
            if refreshed:
                self._cache = refreshed
                self._last_updated = _utc_now()
                self._data_status = (
                    "FALLBACK_REDUNDANCY_ACTIVE" if warnings else "LIVE_PRIMARY"
                )
            elif self._cache:
                warnings.append(
                    "All live news providers are unavailable; serving the last successful in-memory snapshot."
                )
                self._data_status = "FALLBACK_REDUNDANCY_ACTIVE"
            else:
                warnings.append(
                    "All live news providers and the local news snapshot are unavailable."
                )
                self._data_status = "SERVICE_TEMPORARILY_OFFLINE"
            self._warnings = list(dict.fromkeys(warnings))[:8]
            self._last_refresh = time.monotonic()

    async def snapshot(self, symbol: Optional[str] = None, limit: int = 150) -> Dict[str, Any]:
        if time.monotonic() - self._last_refresh >= self._refresh_seconds and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._worker())
        sym = str(symbol or "").upper().strip()
        rows = list(self._cache)
        if sym:
            rows = [row for row in rows if sym in row.get("symbols", [])]
        status_warning = (
            self._data_status if self._data_status != "LIVE_PRIMARY" else None
        )
        return {
            "items": rows[:max(1, min(int(limit or 150), 150))],
            "providers": sorted(set(row.get("source", "unknown") for row in rows)),
            "warnings": list(self._warnings),
            "timestamp": _utc_now(),
            "generated_at": _utc_now(),
            "last_updated": self._last_updated or _utc_now(),
            "count": len(rows),
            # A symbol filter may legitimately return no matching stories while
            # the underlying provider cache remains healthy.
            "status": "available" if self._cache else "unavailable",
            "data_status": self._data_status,
            "status_warning": status_warning,
            "read_only": True,
            "advisory_only": True,
            "execution_mutation_allowed": False,
        }


_aggregator: Optional[MultiProviderNewsAggregator] = None


def get_news_aggregator() -> MultiProviderNewsAggregator:
    global _aggregator
    if _aggregator is None:
        _aggregator = MultiProviderNewsAggregator()
    return _aggregator
