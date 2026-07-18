"""Read-only macro-regime handshake for the TBBFX intelligence workspace.

The handshake consolidates analytical context into a deterministic display
state.  It cannot write risk tiers, strategy parameters, stops, or orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional


class MacroRegimeHandshake:
    """Synthesize macro feeds into RISK_ON, RISK_OFF, or SYSTEMIC_STRESS."""

    _IMMUTABLE_GUARDRAILS = {
        "risk_allocations_mutable": False,
        "target_r_mutable": False,
        "trade_frequency_mutable": False,
        "structural_stops_mutable": False,
        "neural_engine_mutable": False,
    }

    def evaluate(
        self,
        sentiment: Optional[Dict[str, Any]] = None,
        cot_positioning: Optional[Dict[str, Any]] = None,
        liquidity: Optional[Dict[str, Any]] = None,
        yield_curve: Optional[Dict[str, Any]] = None,
        yield_spreads: Optional[Dict[str, Any]] = None,
        news_items: Optional[Iterable[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        sentiment = sentiment or {}
        cot_positioning = cot_positioning or {}
        liquidity = liquidity or {}
        yield_curve = yield_curve or {}
        yield_spreads = yield_spreads or {}
        stories = list(news_items or [])

        reasons: List[str] = []
        warnings: List[str] = []
        critical_geopolitical = any(
            str(item.get("severity", "")).lower() == "critical"
            or str(item.get("category", "")).lower() in ("geopolitical_shock", "systemic_stress")
            for item in stories
        )
        inverted = yield_curve.get("yield_curve_inverted") is True
        liquidity_direction = str(
            liquidity.get("liquidity_momentum_direction", "UNAVAILABLE")
        ).upper()

        sentiment_values = []
        for item in sentiment.get("symbols", []) or []:
            try:
                sentiment_values.append(float(item.get("weighted_sentiment_score", 0.0)))
            except (TypeError, ValueError):
                continue
        average_sentiment = (
            sum(sentiment_values) / float(len(sentiment_values)) if sentiment_values else 0.0
        )
        contracting_spreads = sum(
            1
            for item in (yield_spreads.get("spreads", []) or [])
            if str(item.get("delta_state", "")).lower() == "contracting"
        )

        if inverted:
            reasons.append("US 10Y-2Y yield curve is inverted.")
        if critical_geopolitical:
            reasons.append("Critical geopolitical or systemic headline is active.")
        if liquidity_direction == "CONTRACTING":
            reasons.append("USD net-liquidity momentum is contracting.")
        if average_sentiment < -0.30:
            reasons.append("Cross-watchlist weighted sentiment is risk-off.")
        if contracting_spreads >= 2:
            reasons.append("Multiple sovereign spread matrices are contracting.")

        if inverted or critical_geopolitical:
            state = "SYSTEMIC_STRESS"
            advisory_scalar = 0.50
        elif liquidity_direction == "CONTRACTING" or average_sentiment < -0.30 or contracting_spreads >= 2:
            state = "RISK_OFF"
            advisory_scalar = 0.75
        else:
            state = "RISK_ON"
            advisory_scalar = 1.00
            if liquidity_direction == "EXPANDING":
                reasons.append("USD net-liquidity momentum is expanding.")
            if average_sentiment >= -0.30:
                reasons.append("Cross-watchlist sentiment is neutral-to-positive.")
            if not inverted:
                reasons.append("US 10Y-2Y yield curve is not inverted.")

        cot_available = any(
            item.get("status") == "available"
            for item in (cot_positioning.get("positions", []) or [])
        )
        if not cot_available:
            warnings.append("CFTC positioning is unavailable or incomplete; regime state remains advisory.")

        source_freshness = {
            "sentiment": self._freshness(sentiment, "NEWS_INTRADAY"),
            "cftc_positioning": self._freshness(cot_positioning, "CFTC_WEEKLY"),
            "usd_liquidity": self._freshness(liquidity, "FRED_WEEKLY_MIXED"),
            "yield_curve": self._freshness(yield_curve, "FRED_DAILY_TREASURY"),
            "yield_spreads": self._freshness(yield_spreads, "FRED_DAILY_MIXED"),
        }
        generated_at = datetime.now(timezone.utc).isoformat()
        return {
            "status": "available",
            "state": state,
            "label": state.replace("_", " "),
            "confidence": self._confidence(source_freshness),
            "average_sentiment": round(average_sentiment, 4),
            "critical_geopolitical_active": critical_geopolitical,
            "yield_curve_inverted": inverted,
            "liquidity_direction": liquidity_direction,
            "contracting_spread_count": contracting_spreads,
            "reasons": reasons,
            "warnings": warnings,
            "advisory_risk_scalar": advisory_scalar,
            "advisory_note": (
                "Display-only regime guidance. No automatic lot, risk, target, stop, frequency, or neural-engine mutation is authorized."
            ),
            "automatic_application": False,
            "read_only": True,
            "advisory_only": True,
            "execution_mutation_allowed": False,
            "strategy_guardrails": dict(self._IMMUTABLE_GUARDRAILS),
            "source_freshness": source_freshness,
            "provider": "tbbfx_read_only_macro_handshake",
            "source_frequency": "MIXED_FREQUENCY",
            "refresh_cadence_seconds": 300,
            "last_updated": generated_at,
            "generated_at": generated_at,
        }

    @staticmethod
    def _freshness(payload: Dict[str, Any], default_frequency: str) -> Dict[str, Any]:
        return {
            "status": payload.get("status", "available" if payload else "unavailable"),
            "provider": payload.get("provider", "unknown"),
            "last_updated": payload.get("last_updated") or payload.get("as_of") or payload.get("generated_at"),
            "source_frequency": payload.get("source_frequency", default_frequency),
            "refresh_cadence_seconds": payload.get("refresh_cadence_seconds"),
        }

    @staticmethod
    def _confidence(source_freshness: Dict[str, Dict[str, Any]]) -> float:
        available = sum(
            1 for item in source_freshness.values() if item.get("status") != "unavailable"
        )
        return round(available / float(max(1, len(source_freshness))), 2)


_HANDSHAKE: Optional[MacroRegimeHandshake] = None


def get_macro_regime_handshake() -> MacroRegimeHandshake:
    global _HANDSHAKE
    if _HANDSHAKE is None:
        _HANDSHAKE = MacroRegimeHandshake()
    return _HANDSHAKE
