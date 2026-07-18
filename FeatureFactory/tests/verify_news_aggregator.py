"""Offline checks for macro relevance tagging and surprise-delta contracts."""

from core.macro_intelligence import compute_surprise_delta
from core.news_aggregator import classify_headline


def main() -> None:
    shock = classify_headline("Strait of Hormuz disruption drives gold safe haven demand")
    assert "XAUUSD" in shock["symbols"]
    assert shock["severity"] == "critical"

    central_bank = classify_headline("FOMC rate cut outlook shifts US Treasury yields")
    assert {"US30", "USTEC"}.issubset(set(central_bank["symbols"]))

    payrolls = classify_headline("NFP payrolls surprise lifts the DXY")
    assert {"EURUSD", "GBPUSD", "USDJPY"}.issubset(set(payrolls["symbols"]))

    surprise = compute_surprise_delta({"actual": "3.2%", "consensus": "2.9%"})
    assert surprise["surprise_delta"] == "+0.30%"
    assert surprise["surprise_direction"] == "positive"

    print("NEWS AGGREGATOR CHECKS PASSED")


if __name__ == "__main__":
    main()
