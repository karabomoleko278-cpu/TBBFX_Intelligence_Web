import numpy as np
from scipy.optimize import differential_evolution
from typing import Dict, List, Tuple, Any, Optional
from core.openbb_quant import calculate_quant_feature_pack

class ExecutionOptimizer:
    """
    Uncapped signal-combination optimizer for the ML core.

    The loss function blends a risk-adjusted performance term (annualised Sharpe
    + total profit) with two regularisers requested for this project:

      * a **trade-frequency** penalty that punishes any 24h window with zero
        trades, so the optimizer cannot hide in a degenerate "never trade"
        corner and is forced to stay active (>= ~1 trade/day); and
      * a one-sided **win-rate floor** penalty that discourages *in-sample* win
        rates below 65%. We deliberately do NOT punish high win rates — an
        earlier upper-band penalty steered the search *away* from genuinely
        strong configurations, which is counterproductive.

    The search itself uses a derivative-free global optimizer (differential
    evolution). The decision rule is a hard threshold, so the loss is
    piecewise-constant in the weights; a gradient method (L-BFGS-B) sees a
    ~zero gradient and never leaves the initial guess. DE actually explores
    the weight space.

    Honest caveat: these penalties shape the optimizer's preference over the
    *historical* data it is fitted on. They do **not** guarantee a 65-68% win
    rate in live trading — realised win rate is a property of genuine edge in
    the features, not of a constraint. In fact the frequency penalty *raises*
    trade count, which generally *lowers* selectivity and win rate. Treat the
    target as a soft search region, never a profit guarantee.
    """

    def __init__(self):
        # Default weights for our signal combination
        self.weights = np.array([0.35, 0.10, 0.30, 0.20, 0.05]) # GEX, Vanna, CVD, OBI, Skew
        self.threshold = 0.5  # Entry threshold

    def custom_loss_function(
        self,
        params: np.ndarray,
        features: np.ndarray,      # shape: (num_steps, 5) -> GEX, Vanna, CVD, OBI, Skew
        returns: np.ndarray,       # shape: (num_steps,) -> future asset return
        timestamps: np.ndarray,    # shape: (num_steps,) -> Unix timestamps
    ) -> float:
        """
        Loss function to optimize strategy parameters.
        Includes constraints:
        1. Penalizes model if it takes 0 trades in any 24-hour window (frequency enforcement).
        2. Applies a one-sided floor penalty for in-sample win rate below 65%
           (no penalty for high win rates).
        """
        weights = params[:5]
        threshold = params[5]
        
        # Calculate raw signals
        signals = np.dot(features, weights)
        
        # Binary execution decisions: 1 (long trade), -1 (short trade), 0 (flat)
        decisions = np.zeros_like(signals)
        decisions[signals > threshold] = 1.0
        decisions[signals < -threshold] = -1.0
        
        # Find index changes to identify individual trades
        trade_indices = np.where(decisions != 0.0)[0]
        num_total_ticks = len(features)
        
        if len(trade_indices) == 0:
            # Maximum penalty if the model takes absolutely no trades
            return 1e6
            
        # Calculate individual trade returns
        trade_returns = returns[trade_indices] * decisions[trade_indices]
        
        # 1. Trade Frequency Enforcement (Penalize 0 trades in 24 hours)
        # Convert timestamps (seconds) to 24-hour buckets (86400 seconds)
        start_time = timestamps[0]
        end_time = timestamps[-1]
        total_duration = end_time - start_time
        num_days = max(1.0, total_duration / 86400.0)
        
        day_buckets = ((timestamps - start_time) / 86400.0).astype(int)
        trades_per_day = np.zeros(int(day_buckets.max() + 1))
        
        # Count trades per day
        for idx in trade_indices:
            bucket = day_buckets[idx]
            if bucket < len(trades_per_day):
                trades_per_day[bucket] += 1
                
        # Count days with 0 trades
        zero_trade_days = np.sum(trades_per_day == 0)
        frequency_penalty = zero_trade_days * 1500.0  # High penalty per empty day
        
        # 2. Win Rate Floor (one-sided): discourage WEAK (<65%) in-sample win
        #    rates only. We do NOT penalise high win rates — punishing accuracy
        #    steered the optimizer toward worse configurations.
        wins = np.sum(trade_returns > 0)
        total_trades = len(trade_returns)
        win_rate = wins / total_trades if total_trades > 0 else 0.0

        win_rate_penalty = 0.0
        if win_rate < 0.65:
            win_rate_penalty += (0.65 - win_rate) * 5000.0

        # 3. Strategy performance component (maximize profit & Sharpe ratio)
        avg_return = np.mean(trade_returns) if len(trade_returns) > 0 else 0.0
        std_return = np.std(trade_returns) if len(trade_returns) > 1 else 1e-4
        if std_return == 0:
            std_return = 1e-4
            
        sharpe = (avg_return / std_return) * np.sqrt(252) # Ann. Sharpe
        
        # Total profit term
        total_profit = np.sum(trade_returns)
        
        # Minimize the negative Sharpe plus penalties
        loss = -sharpe * 100.0 - total_profit * 1000.0 + frequency_penalty + win_rate_penalty
        return float(loss)

    def optimize_parameters(
        self,
        features: np.ndarray,
        returns: np.ndarray,
        timestamps: np.ndarray
    ) -> Dict[str, Any]:
        """
        Optimizes weights and threshold parameters.
        """
        # Parameter order: [w_gex, w_vanna, w_cvd, w_obi, w_skew, threshold]
        bounds = [
            (-1.0, 1.0),  # w_gex
            (-1.0, 1.0),  # w_vanna
            (-1.0, 1.0),  # w_cvd
            (-1.0, 1.0),  # w_obi
            (-1.0, 1.0),  # w_skew
            (0.1, 1.5)    # threshold
        ]

        # The decision rule thresholds the signal into {-1, 0, +1}, so the loss
        # is piecewise-constant in the weights -> a gradient solver (L-BFGS-B)
        # sees a ~zero gradient almost everywhere and never moves off the
        # initial guess. Use a derivative-free GLOBAL optimizer that genuinely
        # searches the weight space (this is the "uncapped" search intent).
        res = differential_evolution(
            self.custom_loss_function,
            bounds=bounds,
            args=(features, returns, timestamps),
            seed=42,            # reproducible runs
            maxiter=200,
            popsize=20,
            tol=1e-7,
            mutation=(0.5, 1.0),
            recombination=0.7,
            polish=True,
        )

        opt_weights = res.x[:5]
        opt_threshold = res.x[5]
        
        # Calculate optimal performance stats
        signals = np.dot(features, opt_weights)
        decisions = np.zeros_like(signals)
        decisions[signals > opt_threshold] = 1.0
        decisions[signals < -opt_threshold] = -1.0
        
        trade_indices = np.where(decisions != 0.0)[0]
        if len(trade_indices) > 0:
            trade_returns = returns[trade_indices] * decisions[trade_indices]
            wins = np.sum(trade_returns > 0)
            win_rate = wins / len(trade_returns)
            total_profit = np.sum(trade_returns)
            quant_pack = calculate_quant_feature_pack(
                trade_returns.tolist(),
                symbol="OPTIMIZER",
                route="ml_optimizer.trade_return_quant_features",
            ).to_dict()
        else:
            win_rate = 0.0
            total_profit = 0.0
            quant_pack = calculate_quant_feature_pack(
                [],
                symbol="OPTIMIZER",
                route="ml_optimizer.trade_return_quant_features",
            ).to_dict()
            
        return {
            "weights": list(opt_weights),
            "threshold": float(opt_threshold),
            "success": bool(res.success),
            "optimized_win_rate": float(win_rate),
            "optimized_profit": float(total_profit),
            "num_trades": int(len(trade_indices)),
            "quant_feature_pack": quant_pack,
            "message": res.message
        }

    def optimize_from_store(
        self,
        db,
        ticker: Optional[str] = None,
        min_samples: int = 200,
    ) -> Dict[str, Any]:
        """
        Load persisted training samples from the local StateDatabase and run the
        optimizer on real history. Falls back with a clear message if there is
        not yet enough data accumulated (so the API never errors on a cold DB).
        The optimizer run is persisted back to the store for auditability.
        """
        features, returns, timestamps = db.load_training_matrix(ticker=ticker)
        if len(features) < min_samples:
            return {
                "success": False,
                "message": (
                    f"Insufficient training data: have {len(features)} samples, "
                    f"need >= {min_samples}. Let the feature pipeline stream longer."
                ),
                "available_samples": int(len(features)),
            }

        result = self.optimize_parameters(features, returns, timestamps)
        result["available_samples"] = int(len(features))
        try:
            db.save_optimization_run(result)
        except Exception as exc:  # noqa: BLE001 - persistence must not break the API
            result["persist_warning"] = str(exc)
        return result
