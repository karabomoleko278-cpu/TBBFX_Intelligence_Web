"""Tests for the Composite Market Momentum scorer + the optimizer search fix."""
import numpy as np

from core.momentum import MomentumScorer, label_for
from core.ml_optimizer import ExecutionOptimizer


def test_label_bands():
    assert label_for(75) == "BULLISH"
    assert label_for(57) == "NEUTRAL"
    assert label_for(20) == "BEARISH"


def test_no_signals_is_neutral():
    s = MomentumScorer().score("EURUSD")
    assert s["score"] == 50.0
    assert s["label"] == "NEUTRAL"


def test_bullish_obi_pushes_score_up():
    s = MomentumScorer().score("EURUSD", obi=0.9)
    assert s["score"] > 60
    assert s["label"] == "BULLISH"
    assert s["weights"] == {"obi": 1.0}  # only live component -> renormalised


def test_bearish_blend():
    sc = MomentumScorer()
    # strong sell-side OBI + spot well below gamma flip in a NEGATIVE regime
    gex = {"spot": 100.0, "gamma_flip": 110.0, "regime": "NEGATIVE"}
    s = sc.score("US30", obi=-0.8, cvd=0.0, gex_snapshot=gex)
    assert s["score"] < 40
    assert s["label"] == "BEARISH"
    # all three components present
    assert set(s["weights"]) == {"gex", "cvd", "obi"}


def test_cvd_builds_baseline_then_reacts():
    sc = MomentumScorer()
    first = sc.score("XAUUSD", cvd=1000.0)      # establishes baseline -> neutral
    assert abs(first["components"]["cvd"]) < 1e-9
    rising = sc.score("XAUUSD", cvd=5000.0)     # big jump above baseline -> bullish cvd
    assert rising["components"]["cvd"] > 0


def test_score_bounded_0_100():
    s = MomentumScorer().score("GBPUSD", obi=5.0)  # out-of-range obi is clamped
    assert 0.0 <= s["score"] <= 100.0
    assert s["components"]["obi"] == 1.0


def test_optimizer_actually_searches():
    """Regression: the old L-BFGS-B solver stalled on the discontinuous loss and
    returned the initial weights. Differential evolution must move off them."""
    rng = np.random.default_rng(0)
    n = 600
    # Feature 2 (CVD) genuinely predicts the next return; others are noise.
    features = rng.normal(size=(n, 5))
    returns = 0.01 * features[:, 2] + 0.002 * rng.normal(size=n)
    timestamps = np.arange(n, dtype=float) * 3600.0  # hourly over ~25 days

    opt = ExecutionOptimizer()
    init = np.array([0.35, 0.10, 0.30, 0.20, 0.05])
    res = opt.optimize_parameters(features, returns, timestamps)

    found = np.array(res["weights"])
    # It must have meaningfully left the initial guess (the bug symptom).
    assert np.linalg.norm(found - init) > 0.05
    assert res["num_trades"] > 0
