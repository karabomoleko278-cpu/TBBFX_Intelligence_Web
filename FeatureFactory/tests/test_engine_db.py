import os
import sys
import tempfile
import asyncio

import numpy as np
import pytest

# Add parent directory to path so core / google_antigravity packages import.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.options_exposure_engine import OptionsExposureEngine
from core.state_db import StateDatabase
from core.governance_agent import TradingRiskGatekeeper
from google_antigravity.sdk.models import ToolCall, TurnContext


# Synthetic chain straddling spot=100 (no network needed).
SPOT = 100.0
CHAIN = [
    {"strike": 95.0, "type": "call", "open_interest": 100.0, "implied_volatility": 0.20, "dte": 10.0},
    {"strike": 100.0, "type": "call", "open_interest": 500.0, "implied_volatility": 0.20, "dte": 10.0},
    {"strike": 105.0, "type": "call", "open_interest": 100.0, "implied_volatility": 0.20, "dte": 10.0},
    {"strike": 95.0, "type": "put", "open_interest": 200.0, "implied_volatility": 0.22, "dte": 10.0},
    {"strike": 100.0, "type": "put", "open_interest": 400.0, "implied_volatility": 0.22, "dte": 10.0},
]


def test_net_gex_per_strike():
    engine = OptionsExposureEngine(db=None)
    per_strike = engine.compute_net_gex_per_strike(SPOT, CHAIN)
    assert set(per_strike.keys()) == {95.0, 100.0, 105.0}
    # Net GEX should be a finite float
    assert np.isfinite(sum(per_strike.values()))


def test_gamma_flip_scanner():
    engine = OptionsExposureEngine(db=None)
    flip = engine.detect_gamma_flip(SPOT, CHAIN)
    assert flip is not None
    assert 80.0 < flip < 120.0
    # At the flip, net GEX must be ~0 (root found).
    assert abs(engine._net_gex_at(flip, CHAIN)) < 1.0


def test_state_database_roundtrip():
    path = os.path.join(tempfile.gettempdir(), "tbbfx_test_roundtrip.db")
    if os.path.exists(path):
        os.remove(path)
    db = StateDatabase(path)
    try:
        db.save_svi_parameters(
            "SPY", {"a": 0.04, "b": 0.1, "rho": -0.3, "m": 0.0, "sigma": 0.1},
            dte=7, forward=500.0, arb_free=True, success=True,
        )
        db.save_gex_snapshot("SPY", spot=500.0, net_gex=1.2e9, gamma_flip=498.5, regime="POSITIVE")
        for i in range(3):
            db.insert_training_sample("SPY", gex=0.5, vanna=0.1, cvd=120.0, obi=0.3, skew=-0.2,
                                      fwd_return=0.001 * i, ts=1000.0 + i)

        latest = db.get_latest_svi_parameters("SPY")
        assert latest is not None and latest["b"] == pytest.approx(0.1)

        feats, rets, tss = db.load_training_matrix("SPY")
        assert feats.shape == (3, 5)
        assert rets.shape == (3,)
        assert tss.shape == (3,)
        assert db.training_sample_count("SPY") == 3
    finally:
        db.close()


def test_risk_gateway_decide_blocks_unauthorized_symbol():
    gk = TradingRiskGatekeeper()
    tc = ToolCall("submit_execution_order", {"symbol": "FAKE", "quantity": 100})
    decision = asyncio.run(gk.decide(TurnContext(), tc))
    assert not decision.allowed


# Use a real, currently-sanctioned symbol so these stay valid as the
# watchlist evolves (the first configured instrument).
from core.config import settings as _settings
VALID_SYMBOL = _settings.WATCHLIST[0]  # e.g. "XAUUSD"


def test_risk_gateway_decide_blocks_oversized():
    gk = TradingRiskGatekeeper()
    tc = ToolCall("submit_execution_order", {"symbol": VALID_SYMBOL, "quantity": 10_000_000})
    decision = asyncio.run(gk.decide(TurnContext(), tc))
    assert not decision.allowed


def test_risk_gateway_decide_allows_valid():
    gk = TradingRiskGatekeeper()
    tc = ToolCall("submit_execution_order", {"symbol": VALID_SYMBOL, "quantity": 500})
    decision = asyncio.run(gk.decide(TurnContext(), tc))
    assert decision.allowed


def test_risk_gateway_decide_allows_broker_suffix():
    # Broker-suffixed form (e.g. "XAUUSDm") must also be authorized.
    gk = TradingRiskGatekeeper()
    tc = ToolCall("submit_execution_order",
                  {"symbol": VALID_SYMBOL + _settings.MT5_SYMBOL_SUFFIX, "quantity": 500})
    decision = asyncio.run(gk.decide(TurnContext(), tc))
    assert decision.allowed


def test_risk_gateway_transform_clamps_size():
    gk = TradingRiskGatekeeper()
    tc = ToolCall("submit_execution_order", {"symbol": VALID_SYMBOL, "quantity": 2500})
    out = asyncio.run(gk.transform(TurnContext(), tc))
    # Default RISK_MAX_ORDER_SIZE is 1000 -> clamped.
    assert out.arguments["quantity"] == 1000


def test_quote_only_orderflow_proxy():
    """CVD/OBI must respond to quote pressure on a volume-less feed (retail FX)."""
    from core.stream_processor import StreamProcessor
    sp = StreamProcessor(symbol="XAUUSD", mt5_symbol="XAUUSDm")

    # Feed a steadily-rising mid (buyer-initiated upticks, no volume field).
    base = 4500.0
    for i in range(20):
        mid = base + i * 0.10
        sp.process_quote(bid=mid - 0.15, ask=mid + 0.15, tick_volume=0.0)

    # Rising market => positive cumulative tick delta and positive imbalance.
    assert sp.cvd > 0, "CVD should accumulate positive on a rising series"
    assert sp.obi > 0, "OBI proxy should be positive when mid is drifting up"
    assert sp.microprice > base, "microprice should track the rising mid"
    assert len(sp.footprint) > 0

    # Now feed a falling series and confirm CVD turns down.
    cvd_peak = sp.cvd
    for i in range(20):
        mid = base + 2.0 - i * 0.10
        sp.process_quote(bid=mid - 0.15, ask=mid + 0.15, tick_volume=0.0)
    assert sp.cvd < cvd_peak, "CVD should fall back as the series declines"
