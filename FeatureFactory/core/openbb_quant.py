"""OpenBB-backed quantitative feature helpers with safe local fallbacks."""

from __future__ import annotations

import math
import time
from typing import Any, Dict, Iterable, List

import numpy as np
import pandas as pd

from core.tbbfx_object import TbbFxObject, make_tbbfx_object

_OPENBB_IMPORT_WARNINGS: List[str] = []

try:  # These packages are optional in local dev; calculations degrade safely.
    import openbb_technical  # type: ignore  # noqa: F401
except Exception as exc:  # noqa: BLE001
    _OPENBB_IMPORT_WARNINGS.append(f"openbb-technical unavailable: {type(exc).__name__}: {exc}")

try:
    import openbb_quantitative  # type: ignore  # noqa: F401
except Exception as exc:  # noqa: BLE001
    _OPENBB_IMPORT_WARNINGS.append(f"openbb-quantitative unavailable: {type(exc).__name__}: {exc}")


def _extract_closes(rows_or_values: Any) -> pd.Series:
    if rows_or_values is None:
        return pd.Series(dtype="float64")

    if isinstance(rows_or_values, pd.DataFrame):
        if "close" in rows_or_values.columns:
            return pd.to_numeric(rows_or_values["close"], errors="coerce").dropna()
        return pd.to_numeric(rows_or_values.iloc[:, -1], errors="coerce").dropna()

    if isinstance(rows_or_values, Iterable) and not isinstance(rows_or_values, (str, bytes, dict)):
        values = list(rows_or_values)
        if not values:
            return pd.Series(dtype="float64")
        if isinstance(values[0], dict):
            return pd.Series(
                pd.to_numeric([v.get("close", v.get("price", v.get("value"))) for v in values], errors="coerce")
            ).dropna()
        return pd.Series(pd.to_numeric(values, errors="coerce")).dropna()

    return pd.Series(dtype="float64")


def _hurst_exponent(values: np.ndarray) -> float:
    if len(values) < 32:
        return 0.5
    lags = range(2, min(20, len(values) // 2))
    tau = [np.std(np.subtract(values[lag:], values[:-lag])) for lag in lags]
    tau = [x for x in tau if x > 0]
    if len(tau) < 2:
        return 0.5
    poly = np.polyfit(np.log(list(lags)[: len(tau)]), np.log(tau), 1)
    return float(max(0.0, min(1.0, poly[0] * 2.0)))


def calculate_quant_feature_pack(
    rows_or_values: Any,
    *,
    symbol: str,
    route: str = "openbb_quant_feature_pack",
) -> TbbFxObject:
    """Return rolling quant/technical features in a TbbFxObject envelope."""
    start_ns = time.perf_counter_ns()
    warnings = list(_OPENBB_IMPORT_WARNINGS)
    closes = _extract_closes(rows_or_values)
    if len(closes) < 8:
        return make_tbbfx_object(
            {
                "symbol": symbol,
                "sample_count": int(len(closes)),
                "status": "insufficient_data",
                "features": {},
            },
            provider="openbb_optional_with_local_fallback",
            route=route,
            warnings=warnings + ["Need at least 8 close values for quantitative feature pack."],
            start_ns=start_ns,
        )

    returns = closes.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    rolling = returns.rolling(window=min(20, max(5, len(returns) // 3)))
    downside = returns.where(returns < 0, 0.0)
    downside_std = downside.std() or np.nan
    stdev = returns.std() or np.nan
    mean_return = returns.mean() if len(returns) else 0.0

    sharpe = float((mean_return / stdev) * math.sqrt(252)) if stdev and not np.isnan(stdev) else 0.0
    sortino = float((mean_return / downside_std) * math.sqrt(252)) if downside_std and not np.isnan(downside_std) else 0.0
    feature_pack: Dict[str, Any] = {
        "symbol": symbol,
        "sample_count": int(len(closes)),
        "status": "ok",
        "features": {
            "rolling_variance": float(rolling.var().iloc[-1]) if len(returns) else 0.0,
            "rolling_sharpe": sharpe,
            "rolling_sortino": sortino,
            "skewness": float(returns.skew()) if len(returns) > 2 else 0.0,
            "kurtosis": float(returns.kurtosis()) if len(returns) > 3 else 0.0,
            "hurst_exponent": _hurst_exponent(closes.to_numpy(dtype=float)),
            "last_close": float(closes.iloc[-1]),
        },
    }

    return make_tbbfx_object(
        feature_pack,
        provider="openbb_optional_with_local_fallback",
        route=route,
        warnings=warnings,
        start_ns=start_ns,
    )
