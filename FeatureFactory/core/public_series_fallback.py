"""Secondary public-series transport for read-only FRED observations.

The primary macro engines use FRED CSV directly.  This adapter supplies an
independent DBnomics/FRED route so a FRED graph timeout or HTTP 429/503 does
not blank the analytical dashboard.  It never reads or writes execution
configuration.
"""

from __future__ import annotations

import csv
import io
import os
from typing import Any, Dict, Iterable, List, Optional

import requests


class PublicFredSeriesFallbackFetcher:
    """Fetch FRED-compatible observations through a secondary public route."""

    _DBNOMICS_URL = "https://api.db.nomics.world/v22/series/FRED/{series_id}?observations=1"

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        session: Optional[Any] = None,
        url_template: Optional[str] = None,
    ) -> None:
        self._timeout_seconds = max(0.5, float(timeout_seconds))
        self._session = session or requests
        self._url_template = (
            url_template
            or os.getenv("TBBFX_FRED_FALLBACK_URL_TEMPLATE")
            or self._DBNOMICS_URL
        )

    def __call__(self, series_id: str) -> List[Dict[str, Any]]:
        series = str(series_id).strip().upper()
        if not series:
            raise ValueError("series_id is required")

        response = self._session.get(
            self._url_template.format(series_id=series),
            timeout=self._timeout_seconds,
            headers={"User-Agent": "TBBFX-FeatureFactory/1.0 (+secondary-read-only-macro)"},
        )
        response.raise_for_status()

        content_type = str(getattr(response, "headers", {}).get("Content-Type", "")).lower()
        if "csv" in content_type or self._url_template.lower().endswith(".csv"):
            return self._normalize_rows(csv.DictReader(io.StringIO(response.text)))

        return self._parse_dbnomics(response.json())

    @classmethod
    def _parse_dbnomics(cls, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return cls._normalize_rows(payload)
        if not isinstance(payload, dict):
            return []

        response = payload.get("dataset") or payload.get("series") or payload
        docs: Any = response.get("series", {}).get("docs") if isinstance(response, dict) else None
        if not docs and isinstance(response, dict):
            docs = response.get("docs")
        if not docs and isinstance(payload.get("series"), dict):
            docs = payload["series"].get("docs")
        if not docs:
            return cls._normalize_rows(payload.get("results") or payload.get("observations") or [])

        document = docs[0] if isinstance(docs, list) and docs else {}
        if not isinstance(document, dict):
            return []
        periods = document.get("period") or document.get("period_start_day") or []
        values = document.get("value") or []
        rows = [
            {"date": period, "value": value}
            for period, value in zip(periods, values)
        ]
        return cls._normalize_rows(rows)

    @staticmethod
    def _normalize_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            date_value = row.get("observation_date") or row.get("DATE") or row.get("date")
            value = row.get("value")
            if value is None:
                value = row.get("Value")
            if not date_value or value in (None, "", "."):
                continue
            try:
                normalized.append({"date": str(date_value), "value": float(value)})
            except (TypeError, ValueError):
                continue
        return sorted(normalized, key=lambda item: item["date"])
