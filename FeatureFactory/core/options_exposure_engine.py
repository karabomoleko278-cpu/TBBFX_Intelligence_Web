"""
OptionsExposureEngine
=====================

Service class that turns a free yfinance option chain into a dealer
gamma-exposure (GEX) profile, locates the **Gamma Flip** (zero-gamma) price via
an automated scanning routine, and persists the result + SVI smile parameters to
the local :class:`~core.state_db.StateDatabase`.

Design notes
------------
* **Free data only.** Open interest, strikes and implied vols come from
  ``yfinance`` (Yahoo Finance), which is keyless and free. No paid options
  surface is contacted.
* **Local Greek matrix.** Per-strike gamma is computed from the closed-form
  Black-Scholes gamma in :mod:`core.math_greeks`; the volatility smile is fitted
  with the butterfly-arbitrage-checked SVI calibrator in the same module. This
  keeps the pricing engine self-contained and free of subscription costs.
* The heavy numerics live in :mod:`core.math_greeks` so they stay in one place
  and remain covered by the existing test-suite; this class is the orchestration
  / persistence / scanning layer on top of them.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import yfinance as yf
from scipy.optimize import brentq

from core.config import settings, resolve_options_proxy
from core.math_greeks import bs_greeks, calibrate_svi
from core.state_db import StateDatabase


class OptionsExposureEngine:
    """Computes Net GEX per strike and scans for the Gamma Flip price."""

    def __init__(
        self,
        risk_free_rate: Optional[float] = None,
        contract_multiplier: Optional[int] = None,
        db: Optional[StateDatabase] = None,
    ):
        self.r = risk_free_rate if risk_free_rate is not None else settings.RISK_FREE_RATE
        self.multiplier = (
            contract_multiplier if contract_multiplier is not None else settings.CONTRACT_MULTIPLIER
        )
        self.db = db
        # Short-lived per-symbol cache so dashboard refreshes / the mobile app /
        # the scanner don't each trigger a fresh (slow, rate-limited) yfinance scan.
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ttl = settings.EXPOSURE_CACHE_TTL

    # ==================================================================
    # 1. NET GEX PER STRIKE
    # ==================================================================
    def gamma_exposure(self, spot: float, opt: Dict[str, Any]) -> float:
        """
        Notional gamma exposure of a single option line, in $ / 1% move.

            GEX = sign * Gamma * OI * multiplier * spot^2 * 0.01

        Sign convention (matches the rest of the codebase): long-gamma for calls
        (+) and short-gamma for puts (-), i.e. the classic dealer-gamma proxy.
        """
        T = max(opt["dte"], 1.0) / 365.0
        gamma = bs_greeks(
            S=spot, K=opt["strike"], T=T, r=self.r,
            sigma=opt["implied_volatility"], option_type=opt["type"],
        )["gamma"]
        sign = 1.0 if opt["type"].lower() == "call" else -1.0
        return sign * gamma * opt["open_interest"] * self.multiplier * (spot ** 2) * 0.01

    def compute_net_gex_per_strike(
        self, spot: float, option_chains: List[Dict[str, Any]]
    ) -> Dict[float, float]:
        """Aggregate notional GEX per strike across the whole chain."""
        per_strike: Dict[float, float] = {}
        for opt in option_chains:
            per_strike[opt["strike"]] = per_strike.get(opt["strike"], 0.0) + self.gamma_exposure(spot, opt)
        return per_strike

    def _net_gex_at(self, price: float, option_chains: List[Dict[str, Any]]) -> float:
        """Net GEX of the whole book evaluated *as if* spot were ``price``."""
        total = 0.0
        for opt in option_chains:
            total += self.gamma_exposure(price, opt)
        return total

    def _greek_tilts(self, spot: float, option_chains: List[Dict[str, Any]]) -> Dict[str, float]:
        """Net OI-weighted DELTA / VANNA / CHARM tilt across the chain, each in
        [-1, 1]. Built from the SAME free chain the GEX profile uses; each greek
        is summed with its natural sign (call delta +, put delta -, etc.) weighted
        by open interest, then normalised by the total absolute contribution so the
        panel reads as a directional tilt rather than raw notional.

            DEX  -> net delta exposure  (directional positioning)
            VEX  -> net vanna exposure  (sensitivity of delta to vol)
            CHEX -> net charm exposure  (delta decay into expiry)
        """
        sd = sv = sc = 0.0   # signed sums
        ad = av = ac = 0.0   # absolute sums (normalisers)
        for opt in option_chains:
            T = max(opt["dte"], 1.0) / 365.0
            g = bs_greeks(
                S=spot, K=opt["strike"], T=T, r=self.r,
                sigma=opt["implied_volatility"], option_type=opt["type"],
            )
            oi = opt["open_interest"]
            d, v, c = g["delta"] * oi, g["vanna"] * oi, g["charm"] * oi
            sd += d; sv += v; sc += c
            ad += abs(d); av += abs(v); ac += abs(c)

        def norm(s: float, a: float) -> float:
            return max(-1.0, min(1.0, s / a)) if a > 1e-9 else 0.0

        return {
            "dex": round(norm(sd, ad), 4),
            "vex": round(norm(sv, av), 4),
            "chex": round(norm(sc, ac), 4),
        }

    def volume_profile(self, ticker: str, buckets: int = 26) -> Dict[str, Any]:
        """Real volume-by-price profile + recent close series for ``ticker``.

        Source preference (verified against the live key's entitlements):
          1. **Massive FX/metal aggregates** (C:EURUSD, C:XAUUSD …) — these carry
             real tick volume, so EURUSD/GBPUSD/USDJPY/XAUUSD get a genuine
             volume footprint.
          2. **yfinance ETF proxy** fallback (DIA/QQQ …) for the index CFDs
             (US30/USTEC) and whenever Massive is unset/unentitled/unavailable.
        Powers the liquidity heatmap (2A.1).
        """
        from core import massive

        bars = massive.fx_minute_bars(ticker)
        if bars:
            proxy = massive.fx_ticker(ticker)
            source = f"massive:{proxy}"
            prices = [b["c"] for b in bars]
            vols = [b["v"] for b in bars]
        else:
            proxy = resolve_options_proxy(ticker)
            source = f"yfinance:{proxy}"
            tk = yf.Ticker(proxy)
            hist = tk.history(period="5d", interval="5m")
            if hist is None or hist.empty:
                hist = tk.history(period="3mo", interval="1d")
            if hist is None or hist.empty:
                raise ValueError(f"No price history for {proxy} (proxy of {ticker})")
            prices = [float(p) for p in hist["Close"].tolist()]
            vols = [float(v) if v == v else 0.0 for v in hist["Volume"].tolist()]  # NaN-safe

        lo, hi = min(prices), max(prices)
        rng = (hi - lo) or 1.0
        prof = [0.0] * buckets
        for p, v in zip(prices, vols):
            idx = min(buckets - 1, max(0, int((p - lo) / rng * buckets)))
            prof[idx] += v
        mx = max(prof) or 1.0
        profile = [
            {"price": round(lo + (i + 0.5) / buckets * rng, 5), "vol": round(prof[i] / mx, 4)}
            for i in range(buckets)
        ]
        return {
            "symbol": ticker.upper(),
            "options_proxy": proxy,
            "low": round(lo, 5),
            "high": round(hi, 5),
            "closes": [round(p, 5) for p in prices[-96:]],
            "profile": profile,
            "has_volume": mx > 1.0,
            "source": source,
            "timestamp": time.time(),
        }

    # ==================================================================
    # 2. AUTOMATED GAMMA-FLIP SCANNING ROUTINE
    # ==================================================================
    def detect_gamma_flip(
        self,
        spot: float,
        option_chains: List[Dict[str, Any]],
        search_pct: float = 0.20,
        grid_points: int = 121,
    ) -> Optional[float]:
        """
        Scan net GEX across a price grid around spot and return the price at
        which net dealer gamma flips sign (the "Gamma Flip" / zero-gamma level).

        The routine evaluates the book on a grid, detects the first sign change,
        then refines the crossing to machine precision with Brent's method
        (``scipy.optimize.brentq``). Returns ``None`` if no crossing exists in the
        search band (e.g. an all-positive or all-negative gamma book).
        """
        if not option_chains:
            return None

        lo, hi = spot * (1.0 - search_pct), spot * (1.0 + search_pct)
        grid = np.linspace(lo, hi, grid_points)
        net = np.array([self._net_gex_at(p, option_chains) for p in grid])

        flip: Optional[float] = None
        for i in range(len(net) - 1):
            if net[i] == 0.0:
                flip = float(grid[i])
                break
            if net[i] * net[i + 1] < 0.0:
                # Bracketed sign change -> refine with a root solver.
                try:
                    flip = float(
                        brentq(self._net_gex_at, grid[i], grid[i + 1], args=(option_chains,))
                    )
                except (ValueError, RuntimeError):
                    # Fall back to linear interpolation if brentq cannot converge.
                    g1, g2 = net[i], net[i + 1]
                    flip = float(grid[i] - g1 * (grid[i + 1] - grid[i]) / (g2 - g1))
                break
        return flip

    # ==================================================================
    # 3. FREE OPTION-CHAIN LOADER (yfinance)
    # ==================================================================
    @staticmethod
    def _chain_weight(chain) -> float:
        """Total open-interest+volume on a chain (used to pick the live expiry)."""
        oi = float(np.nan_to_num(chain.calls["openInterest"]).sum()
                   + np.nan_to_num(chain.puts["openInterest"]).sum())
        vol = float(np.nan_to_num(chain.calls["volume"]).sum()
                    + np.nan_to_num(chain.puts["volume"]).sum())
        return oi + vol

    def load_chain(self, ticker: str) -> Dict[str, Any]:
        """Pull spot + the most-active option chain from yfinance (free, keyless).

        For instruments without listed options (spot FX/metals/indices) the
        configured ETF proxy is used. Yahoo's free feed frequently returns
        ``openInterest = 0`` even on highly liquid names, so we fall back to
        ``volume`` as the GEX weight and choose the expiry carrying the most
        real activity rather than blindly taking the front contract.
        """
        proxy = resolve_options_proxy(ticker)

        # Prefer Tradier (real greeks/OI/IV, reliable, no scraping) when
        # TRADIER_TOKEN is set; it returns the same chain shape. Fails soft to
        # the yfinance path below so nothing breaks without a token.
        from core import tradier
        t_chain = tradier.load_chain(proxy)
        if t_chain:
            return t_chain

        tk = yf.Ticker(proxy)
        hist = tk.history(period="1d")
        if hist.empty:
            raise ValueError(f"Could not retrieve spot price for {proxy} (proxy of {ticker})")
        spot = float(hist["Close"].iloc[-1])

        expirations = tk.options
        if not expirations:
            raise ValueError(f"No listed options found for {proxy} (proxy of {ticker})")

        # Pick the expiry (within the first ~12) with the most OI+volume so we
        # never land on an empty 0DTE/just-listed contract.
        best_expiry, best_chain, best_weight = expirations[0], None, -1.0
        for e in expirations[:12]:
            try:
                ch = tk.option_chain(e)
            except Exception:  # noqa: BLE001
                continue
            w = self._chain_weight(ch)
            if w > best_weight:
                best_expiry, best_chain, best_weight = e, ch, w
        if best_chain is None:
            best_expiry = expirations[0]
            best_chain = tk.option_chain(best_expiry)

        dte = max(1.0, float((datetime.strptime(best_expiry, "%Y-%m-%d").date() - datetime.now().date()).days))

        rows: List[Dict[str, Any]] = []
        oi_used = False
        for frame, opt_type in ((best_chain.calls, "call"), (best_chain.puts, "put")):
            for _, row in frame.iterrows():
                iv = row["impliedVolatility"]
                oi = row["openInterest"]
                vol = row.get("volume", 0.0)
                if np.isnan(iv) or iv <= 0:
                    iv = 0.25  # neutral fallback so a missing IV never breaks the fit
                oi = 0.0 if (np.isnan(oi) or oi < 0) else float(oi)
                vol = 0.0 if (vol is None or np.isnan(vol) or vol < 0) else float(vol)
                # Use OI when present, else fall back to traded volume as the
                # gamma-weighting proxy (Yahoo often nulls OI intraday).
                weight = oi if oi > 0 else vol
                if oi > 0:
                    oi_used = True
                rows.append({
                    "strike": float(row["strike"]),
                    "type": opt_type,
                    "open_interest": weight,
                    "implied_volatility": float(iv),
                    "dte": dte,
                })
        return {
            "spot": spot,
            "dte": dte,
            "expiry": best_expiry,
            "chains": rows,
            "proxy": proxy,
            "weight_source": "open_interest" if oi_used else "volume",
        }

    # ==================================================================
    # 4. FULL ANALYSIS + PERSISTENCE
    # ==================================================================
    def analyze(self, ticker: str, persist: bool = True, use_cache: bool = True) -> Dict[str, Any]:
        """End-to-end: load chain, build GEX profile, find flip, fit SVI, persist.

        Results are cached per symbol for ``EXPOSURE_CACHE_TTL`` seconds so rapid
        repeat calls (UI refresh, mobile poll, scanner) reuse a recent reading
        instead of re-hitting the rate-limited free yfinance feed.
        """
        ticker = ticker.upper()
        if use_cache:
            hit = self._cache.get(ticker)
            if hit and (time.time() - hit["timestamp"]) < self._cache_ttl:
                return hit
        loaded = self.load_chain(ticker)
        spot, dte, chains = loaded["spot"], loaded["dte"], loaded["chains"]

        per_strike = self.compute_net_gex_per_strike(spot, chains)
        net_gex = float(sum(per_strike.values()))
        gamma_flip = self.detect_gamma_flip(spot, chains)
        regime = "POSITIVE" if net_gex >= 0 else "NEGATIVE"

        distance_to_flip_pct = (
            abs(spot - gamma_flip) / spot if gamma_flip is not None else None
        )

        # SVI smile on calls that actually carry open interest.
        svi = {"success": False, "params": {}, "durrleman_arbitrage_free": None}
        liquid_calls = [o for o in chains if o["type"] == "call" and o["open_interest"] > 0]
        if len(liquid_calls) >= 5:
            strikes = np.array([o["strike"] for o in liquid_calls])
            vols = np.array([o["implied_volatility"] for o in liquid_calls])
            svi = calibrate_svi(strikes, vols, spot, dte / 365.0)

        # Higher-order greek exposure tilts (DEX/VEX/CHEX) from the same chain.
        tilts = self._greek_tilts(spot, chains)

        result: Dict[str, Any] = {
            "ticker": ticker,
            "options_proxy": loaded.get("proxy", ticker),
            "weight_source": loaded.get("weight_source", "open_interest"),
            "underlying_price": spot,
            "dte": dte,
            "expiry": loaded["expiry"],
            "net_gex": net_gex,
            "gamma_flip": gamma_flip,
            "regime": regime,
            "distance_to_flip_pct": distance_to_flip_pct,
            "num_strikes": len(per_strike),
            # Top-10 strikes by absolute exposure, ready for the UI.
            "top_strikes": [
                {"strike": k, "gex": v}
                for k, v in sorted(per_strike.items(), key=lambda kv: abs(kv[1]), reverse=True)[:10]
            ],
            "svi_calibration": svi,
            "dex": tilts["dex"],
            "vex": tilts["vex"],
            "chex": tilts["chex"],
            "timestamp": time.time(),
        }

        if persist and self.db is not None:
            self.db.save_gex_snapshot(ticker, spot, net_gex, gamma_flip or spot, regime)
            if svi.get("success") and svi.get("params"):
                self.db.save_svi_parameters(
                    ticker, svi["params"], dte, spot,
                    arb_free=bool(svi.get("durrleman_arbitrage_free", False)),
                    success=bool(svi.get("success", False)),
                )

        self._cache[ticker] = result
        return result

    # ==================================================================
    # 5. AUTOMATED WATCHLIST SCANNER
    # ==================================================================
    def scan_watchlist(
        self, symbols: List[str], flip_proximity_pct: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Run :meth:`analyze` across a list of symbols and raise an alert whenever
        spot is sitting within ``flip_proximity_pct`` of its gamma flip — the
        regime boundary where dealer hedging flips from vol-suppressive to
        vol-amplifying.
        """
        threshold = flip_proximity_pct if flip_proximity_pct is not None else settings.GAMMA_FLIP_ALERT_PCT
        alerts: List[Dict[str, Any]] = []
        for sym in symbols:
            try:
                a = self.analyze(sym)
            except Exception as exc:  # noqa: BLE001 - never let one bad symbol stop the scan
                alerts.append({"ticker": sym.upper(), "error": str(exc)})
                continue
            dist = a.get("distance_to_flip_pct")
            a["near_flip"] = dist is not None and dist <= threshold
            if a["near_flip"]:
                a["alert"] = (
                    f"{a['ticker']} within {dist * 100:.2f}% of gamma flip "
                    f"({a['gamma_flip']:.2f}) — regime {a['regime']}"
                )
            alerts.append(a)
        return alerts

    async def run_scanner_loop(
        self, symbols: List[str], interval_seconds: Optional[int] = None
    ):
        """Continuous background scanner (used by the FastAPI startup hook)."""
        import asyncio

        interval = interval_seconds if interval_seconds is not None else settings.SCAN_INTERVAL_SECONDS
        while True:
            try:
                results = await asyncio.to_thread(self.scan_watchlist, symbols)
                fired = [r.get("alert") for r in results if r.get("near_flip")]
                if fired:
                    print(f"[OptionsExposureEngine] GAMMA-FLIP ALERTS: {fired}")
            except Exception as exc:  # noqa: BLE001
                print(f"[OptionsExposureEngine] scan error: {exc}")
            await asyncio.sleep(interval)


if __name__ == "__main__":
    from core.state_db import get_state_db

    engine = OptionsExposureEngine(db=get_state_db())
    print("Analyzing SPY (live yfinance data)...")
    out = engine.analyze("SPY")
    print(f"  Spot         : {out['underlying_price']:.2f}")
    print(f"  Net GEX      : {out['net_gex']:,.0f}")
    print(f"  Gamma Flip   : {out['gamma_flip']}")
    print(f"  Regime       : {out['regime']}")
    print(f"  SVI arb-free : {out['svi_calibration'].get('durrleman_arbitrage_free')}")
