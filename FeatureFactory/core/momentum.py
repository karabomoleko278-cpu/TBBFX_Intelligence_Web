"""
Composite Market Momentum scorer (terminal panel 2B.3 / MAUI BiasGrid).

Blends the live microstructure + options-positioning signals the platform
already produces into a single 0-100 "market momentum" reading:

    50   = balanced / no directional conviction
    > 60 = BULLISH lean      < 40 = BEARISH lean      40-60 = NEUTRAL

It deliberately reuses the ExecutionOptimizer's *own* signal weights so the
gauge is consistent with the ML core rather than an arbitrary blend:

    GEX 0.35   CVD 0.30   OBI 0.20

(Vanna 0.10 / Skew 0.05 from the optimizer are reserved until per-symbol
DEX/VEX aggregation is wired in; only the three signals we can source live
contribute today.) Only components with live data contribute; their weights
are renormalised so a missing GEX snapshot biases the score toward neutral
instead of silently dropping signal. Every component is returned in the payload
so the gauge is fully explainable (no black box).

Honest note: this is a *current-state* conviction reading derived from order
flow + dealer positioning. It is not a forecast and carries no win-rate promise.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional


# Reuse the ML optimizer's signal weights (the subset we can source live).
_RAW_WEIGHTS = {"gex": 0.35, "cvd": 0.30, "obi": 0.20}

BULLISH_BAND = 60.0
BEARISH_BAND = 40.0


def label_for(score: float) -> str:
    """Map a 0-100 score to its directional band."""
    if score > BULLISH_BAND:
        return "BULLISH"
    if score < BEARISH_BAND:
        return "BEARISH"
    return "NEUTRAL"


class MomentumScorer:
    """Stateful scorer: keeps a per-symbol CVD baseline so the cumulative,
    unbounded CVD can be turned into a bounded [-1, 1] momentum component."""

    def __init__(self, ema_alpha: float = 0.1):
        self._alpha = ema_alpha
        # per-symbol CVD baseline: {sym: {"ema": float, "mad": float}}
        self._cvd_state: Dict[str, Dict[str, float]] = {}

    # ------------------------------------------------------------------
    # Component derivations (each returns a value in [-1, 1] or None)
    # ------------------------------------------------------------------
    def _cvd_component(self, symbol: str, cvd: Optional[float]) -> Optional[float]:
        """CVD is a cumulative, unbounded signed tick sum. Convert it to a
        bounded momentum reading = how far current CVD has drifted from its
        own recent EMA baseline, scaled by its mean absolute deviation."""
        if cvd is None:
            return None
        st = self._cvd_state.get(symbol)
        if st is None:
            # First reading establishes the baseline; no momentum yet (neutral).
            self._cvd_state[symbol] = {"ema": float(cvd), "mad": 0.0}
            return 0.0
        dev = cvd - st["ema"]
        scale = st["mad"] if st["mad"] > 1e-9 else max(abs(dev), 1.0)
        comp = math.tanh(dev / scale)
        # Update the rolling baseline + mean absolute deviation.
        st["ema"] = (1.0 - self._alpha) * st["ema"] + self._alpha * cvd
        st["mad"] = (1.0 - self._alpha) * st["mad"] + self._alpha * abs(dev)
        return float(comp)

    @staticmethod
    def _gex_component(gex: Optional[Dict[str, Any]]) -> Optional[float]:
        """Directional lean from spot vs the gamma-flip level + regime.

        Spot above the gamma flip -> upward magnet (bullish); below -> bearish.
        Damped in a POSITIVE-gamma (mean-reverting / vol-suppressive) regime,
        where dealer hedging fights momentum.
        """
        if not gex:
            return None
        spot = gex.get("spot")
        flip = gex.get("gamma_flip")
        regime = (gex.get("regime") or "").upper()
        if not spot or not flip or spot <= 0:
            return None
        rel = (spot - flip) / spot          # signed relative distance
        lean = math.tanh(rel * 8.0)         # squashed into [-1, 1]
        if regime == "POSITIVE":            # dealers suppress vol -> halve lean
            lean *= 0.5
        return float(lean)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def score(
        self,
        symbol: str,
        obi: Optional[float] = None,
        cvd: Optional[float] = None,
        gex_snapshot: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        symbol = symbol.upper()
        comps: Dict[str, Optional[float]] = {
            "gex": self._gex_component(gex_snapshot),
            "cvd": self._cvd_component(symbol, cvd),
            "obi": None if obi is None else float(max(-1.0, min(1.0, obi))),
        }
        present = {k: v for k, v in comps.items() if v is not None}

        if not present:
            return {
                "symbol": symbol,
                "score": 50.0,
                "label": "NEUTRAL",
                "components": comps,
                "weights": {},
                "data_quality": "no live signals available; defaulting to neutral",
                "timestamp": time.time(),
            }

        wsum = sum(_RAW_WEIGHTS[k] for k in present)
        weights = {k: _RAW_WEIGHTS[k] / wsum for k in present}
        weighted = sum(weights[k] * present[k] for k in present)  # in [-1, 1]
        score = max(0.0, min(100.0, 50.0 + 50.0 * weighted))

        missing = [k for k, v in comps.items() if v is None]
        dq = "all signals live" if not missing else f"missing/neutral: {', '.join(missing)}"
        return {
            "symbol": symbol,
            "score": round(score, 1),
            "label": label_for(score),
            "components": comps,
            "contributions": {k: round(weights[k] * present[k], 4) for k in present},
            "weights": {k: round(weights[k], 4) for k in present},
            "data_quality": dq,
            "timestamp": time.time(),
        }
