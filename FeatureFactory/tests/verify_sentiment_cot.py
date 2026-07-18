"""Fast offline contract checks for the read-only sentiment and COT analytics."""

from __future__ import annotations

from core.cot_positioning import CotPositioningEngine
from core.sentiment_engine import TbbFxSentimentEngine


def main() -> None:
    sentiment = TbbFxSentimentEngine()
    assert sentiment.analyze_text("Safe haven demand supports a bullish gold rally") > 0.30
    assert sentiment.analyze_text("Geopolitical escalation triggers panic and supply disruption") < -0.30
    summary = sentiment.weighted_sentiment(
        [
            {"title": "Safe haven demand supports bullion", "severity": "high", "symbols": ["XAUUSD"], "provider": "test"},
            {"title": "FOMC rate cut boosts tech recovery", "severity": "medium", "symbols": ["USTEC", "US30"], "provider": "test"},
        ]
    )
    assert next(item for item in summary["symbols"] if item["symbol"] == "XAUUSD")["stance"] == "bullish"

    rows = [{
        "symbol": "XAUUSD", "contract": "Gold / COMEX", "venue": "COMEX", "cftc_code": "088691",
        "noncommercial_long": 320000, "noncommercial_short": 100000,
        "commercial_long": 150000, "commercial_short": 320000,
        "provider": "fixture", "source": "fixture", "timestamp": "2026-07-13T00:00:00+00:00",
    }]
    cot = CotPositioningEngine(source_fetcher=lambda: rows, history_provider=lambda _symbol: list(range(-200000, 200000, 8000)))
    snapshot = cot.snapshot("XAUUSD")
    position = snapshot["positions"][0]
    assert position["status"] == "available"
    assert position["net_speculative_contracts"] == 220000
    assert 0 <= position["percentile_skew_52w"] <= 100
    print("ALL SENTIMENT/COT CHECKS PASSED")


if __name__ == "__main__":
    main()
