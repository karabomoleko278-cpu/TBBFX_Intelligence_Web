"""Read-only sovereign yield-spread analytics sourced from public FRED series."""

from __future__ import annotations

import copy
import csv
import io
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests


SeriesFetcher = Callable[[str], Iterable[Dict[str, Any]]]


class YieldSpreadEngine:
    """Calculates USD-versus-foreign 10-year yield spreads for FX context only."""

    _FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    _YIELD_SERIES = {
        "US10Y": "DGS10",
        "JP10Y": "IRLTLT01JPM156N",
        "DE10Y": "IRLTLT01DEM156N",
        "UK10Y": "IRLTLT01GBM156N",
    }
    _PAIR_CONFIG = {
        "USDJPY": ("US10Y", "JP10Y"),
        "EURUSD": ("US10Y", "DE10Y"),
        "GBPUSD": ("US10Y", "UK10Y"),
    }

    def __init__(
        self,
        series_fetcher: Optional[SeriesFetcher] = None,
        cache_ttl_seconds: int = 300,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._series_fetcher = series_fetcher
        self._cache_ttl_seconds = cache_ttl_seconds
        self._timeout_seconds = timeout_seconds
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_at = 0.0
        self._lock = threading.Lock()

    def snapshot(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Return current spread context; never alters trading risk or execution profiles."""
        with self._lock:
            if not self._cache or time.monotonic() - self._cache_at >= self._cache_ttl_seconds:
                self._cache = self._build_snapshot()
                self._cache_at = time.monotonic()
            result = copy.deepcopy(self._cache)

        normalized_symbol = self._normalize_symbol(symbol)
        if normalized_symbol:
            matching = [row for row in result["spreads"] if row["symbol"] == normalized_symbol]
            result["spreads"] = matching
            result["requested_symbol"] = normalized_symbol
            if not matching and normalized_symbol not in self._PAIR_CONFIG:
                result["warnings"].append(
                    "%s has no direct sovereign 10-year spread mapping." % normalized_symbol
                )
        return result

    def _build_snapshot(self) -> Dict[str, Any]:
        warnings: List[str] = []
        yield_rows: Dict[str, List[Dict[str, Any]]] = {}
        latest_yields: Dict[str, Dict[str, Any]] = {}

        for code, series_id in self._YIELD_SERIES.items():
            try:
                rows = self._fetch_series(series_id)
                yield_rows[code] = rows
                if not rows:
                    warnings.append("%s returned no usable observations." % code)
                    continue
                latest_yields[code] = {
                    "yield_pct": rows[-1]["value"],
                    "as_of": rows[-1]["date"],
                    "previous_yield_pct": rows[-2]["value"] if len(rows) > 1 else None,
                    "previous_as_of": rows[-2]["date"] if len(rows) > 1 else None,
                    "provider_series": series_id,
                }
            except Exception as exc:  # Display warnings instead of breaking the public dashboard.
                yield_rows[code] = []
                warnings.append("%s unavailable: %s" % (code, str(exc)))

        spreads = [self._build_spread(symbol, latest_yields, warnings) for symbol in self._PAIR_CONFIG]
        status = "available" if any(item["status"] == "available" for item in spreads) else "unavailable"
        as_of = max((item.get("as_of") or "" for item in latest_yields.values()), default=None)
        generated_at = datetime.now(timezone.utc).isoformat()
        return {
            "status": status,
            "provider": "fred_public_csv",
            "as_of": as_of,
            "last_updated": as_of or generated_at,
            "source_frequency": "FRED_DAILY_MIXED",
            "refresh_cadence_seconds": self._cache_ttl_seconds,
            "yields": latest_yields,
            "spreads": spreads,
            "warnings": warnings,
            "read_only": True,
            "advisory_only": True,
            "execution_mutation_allowed": False,
            "generated_at": generated_at,
        }

    def _build_spread(
        self,
        symbol: str,
        latest_yields: Dict[str, Dict[str, Any]],
        warnings: List[str],
    ) -> Dict[str, Any]:
        us_code, foreign_code = self._PAIR_CONFIG[symbol]
        us = latest_yields.get(us_code)
        foreign = latest_yields.get(foreign_code)
        if not us or not foreign:
            return {
                "symbol": symbol,
                "status": "unavailable",
                "spread_name": "%s - %s" % (us_code, foreign_code),
                "warnings": ["Required sovereign yield observation is unavailable."],
            }

        spread_pct = us["yield_pct"] - foreign["yield_pct"]
        delta_bps: Optional[float] = None
        if us.get("previous_yield_pct") is not None and foreign.get("previous_yield_pct") is not None:
            previous_spread = us["previous_yield_pct"] - foreign["previous_yield_pct"]
            delta_bps = (spread_pct - previous_spread) * 100.0

        if delta_bps is None or abs(delta_bps) < 0.01:
            delta_state = "unchanged"
            favors_base_asset = False
        elif delta_bps > 0:
            delta_state = "widening"
            # USDJPY is quoted with USD as base; EURUSD and GBPUSD are inverse to a US spread.
            favors_base_asset = symbol == "USDJPY"
        else:
            delta_state = "contracting"
            favors_base_asset = symbol in ("EURUSD", "GBPUSD")

        if foreign_code != "JP10Y":
            warnings.append(
                "%s delta uses the last two available official observations; the foreign series may not update daily."
                % symbol
            )

        return {
            "symbol": symbol,
            "status": "available",
            "spread_name": "%s - %s" % (us_code, foreign_code),
            "as_of": min(us["as_of"], foreign["as_of"]),
            "us_yield_pct": round(us["yield_pct"], 4),
            "foreign_yield_pct": round(foreign["yield_pct"], 4),
            "yield_spread_pct": round(spread_pct, 4),
            "yield_spread_bps": round(spread_pct * 100.0, 2),
            "delta_bps_24h": round(delta_bps, 2) if delta_bps is not None else None,
            "delta_observation_basis": "previous_available_official_observation",
            "delta_state": delta_state,
            "favors_base_asset": favors_base_asset,
            "provider": "fred_public_csv",
            "read_only": True,
        }

    def _fetch_series(self, series_id: str) -> List[Dict[str, Any]]:
        if self._series_fetcher is not None:
            return self._normalize_rows(self._series_fetcher(series_id))

        response = requests.get(
            self._FRED_CSV_URL.format(series_id=series_id),
            timeout=self._timeout_seconds,
            headers={"User-Agent": "TBBFX-FeatureFactory/1.0 (+read-only-macro-analytics)"},
        )
        response.raise_for_status()
        return self._normalize_rows(csv.DictReader(io.StringIO(response.text)))

    @staticmethod
    def _normalize_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for row in rows:
            date_value = row.get("observation_date") or row.get("DATE") or row.get("date")
            value = row.get("value")
            if value is None:
                for key, candidate in row.items():
                    if key not in ("observation_date", "DATE", "date"):
                        value = candidate
                        break
            if not date_value or value in (None, "", "."):
                continue
            try:
                normalized.append({"date": str(date_value), "value": float(value)})
            except (TypeError, ValueError):
                continue
        return sorted(normalized, key=lambda item: item["date"])

    @staticmethod
    def _normalize_symbol(symbol: Optional[str]) -> Optional[str]:
        if not symbol:
            return None
        normalized = str(symbol).strip().upper()
        return normalized[:-1] if normalized.endswith("M") else normalized


_ENGINE: Optional[YieldSpreadEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_yield_spread_engine() -> YieldSpreadEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = YieldSpreadEngine()
        return _ENGINE
