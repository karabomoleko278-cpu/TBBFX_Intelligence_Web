"""
Institutional ex-post portfolio risk analytics for TBBFX.

The engine is intentionally read-only: it calculates rolling parametric VaR
from persisted market observations, then returns passive circuit-breaker
telemetry for governance tools. It never mutates symbol risk tiers, stop
policies, target-R settings, or execution-frequency rules.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

from core.config import settings
from core.state_db import StateDatabase, get_state_db


Z_SCORE_95 = 1.645
Z_SCORE_99 = 2.326
DEFAULT_ROLLING_WINDOW = 500
DEFAULT_ACCOUNT_BALANCE_ZAR = float(os.getenv("TBBFX_ACCOUNT_BALANCE_ZAR", "2500"))
DEFAULT_MAX_VAR_FRACTION = float(os.getenv("TBBFX_MAX_VAR_FRACTION", "0.20"))


class PortfolioRiskEngine:
    """Parametric Variance-Covariance VaR calculator for watchlisted symbols."""

    def __init__(
        self,
        db: Optional[StateDatabase] = None,
        rolling_window: int = DEFAULT_ROLLING_WINDOW,
        account_balance_zar: float = DEFAULT_ACCOUNT_BALANCE_ZAR,
        max_var_fraction: float = DEFAULT_MAX_VAR_FRACTION,
    ) -> None:
        self.db = db or get_state_db()
        self.rolling_window = int(rolling_window)
        self.account_balance_zar = float(account_balance_zar)
        self.max_var_fraction = float(max_var_fraction)

    @staticmethod
    def _returns_from_prices(prices: Iterable[float]) -> np.ndarray:
        arr = np.asarray(list(prices), dtype=float)
        arr = arr[np.isfinite(arr) & (arr > 0)]
        if len(arr) < 2:
            return np.empty((0,), dtype=float)
        returns = np.diff(arr) / arr[:-1]
        return returns[np.isfinite(returns)]

    def _load_symbol_returns(self, symbol: str) -> Dict[str, Any]:
        observations = self.db.get_price_series(symbol, limit=self.rolling_window + 1)
        prices = [row["price"] for row in observations]
        returns = self._returns_from_prices(prices)
        source = observations[-1]["source"] if observations else "unavailable"

        return {
            "returns": returns,
            "observations": len(observations),
            "source": source,
        }

    def calculate_symbol_var(
        self,
        symbol: str,
        account_balance_zar: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Return 95%/99% parametric VaR boundaries for one symbol."""
        started = time.perf_counter_ns()
        symbol = symbol.upper()
        balance = float(account_balance_zar or self.account_balance_zar)
        warnings: List[str] = []

        loaded = self._load_symbol_returns(symbol)
        returns = loaded["returns"]
        sample_count = int(len(returns))

        if sample_count < 20:
            warnings.append(
                "Insufficient return history for enforced VaR; governance falls back to immutable strategy gates."
            )
            std = 0.0
            status = "insufficient_data"
        else:
            std = float(pd.Series(returns).tail(self.rolling_window).std(ddof=1))
            if not np.isfinite(std):
                std = 0.0
                warnings.append("Rolling standard deviation was non-finite; VaR set to zero for this cycle.")
            status = "active"

        var_95 = balance * Z_SCORE_95 * std
        var_99 = balance * Z_SCORE_99 * std
        var_99_fraction = (var_99 / balance) if balance > 0 else 0.0

        # Only enforce once a complete 500-candle window exists. Partial history
        # is visible to analysts but never blocks a strategy unfairly.
        enforced = sample_count >= self.rolling_window
        exceeds = bool(enforced and var_99_fraction > self.max_var_fraction)
        if exceeds:
            warnings.append("99% VaR exceeds the configured portfolio variance allowance.")

        return {
            "symbol": symbol,
            "rolling_window": self.rolling_window,
            "sample_count": sample_count,
            "observations": int(loaded["observations"]),
            "data_lineage_source": loaded["source"],
            "account_balance_zar": round(balance, 2),
            "daily_return_std": round(std, 8),
            "var_95_zar": round(float(var_95), 2),
            "var_99_zar": round(float(var_99), 2),
            "var_99_fraction": round(float(var_99_fraction), 6),
            "max_var_fraction": round(float(self.max_var_fraction), 6),
            "enforced": enforced,
            "exceeds_max_variance": exceeds,
            "status": status,
            "warnings": warnings,
            "duration_ns": time.perf_counter_ns() - started,
        }

    def calculate_matrix(
        self,
        symbols: Optional[Iterable[str]] = None,
        account_balance_zar: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Return a portfolio-wide VaR matrix for the active watchlist."""
        started = time.perf_counter_ns()
        symbols = list(symbols or settings.WATCHLIST)
        rows = [self.calculate_symbol_var(sym, account_balance_zar) for sym in symbols]
        active_rows = [row for row in rows if row["status"] == "active"]
        max_var_99 = max((row["var_99_zar"] for row in rows), default=0.0)
        breached = [row["symbol"] for row in rows if row["exceeds_max_variance"]]

        return {
            "symbols": rows,
            "portfolio": {
                "active_symbol_count": len(active_rows),
                "breached_symbols": breached,
                "max_symbol_var_99_zar": round(float(max_var_99), 2),
                "max_var_fraction": round(float(self.max_var_fraction), 6),
                "rolling_window": self.rolling_window,
            },
            "duration_ns": time.perf_counter_ns() - started,
        }


def get_portfolio_risk_engine() -> PortfolioRiskEngine:
    return PortfolioRiskEngine()
