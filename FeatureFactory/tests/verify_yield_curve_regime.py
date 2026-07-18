"""Deterministic checks for the read-only yield-curve and macro-regime layer."""

from __future__ import annotations

from core.regime_handshake import MacroRegimeHandshake
from core.yield_curve_engine import YieldCurveEngine


def _row(date: str, value: float) -> dict[str, object]:
    return {"date": date, "value": value}


def _inverted_curve_fetcher(series_id: str) -> list[dict[str, object]]:
    fixtures = {
        "DGS2": [_row("2026-07-13", 4.80), _row("2026-07-14", 4.90)],
        "DGS10": [_row("2026-07-13", 4.20), _row("2026-07-14", 4.30)],
        "T10Y2Y": [_row("2026-07-13", -0.60), _row("2026-07-14", -0.60)],
    }
    return fixtures[series_id]


def main() -> None:
    curve = YieldCurveEngine(series_fetcher=_inverted_curve_fetcher).snapshot()
    assert curve["status"] == "available"
    assert curve["yield_curve_inverted"] is True
    assert curve["calculated_slope_bps"] == -60.0
    assert curve["safe_haven_signal"]["active"] is True
    assert curve["execution_mutation_allowed"] is False
    assert curve["read_only"] is True

    regime = MacroRegimeHandshake().evaluate(
        sentiment={
            "status": "available",
            "symbols": [{"symbol": "XAUUSD", "weighted_sentiment_score": -0.10}],
            "last_updated": "2026-07-15T08:00:00+00:00",
            "source_frequency": "NEWS_INTRADAY",
        },
        cot_positioning={
            "status": "available",
            "positions": [{"symbol": "XAUUSD", "status": "available"}],
            "last_updated": "2026-07-11T19:30:00+00:00",
            "source_frequency": "CFTC_WEEKLY",
        },
        liquidity={
            "status": "available",
            "liquidity_momentum_direction": "EXPANDING",
            "last_updated": "2026-07-15T07:30:00+00:00",
            "source_frequency": "FRED_WEEKLY_MIXED",
        },
        yield_curve=curve,
        yield_spreads={
            "status": "available",
            "spreads": [],
            "last_updated": "2026-07-15T08:00:00+00:00",
            "source_frequency": "FRED_DAILY_MIXED",
        },
        news_items=[],
    )
    assert regime["state"] == "SYSTEMIC_STRESS"
    assert regime["advisory_risk_scalar"] == 0.50
    assert regime["automatic_application"] is False
    assert regime["read_only"] is True
    assert regime["strategy_guardrails"]["risk_allocations_mutable"] is False
    assert regime["strategy_guardrails"]["neural_engine_mutable"] is False
    assert regime["strategy_guardrails"]["target_r_mutable"] is False
    assert regime["strategy_guardrails"]["trade_frequency_mutable"] is False
    assert regime["strategy_guardrails"]["structural_stops_mutable"] is False

    risk_off = MacroRegimeHandshake().evaluate(
        sentiment={"status": "available", "symbols": []},
        cot_positioning={"status": "available", "positions": []},
        liquidity={"status": "available", "liquidity_momentum_direction": "CONTRACTING"},
        yield_curve={"status": "available", "yield_curve_inverted": False},
        yield_spreads={"status": "available", "spreads": []},
        news_items=[],
    )
    assert risk_off["state"] == "RISK_OFF"
    assert risk_off["automatic_application"] is False

    print("YIELD CURVE AND MACRO REGIME CHECKS PASSED")


if __name__ == "__main__":
    main()
