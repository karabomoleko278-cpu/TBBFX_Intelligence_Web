"""Read-only CFTC Commitments of Traders positioning analytics.

The public CFTC report is an advisory macro input.  It only emits positioning
telemetry and warnings; it cannot mutate TBBFX risk tiers or trade execution.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

from core.news_aggregator import WATCHLIST


@dataclass(frozen=True)
class CotContract:
    symbol: str
    cftc_code: str
    market_name: str
    venue: str


CONTRACTS: Dict[str, CotContract] = {
    "EURUSD": CotContract("EURUSD", "099741", "Euro FX / CME", "CME"),
    "GBPUSD": CotContract("GBPUSD", "096742", "British Pound Sterling / CME", "CME"),
    "USDJPY": CotContract("USDJPY", "097741", "Japanese Yen / CME", "CME"),
    "XAUUSD": CotContract("XAUUSD", "088691", "Gold / COMEX", "COMEX"),
    "US30": CotContract("US30", "124601", "Dow / E-mini", "CBOT"),
    "USTEC": CotContract("USTEC", "209742", "Nasdaq-100 / E-mini", "CME"),
}


class CotPositioningEngine:
    """Fetch and normalize COT records with bounded caching and graceful failover."""

    _SOURCES: Tuple[str, ...] = (
        "https://www.cftc.gov/dea/newcot/FinFutWk.txt",
        "https://www.cftc.gov/dea/newcot/FutWk.txt",
    )

    def __init__(
        self,
        source_fetcher: Optional[Callable[[], Iterable[Dict[str, Any]]]] = None,
        history_provider: Optional[Callable[[str], Iterable[float]]] = None,
        cache_ttl_seconds: int = 1800,
    ) -> None:
        self._source_fetcher = source_fetcher
        self._history_provider = history_provider
        self._cache_ttl_seconds = max(60, cache_ttl_seconds)
        self._cached_rows: List[Dict[str, Any]] = []
        self._cached_at = 0.0
        self._cached_warnings: List[str] = []

    @staticmethod
    def _normalise_symbol(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        symbol = str(value).upper().strip()
        return symbol[:-1] if symbol.endswith("M") and symbol[:-1] in WATCHLIST else symbol

    @staticmethod
    def _parse_int(value: Any) -> int:
        try:
            return int(str(value or "0").replace(",", "").strip())
        except (TypeError, ValueError):
            return 0

    def _parse_report(self, text: str, source: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for contract in CONTRACTS.values():
            pattern = re.compile(
                r"(?P<block>.*?Code-\s*%s.*?)(?=\n\s*.*?Code-\s*\d+|\Z)" % re.escape(contract.cftc_code),
                re.IGNORECASE | re.DOTALL,
            )
            match = pattern.search(text)
            if not match:
                continue
            block = match.group("block")

            def field(label: str) -> int:
                found = re.search(label + r"\s*:\s*([\d,]+)", block, re.IGNORECASE)
                return self._parse_int(found.group(1) if found else 0)

            noncommercial_long = field(r"Noncommercial Positions-Long \(All\)")
            noncommercial_short = field(r"Noncommercial Positions-Short \(All\)")
            commercial_long = field(r"Commercial Positions-Long \(All\)")
            commercial_short = field(r"Commercial Positions-Short \(All\)")
            if not (noncommercial_long or noncommercial_short or commercial_long or commercial_short):
                continue
            rows.append(
                {
                    "symbol": contract.symbol,
                    "contract": contract.market_name,
                    "venue": contract.venue,
                    "cftc_code": contract.cftc_code,
                    "noncommercial_long": noncommercial_long,
                    "noncommercial_short": noncommercial_short,
                    "commercial_long": commercial_long,
                    "commercial_short": commercial_short,
                    "provider": "cftc",
                    "source": source,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
        return rows

    def _fetch_live_rows(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        if self._source_fetcher is not None:
            return list(self._source_fetcher()), []

        warnings: List[str] = []
        collected: Dict[str, Dict[str, Any]] = {}
        for source in self._SOURCES:
            try:
                response = requests.get(source, timeout=2.5, headers={"User-Agent": "TBBFX-Research/1.0"})
                response.raise_for_status()
                for row in self._parse_report(response.text, source):
                    collected.setdefault(str(row["symbol"]), row)
            except requests.RequestException as error:
                warnings.append("CFTC source unavailable (%s): %s" % (source.rsplit("/", 1)[-1], str(error)))
        return list(collected.values()), warnings

    def _rows(self) -> Tuple[List[Dict[str, Any]], List[str]]:
        if self._cached_rows and time.monotonic() - self._cached_at < self._cache_ttl_seconds:
            return list(self._cached_rows), list(self._cached_warnings)
        rows, warnings = self._fetch_live_rows()
        # Keep a prior valid snapshot during a transient CFTC outage.
        if rows:
            self._cached_rows = list(rows)
            self._cached_at = time.monotonic()
            self._cached_warnings = list(warnings)
            return rows, warnings
        if self._cached_rows:
            return list(self._cached_rows), warnings + ["CFTC live refresh failed; using the last valid local snapshot."]
        return [], warnings

    def _history(self, symbol: str, current_net: int) -> List[float]:
        if self._history_provider is None:
            return [float(current_net)]
        values: List[float] = []
        for value in self._history_provider(symbol) or []:
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
        values.append(float(current_net))
        return values[-52:]

    def snapshot(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        requested = self._normalise_symbol(symbol)
        symbols: Sequence[str] = (requested,) if requested in CONTRACTS else tuple(CONTRACTS)
        rows, warnings = self._rows()
        by_symbol = {str(row.get("symbol", "")).upper(): row for row in rows}
        positions: List[Dict[str, Any]] = []

        for code in symbols:
            contract = CONTRACTS[code]
            row = by_symbol.get(code)
            if row is None:
                positions.append(
                    {
                        "symbol": code,
                        "contract": contract.market_name,
                        "venue": contract.venue,
                        "cftc_code": contract.cftc_code,
                        "status": "unavailable",
                        "net_speculative_contracts": None,
                        "percentile_skew_52w": None,
                        "leveraged_to_commercial_ratio": None,
                    }
                )
                continue

            net_speculative = int(row["noncommercial_long"]) - int(row["noncommercial_short"])
            net_commercial = int(row["commercial_long"]) - int(row["commercial_short"])
            history = self._history(code, net_speculative)
            rank = sum(1 for value in history if value <= net_speculative)
            percentile = round((rank / max(1, len(history))) * 100.0, 2)
            ratio = round(net_speculative / max(1.0, abs(net_commercial)), 4)
            positions.append(
                {
                    **row,
                    "status": "available",
                    "net_speculative_contracts": net_speculative,
                    "net_commercial_contracts": net_commercial,
                    "percentile_skew_52w": percentile,
                    "history_samples": len(history),
                    "leveraged_to_commercial_ratio": ratio,
                    "position_bias": "net_long" if net_speculative > 0 else "net_short" if net_speculative < 0 else "flat",
                }
            )

        if any(item.get("history_samples", 0) < 52 for item in positions if item.get("status") == "available"):
            warnings.append("CFTC 52-week percentile is based on the currently available local history samples.")
        generated_at = datetime.now(timezone.utc).isoformat()
        return {
            "status": "available" if any(item.get("status") == "available" for item in positions) else "unavailable",
            "as_of": generated_at,
            "last_updated": generated_at,
            "positions": positions,
            "provider": "cftc",
            "source_frequency": "CFTC_WEEKLY",
            "refresh_cadence_seconds": self._cache_ttl_seconds,
            "warnings": list(dict.fromkeys(warnings)),
            "read_only": True,
            "advisory_only": True,
            "execution_mutation_allowed": False,
            "generated_at": generated_at,
        }


_default_engine: Optional[CotPositioningEngine] = None


def get_cot_positioning_engine() -> CotPositioningEngine:
    global _default_engine
    if _default_engine is None:
        _default_engine = CotPositioningEngine()
    return _default_engine
