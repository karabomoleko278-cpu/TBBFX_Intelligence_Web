import pytest
import numpy as np
import sys
import os

# Add parent directory to path so core modules can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.math_greeks import bs_greeks, calculate_gex_profile, svi_variance, calibrate_svi, calibrate_heston
from core.stream_processor import StreamProcessor
from core.ml_optimizer import ExecutionOptimizer

def test_bs_greeks():
    # Test Black-Scholes Greeks at typical levels
    S = 100.0
    K = 100.0
    T = 0.1
    r = 0.05
    sigma = 0.2
    
    greeks_call = bs_greeks(S, K, T, r, sigma, "call")
    greeks_put = bs_greeks(S, K, T, r, sigma, "put")
    
    assert greeks_call["price"] > 0
    assert greeks_put["price"] > 0
    assert 0.0 < greeks_call["delta"] < 1.0
    assert -1.0 < greeks_put["delta"] < 0.0
    assert greeks_call["gamma"] == greeks_put["gamma"]
    assert greeks_call["gamma"] > 0
    assert greeks_call["vanna"] != 0
    assert greeks_call["charm"] != 0

def test_gex_calculation():
    # Test GEX and Zero Gamma Level
    spot = 100.0
    option_chains = [
        {"strike": 95.0, "type": "call", "open_interest": 100.0, "implied_volatility": 0.20, "dte": 10.0},
        {"strike": 100.0, "type": "call", "open_interest": 500.0, "implied_volatility": 0.20, "dte": 10.0},
        {"strike": 105.0, "type": "call", "open_interest": 100.0, "implied_volatility": 0.20, "dte": 10.0},
        {"strike": 95.0, "type": "put", "open_interest": 200.0, "implied_volatility": 0.22, "dte": 10.0},
        {"strike": 100.0, "type": "put", "open_interest": 400.0, "implied_volatility": 0.22, "dte": 10.0},
    ]
    
    strike_gex, flip_level = calculate_gex_profile(spot, option_chains, r=0.05)
    
    assert len(strike_gex) == 3
    assert 90.0 < flip_level < 110.0
    assert isinstance(flip_level, float)

def test_svi_calibration():
    # Generate mock smile data
    forward = 100.0
    T = 0.1
    strikes = np.array([90.0, 95.0, 100.0, 105.0, 110.0])
    implied_vols = np.array([0.25, 0.22, 0.20, 0.21, 0.24])
    
    res = calibrate_svi(strikes, implied_vols, forward, T)
    
    assert res["success"] is True
    assert "a" in res["params"]
    assert "b" in res["params"]
    assert res["params"]["b"] >= 0
    assert abs(res["params"]["rho"]) < 1.0
    assert res["params"]["sigma"] > 0
    # Check Lee moment bound condition
    assert res["params"]["b"] * (1 + abs(res["params"]["rho"])) <= 4.0

def test_heston_calibration():
    # Test Heston pricing and calibration with mock prices
    S0 = 100.0
    T = 0.1
    r = 0.05
    strikes = np.array([95.0, 100.0, 105.0])
    # Call prices under typical volatility parameters
    market_prices = np.array([6.50, 2.90, 0.95])
    
    res = calibrate_heston(strikes, market_prices, S0, T, r, option_type="call")
    
    assert "kappa" in res["params"]
    assert "theta" in res["params"]
    assert "xi" in res["params"]
    # Check Feller condition: 2 * kappa * theta > xi^2
    kappa = res["params"]["kappa"]
    theta = res["params"]["theta"]
    xi = res["params"]["xi"]
    assert 2.0 * kappa * theta >= xi**2

def test_stream_processor():
    sp = StreamProcessor(symbol="EURUSD")
    
    # Test Microprice calculation
    micro = sp.calculate_microprice(bid_price=1.08500, bid_vol=10.0, ask_price=1.08510, ask_vol=30.0)
    # Since ask volume is 3x larger, microprice should pull closer to bid price
    assert 1.08500 < micro < 1.08505
    
    # Test OBI calculation
    obi = sp.calculate_obi(bid_depth=[10, 5, 2], ask_depth=[20, 15, 10])
    assert obi < 0 # More ask volume means negative OBI
    
    # Test Tick processing state updates
    sp.process_tick(
        bid=1.08500,
        ask=1.08510,
        bid_vol=10.0,
        ask_vol=10.0,
        trade_price=1.08510,
        trade_vol=5.0,
        side="buy"
    )
    assert sp.cvd == 5.0
    assert 1.08510 in sp.footprint
    assert sp.footprint[1.08510]["ask_volume"] == 5.0

def test_ml_optimizer():
    opt = ExecutionOptimizer()
    
    # Generate mock features (5 features)
    # GEX, Vanna, CVD, OBI, Skew
    np.random.seed(42)
    num_steps = 1000
    features = np.random.randn(num_steps, 5)
    returns = np.random.randn(num_steps) * 0.001
    
    # Create timestamps (1 second apart)
    timestamps = np.arange(num_steps) * 1.0
    
    # Test custom loss function returns a number
    params = [0.35, 0.10, 0.30, 0.20, 0.05, 0.5]
    loss = opt.custom_loss_function(np.array(params), features, returns, timestamps)
    assert isinstance(loss, float)
    
    # Run mock calibration
    res = opt.optimize_parameters(features, returns, timestamps)
    assert "weights" in res
    assert len(res["weights"]) == 5
    assert "threshold" in res
