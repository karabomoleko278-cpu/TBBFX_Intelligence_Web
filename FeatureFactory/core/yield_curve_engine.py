"""Read-only US Treasury yield-curve analytics sourced from public FRED data.

The curve engine is deliberately isolated from execution, position sizing, and
strategy optimization.  It publishes macro context only.
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


class YieldCurveEngine:
    """Calculate the US 10Y-2Y slope and expose an inversion advisory."""

    _FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    _SERIES = ("DGS2", "DGS10", "T10Y2Y")
    _SOURCE_FREQUENCY = "FRED_DAILY_TREASURY"

    def __init__(
        self,
        series_fetcher: Optional[SeriesFetcher] = None,
        cache_ttl_seconds: int = 300,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._series_fetcher = series_fetcher
        self._cache_ttl_seconds = max(30, int(cache_ttl_seconds))
        self._timeout_seconds = timeout_seconds
        self._cache: Optional[Dict[str, Any]] = None
        self._cache_at = 0.0
        self._lock = threading.Lock()

    def snapshot(self) -> Dict[str, Any]:
        """Return a cached curve snapshot without mutating any trading state."""
        with self._lock:
            if self._cache and time.monotonic() - self._cache_at < self._cache_ttl_seconds:
                return copy.deepcopy(self._cache)
            self._cache = self._build_snapshot()
            self._cache_at = time.monotonic()
            return copy.deepcopy(self._cache)

    def _build_snapshot(self) -> Dict[str, Any]:
        warnings: List[str] = []
        rows: Dict[str, List[Dict[str, Any]]] = {}
        for series_id in self._SERIES:
            try:
                rows[series_id] = self._fetch_series(series_id)
                if not rows[series_id]:
                    warnings.append("%s returned no usable observations." % series_id)
            except Exception as exc:  # Provider faults remain visible but non-fatal.
                rows[series_id] = []
                warnings.append("%s unavailable: %s" % (series_id, str(exc)))

        aligned = self._latest_aligned(rows.get("DGS2", []), rows.get("DGS10", []))
        if aligned is None:
            return self._unavailable_snapshot(warnings)

        as_of = aligned["date"]
        two_year = aligned["dgs2"]
        ten_year = aligned["dgs10"]
        calculated_slope = ten_year - two_year
        reported_row = self._latest_at_or_before(rows.get("T10Y2Y", []), as_of)
        reported_slope = reported_row["value"] if reported_row else None
        inverted = calculated_slope < 0.0
        flat = abs(calculated_slope) < 0.10
        curve_state = "INVERTED" if inverted else "FLAT" if flat else "UPWARD_SLOPING"
        generated_at = datetime.now(timezone.utc).isoformat()

        if reported_slope is None:
            warnings.append("T10Y2Y comparison series is unavailable; calculated DGS10-DGS2 is authoritative.")

        return {
            "status": "available",
            "provider": "fred_public_csv",
            "provider_series": list(self._SERIES),
            "as_of": as_of,
            "last_updated": as_of,
            "source_frequency": self._SOURCE_FREQUENCY,
            "refresh_cadence_seconds": self._cache_ttl_seconds,
            "us2y_yield_pct": round(two_year, 4),
            "us10y_yield_pct": round(ten_year, 4),
            "calculated_slope_pct": round(calculated_slope, 4),
            "calculated_slope_bps": round(calculated_slope * 100.0, 2),
            "fred_reported_slope_pct": round(reported_slope, 4) if reported_slope is not None else None,
            "slope_difference_bps": (
                round((calculated_slope - reported_slope) * 100.0, 2)
                if reported_slope is not None
                else None
            ),
            "yield_curve_inverted": inverted,
            "curve_state": curve_state,
            "safe_haven_signal": {
                "active": inverted,
                "level": "ELEVATED" if inverted else "NORMAL",
                "target_assets": ["XAUUSD", "CASH_EQUIVALENTS"],
                "description": (
                    "Curve inversion is an analytical safe-haven context signal only."
                    if inverted
                    else "The 10Y-2Y curve is not currently inverted."
                ),
            },
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
            headers={"User-Agent": "TBBFX-FeatureFactory/1.0 (+read-only-yield-curve)"},
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
    def _latest_aligned(
        two_year_rows: List[Dict[str, Any]],
        ten_year_rows: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not two_year_rows or not ten_year_rows:
            return None
        two_by_date = {row["date"]: row["value"] for row in two_year_rows}
        ten_by_date = {row["date"]: row["value"] for row in ten_year_rows}
        common_dates = sorted(set(two_by_date).intersection(ten_by_date))
        if not common_dates:
            return None
        latest_date = common_dates[-1]
        return {
            "date": latest_date,
            "dgs2": two_by_date[latest_date],
            "dgs10": ten_by_date[latest_date],
        }

    @staticmethod
    def _latest_at_or_before(
        rows: List[Dict[str, Any]],
        date_value: str,
    ) -> Optional[Dict[str, Any]]:
        eligible = [row for row in rows if row["date"] <= date_value]
        return eligible[-1] if eligible else None

    def _unavailable_snapshot(self, warnings: List[str]) -> Dict[str, Any]:
        generated_at = datetime.now(timezone.utc).isoformat()
        return {
            "status": "unavailable",
            "provider": "fred_public_csv",
            "provider_series": list(self._SERIES),
            "as_of": None,
            "last_updated": generated_at,
            "source_frequency": self._SOURCE_FREQUENCY,
            "refresh_cadence_seconds": self._cache_ttl_seconds,
            "us2y_yield_pct": None,
            "us10y_yield_pct": None,
            "calculated_slope_pct": None,
            "calculated_slope_bps": None,
            "fred_reported_slope_pct": None,
            "slope_difference_bps": None,
            "yield_curve_inverted": None,
            "curve_state": "UNAVAILABLE",
            "safe_haven_signal": {
                "active": False,
                "level": "UNAVAILABLE",
                "target_assets": ["XAUUSD", "CASH_EQUIVALENTS"],
                "description": "Yield-curve data is temporarily unavailable.",
            },
            "warnings": warnings or ["FRED yield-curve data is currently unavailable."],
            "read_only": True,
            "advisory_only": True,
            "execution_mutation_allowed": False,
            "generated_at": generated_at,
        }


_ENGINE: Optional[YieldCurveEngine] = None
_ENGINE_LOCK = threading.Lock()


def get_yield_curve_engine() -> YieldCurveEngine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = YieldCurveEngine()
        return _ENGINE
