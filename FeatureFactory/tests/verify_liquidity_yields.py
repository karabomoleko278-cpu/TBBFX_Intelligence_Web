"""Offline verification for read-only liquidity and sovereign spread engines."""

from __future__ import annotations

from core.liquidity_engine import FedNetLiquidityEngine
from core.yield_spread_engine import YieldSpreadEngine


def _rows(values):
    return [
        {"observation_date": "2026-01-%02d" % (index + 1), "value": value}
        for index, value in enumerate(values)
    ]


def _liquidity_fetcher(series_id):
    values = {
        "WALCL": [8000000 + (index * 10000) for index in range(12)],
        "WLRRAL": [500000 - (index * 2000) for index in range(12)],
        "WDTGAL": [700000 - (index * 1000) for index in range(12)],
    }
    return _rows(values[series_id])


def _yield_fetcher(series_id):
    values = {
        "DGS10": [4.00 + (index * 0.01) for index in range(12)],
        "IRLTLT01JPM156N": [0.50 for _ in range(12)],
        "IRLTLT01DEM156N": [2.50 for _ in range(12)],
        "IRLTLT01GBM156N": [4.00 + (index * 0.005) for index in range(12)],
    }
    return _rows(values[series_id])


def main():
    liquidity = FedNetLiquidityEngine(series_fetcher=_liquidity_fetcher, cache_ttl_seconds=0).snapshot()
    assert liquidity["status"] == "available"
    assert liquidity["liquidity_momentum_direction"] == "EXPANDING"
    assert liquidity["read_only"] is True
    assert liquidity["execution_mutation_allowed"] is False
    assert liquidity["historical_weekly"]

    yield_snapshot = YieldSpreadEngine(series_fetcher=_yield_fetcher, cache_ttl_seconds=0).snapshot()
    assert yield_snapshot["status"] == "available"
    by_symbol = {row["symbol"]: row for row in yield_snapshot["spreads"]}
    assert by_symbol["USDJPY"]["delta_bps_24h"] > 0
    assert by_symbol["USDJPY"]["favors_base_asset"] is True
    assert by_symbol["EURUSD"]["favors_base_asset"] is False
    assert yield_snapshot["read_only"] is True
    assert yield_snapshot["execution_mutation_allowed"] is False

    filtered = YieldSpreadEngine(series_fetcher=_yield_fetcher, cache_ttl_seconds=0).snapshot("EURUSDm")
    assert len(filtered["spreads"]) == 1
    assert filtered["spreads"][0]["symbol"] == "EURUSD"

    print("liquidity momentum=%s" % liquidity["liquidity_momentum"])
    print("USDJPY delta_bps=%s" % by_symbol["USDJPY"]["delta_bps_24h"])
    print("ALL LIQUIDITY/YIELD CHECKS PASSED")


if __name__ == "__main__":
    main()
