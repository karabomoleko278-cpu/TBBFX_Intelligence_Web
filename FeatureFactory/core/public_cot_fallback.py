"""Secondary CFTC public-reporting source for read-only COT telemetry."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

import requests

from core.cot_positioning import CONTRACTS


class PublicCftcFallbackFetcher:
    """Fetch the latest watchlist records from CFTC's Socrata dataset.

    The primary engine consumes the CFTC weekly text reports. This independent
    JSON route is deliberately used only as a secondary provider.
    """

    DATASET_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        timeout_seconds: float = 4.0,
    ) -> None:
        self._session = session or requests.Session()
        self._timeout_seconds = max(0.5, float(timeout_seconds))

    @staticmethod
    def _number(row: Dict[str, Any], *keys: str) -> int:
        for key in keys:
            value = row.get(key)
            if value not in (None, ""):
                try:
                    return int(float(str(value).replace(",", "")))
                except (TypeError, ValueError):
                    continue
        return 0

    def __call__(self) -> Iterable[Dict[str, Any]]:
        codes = ",".join("'%s'" % contract.cftc_code for contract in CONTRACTS.values())
        params = {
            "$select": (
                "report_date_as_yyyy_mm_dd,cftc_contract_market_code,"
                "market_and_exchange_names,noncomm_positions_long_all,"
                "noncomm_positions_short_all,comm_positions_long_all,"
                "comm_positions_short_all"
            ),
            "$where": "cftc_contract_market_code in (%s)" % codes,
            "$order": "report_date_as_yyyy_mm_dd DESC",
            "$limit": "500",
        }
        response = self._session.get(
            self.DATASET_URL,
            params=params,
            timeout=self._timeout_seconds,
            headers={"User-Agent": "TBBFX-Research/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("CFTC public-reporting fallback returned a non-list payload")

        contracts_by_code = {contract.cftc_code: contract for contract in CONTRACTS.values()}
        latest_by_symbol: Dict[str, Dict[str, Any]] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            code = str(item.get("cftc_contract_market_code") or "").strip()
            contract = contracts_by_code.get(code)
            if contract is None or contract.symbol in latest_by_symbol:
                continue
            latest_by_symbol[contract.symbol] = {
                "symbol": contract.symbol,
                "contract": str(item.get("market_and_exchange_names") or contract.market_name),
                "venue": contract.venue,
                "cftc_code": code,
                "noncommercial_long": self._number(item, "noncomm_positions_long_all"),
                "noncommercial_short": self._number(item, "noncomm_positions_short_all"),
                "commercial_long": self._number(item, "comm_positions_long_all"),
                "commercial_short": self._number(item, "comm_positions_short_all"),
                "provider": "cftc_public_reporting_socrata",
                "source": self.DATASET_URL,
                "report_date": item.get("report_date_as_yyyy_mm_dd"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        rows: List[Dict[str, Any]] = list(latest_by_symbol.values())
        if not rows:
            raise ValueError("CFTC public-reporting fallback returned no watchlist contracts")
        return rows
