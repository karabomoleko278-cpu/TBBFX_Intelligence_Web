"""Read-only Federal Reserve USD net-liquidity analytics.

This module is intentionally isolated from strategy, execution, and sizing code. It
only normalizes public FRED observations for the macro-information workspace.
"""

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


class FedNetLiquidityEngine:
    """Calculates WALCL - WLRRAL - WDTGAL from public FRED observations."""

    _FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    _SERIES = {
        "WALCL": "Federal Reserve total assets",
        "WLRRAL": "Reverse repurchase agreements",
        "WDTGAL": "US Treasury General Account",
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

    def snapshot(self) -> Dict[str, Any]:
        """Return a cached, read-only liquidity snapshot without mutating trading state."""
        with self._lock:
            if self._cache and time.monotonic() - self._cache_at < self._cache_ttl_seconds:
                return copy.deepcopy(self._cache)

            snapshot = self._build_snapshot()
            self._cache = snapshot
            self._cache_at = time.monotonic()
            return copy.deepcopy(snapshot)

    def _build_snapshot(self) -> Dict[str, Any]:
        warnings: List[str] = []
        series_data: Dict[str, List[Dict[str, Any]]] = {}

        for series_id in self._SERIES:
            try:
                observations = self._fetch_series(series_id)
                if not observations:
                    warnings.append("%s returned no usable observations." % series_id)
                series_data[series_id] = observations
            except Exception as exc:  # Keep visual macro analysis resilient to provider outages.
                warnings.append("%s unavailable: %s" % (series_id, str(exc)))
                series_data[series_id] = []

        aligned = self._align_observations(series_data)
        if not aligned:
            return self._unavailable_snapshot(warnings)

        latest = aligned[-1]
        latest_value = latest["net_liquidity_millions"]
        recent_values = [row["net_liquidity_millions"] for row in aligned[-10:]]
        sma_10_millions = sum(recent_values) / float(len(recent_values))
        momentum_millions = latest_value - sma_10_millions
        momentum_billions = momentum_millions / 1000.0

        if momentum_billions > 0.05:
            direction = "EXPANDING"
        elif momentum_billions < -0.05:
            direction = "CONTRACTING"
        else:
            direction = "STABLE"

        sign = "+" if momentum_billions >= 0 else ""
        weekly_history = self._weekly_history(aligned)
        generated_at = datetime.now(timezone.utc).isoformat()
        return {
            "status": "available",
            "as_of": latest["date"],
            "last_updated": latest["date"],
            "provider": "fred_public_csv",
            "source_frequency": "FRED_WEEKLY_MIXED",
            "refresh_cadence_seconds": self._cache_ttl_seconds,
            "series_units": "USD millions",
            "formula": "WALCL - WLRRAL - WDTGAL",
            "net_liquidity_millions": round(latest_value, 2),
            "net_liquidity_billions": round(latest_value / 1000.0, 2),
            "sma_10d_billions": round(sma_10_millions / 1000.0, 2),
            "momentum_billions": round(momentum_billions, 2),
            "liquidity_momentum_direction": direction,
            "liquidity_momentum": "%s (%s%.1fB)" % (direction, sign, momentum_billions),
            "historical_weekly": weekly_history,
            "warnings": warnings,
            "read_only": True,
            "advisory_only": True,
            "execution_mutation_allowed": False,
            "generated_at": generated_at,
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
    def _align_observations(series_data: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        walcl = series_data.get("WALCL", [])
        reverse_repo = series_data.get("WLRRAL", [])
        treasury = series_data.get("WDTGAL", [])
        if not walcl or not reverse_repo or not treasury:
            return []

        reverse_index = 0
        treasury_index = 0
        current_reverse: Optional[float] = None
        current_treasury: Optional[float] = None
        aligned: List[Dict[str, Any]] = []

        for assets_row in walcl:
            date_value = assets_row["date"]
            while reverse_index < len(reverse_repo) and reverse_repo[reverse_index]["date"] <= date_value:
                current_reverse = reverse_repo[reverse_index]["value"]
                reverse_index += 1
            while treasury_index < len(treasury) and treasury[treasury_index]["date"] <= date_value:
                current_treasury = treasury[treasury_index]["value"]
                treasury_index += 1
            if current_reverse is None or current_treasury is None:
                continue

            net_liquidity = assets_row["value"] - current_reverse - current_treasury
            aligned.append(
                {
                    "date": date_value,
                    "walcl_millions": assets_row["value"],
                    "wlrral_millions": current_reverse,
                    "wdtgal_millions": current_treasury,
                    "net_liquidity_millions": net_liquidity,
                }
            )
        return aligned

    @staticmethod
    def _weekly_history(aligned: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        sampled = aligned[::5] if len(aligned) > 5 else aligned
        return [
            {
                "date": row["date"],
                "net_liquidity_billions": round(row["net_liquidity_millions"] / 1000.0, 2),
            }
            for row in sampled[-52:]
        ]

    @staticmethod
    def _unavailable_snapshot(warnings: List[str]) -> Dict[str, Any]:
        generated_at = datetime.now(timezone.utc).isoformat()
        return {
            "status": "unavailable",
            "as_of": None,
            "last_updated": generated_at,
            "provider": "fred_public_csv",
            "source_frequency": "FRED_WEEKLY_MIXED",
            "refresh_cadence_seconds": 300,
            "series_units": "USD millions",
            "formula": "WALCL - WLRRAL - WDTGAL",
            "net_liquidity_millions": None,
            "net_liquidity_billions": None,
            "sma_10d_billions": None,
            "momentum_billions": None,
            "liquidity_momentum_direction": "UNAVAILABLE",
            "liquidity_momentum": "UNAVAILABLE",
            "historical_weekly": [],
            "warnings": warnings or ["FRED liquidity data is currently unavailable."],
            "read_only": True,
            "advisory_only": True,
            "execution_mutation_allowed": False,
            "generated_at": generated_at,
        }


_ENGINE: Optional[FedNetLiquidityEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_fed_net_liquidity_engine() -> FedNetLiquidityEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = FedNetLiquidityEngine()
        return _ENGINE
