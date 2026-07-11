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
import requests
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

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            if value is None:
                return default
            out = float(value)
            if np.isnan(out) or np.isinf(out):
                return default
            return out
        except Exception:
            return default

    def _transform_chain_row(self, row: Dict[str, Any], opt_type: str, dte: float) -> Dict[str, Any]:
        """TET final transform: normalize raw provider fields into our GEX row shape."""
        iv = self._safe_float(
            row.get("impliedVolatility") or row.get("implied_volatility") or row.get("iv"),
            0.25,
        )
        if iv <= 0:
            iv = 0.25
        oi = self._safe_float(row.get("openInterest") or row.get("open_interest"), 0.0)
        vol = self._safe_float(row.get("volume"), 0.0)
        return {
            "strike": self._safe_float(row.get("strike"), 0.0),
            "type": opt_type,
            "open_interest": oi if oi > 0 else vol,
            "implied_volatility": iv,
            "dte": dte,
            "_oi_present": oi > 0,
        }

    def _yahoo_chart_spot(self, proxy: str) -> float:
        """Backup open endpoint for spot when yfinance wrappers rate-limit."""
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{proxy}"
        r = requests.get(url, params={"range": "5d", "interval": "1d"}, timeout=10)
        r.raise_for_status()
        result = (r.json().get("chart") or {}).get("result") or []
        if not result:
            raise ValueError(f"No chart data from Yahoo backup endpoint for {proxy}")
        meta = result[0].get("meta") or {}
        close = (((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
        prices = [self._safe_float(x, 0.0) for x in close if self._safe_float(x, 0.0) > 0]
        spot = self._safe_float(meta.get("regularMarketPrice"), 0.0) or (prices[-1] if prices else 0.0)
        if spot <= 0:
            raise ValueError(f"Backup spot endpoint returned no usable price for {proxy}")
        return spot

    def _backup_yahoo_options_chain(self, ticker: str, proxy: str, reason: str) -> Dict[str, Any]:
        """Extract from Yahoo's open options endpoint, then normalize to GEX rows.

        This is the TET failure lane used when yfinance errors, rate-limits, or
        returns a market-session chain with zero usable open-interest/volume.
        """
        spot = self._yahoo_chart_spot(proxy)
        first = requests.get(f"https://query2.finance.yahoo.com/v7/finance/options/{proxy}", timeout=12)
        first.raise_for_status()
        root = ((first.json().get("optionChain") or {}).get("result") or [{}])[0]
        expirations = root.get("expirationDates") or []
        if not expirations:
            return self._synthetic_backup_chain(ticker, proxy, spot, reason)

        expiry_ts = int(expirations[0])
        chain_resp = requests.get(
            f"https://query2.finance.yahoo.com/v7/finance/options/{proxy}",
            params={"date": expiry_ts},
            timeout=12,
        )
        chain_resp.raise_for_status()
        root = ((chain_resp.json().get("optionChain") or {}).get("result") or [{}])[0]
        options = (root.get("options") or [{}])[0]
        expiry = datetime.utcfromtimestamp(expiry_ts).strftime("%Y-%m-%d")
        dte = max(1.0, float((datetime.strptime(expiry, "%Y-%m-%d").date() - datetime.now().date()).days))

        rows: List[Dict[str, Any]] = []
        oi_used = False
        for items, opt_type in ((options.get("calls") or [], "call"), (options.get("puts") or [], "put")):
            for raw in items:
                row = self._transform_chain_row(raw, opt_type, dte)
                if row["strike"] <= 0:
                    continue
                oi_used = oi_used or bool(row.pop("_oi_present", False))
                rows.append(row)

        if len(rows) < 4 or sum(r["open_interest"] for r in rows) <= 0:
            return self._synthetic_backup_chain(ticker, proxy, spot, reason)

        return {
            "spot": spot,
            "dte": dte,
            "expiry": expiry,
            "chains": rows,
            "proxy": proxy,
            "weight_source": "open_interest" if oi_used else "volume",
            "source": "yahoo_open_endpoint_backup",
            "failover_reason": reason,
        }

    def _synthetic_backup_chain(self, ticker: str, proxy: str, spot: float, reason: str) -> Dict[str, Any]:
        """Last-resort normalized chain so GEX/SVI stays online during provider faults.

        The synthetic fallback is intentionally conservative: it uses the latest
        open endpoint spot and distributes a small proxy weight around spot so
        downstream SVI/GEX calculators stay alive without fabricating high
        confidence. Results are tagged via source/failover_reason.
        """
        dte = 7.0
        expiry = datetime.utcfromtimestamp(time.time() + dte * 86400).strftime("%Y-%m-%d")
        strikes = np.linspace(spot * 0.92, spot * 1.08, 17)
        rows: List[Dict[str, Any]] = []
        for strike in strikes:
            distance = abs(strike - spot) / max(spot, 1e-9)
            weight = max(10.0, 250.0 * (1.0 - min(distance / 0.08, 1.0)))
            iv = 0.20 + min(distance * 1.5, 0.20)
            rows.append({"strike": float(strike), "type": "call", "open_interest": weight, "implied_volatility": iv, "dte": dte})
            rows.append({"strike": float(strike), "type": "put", "open_interest": weight, "implied_volatility": iv, "dte": dte})
        return {
            "spot": spot,
            "dte": dte,
            "expiry": expiry,
            "chains": rows,
            "proxy": proxy,
            "weight_source": "synthetic_open_endpoint_spot",
            "source": "tet_synthetic_backup",
            "failover_reason": reason,
        }

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

        try:
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
                best_weight = self._chain_weight(best_chain)
            if best_weight <= 0:
                return self._backup_yahoo_options_chain(ticker, proxy, "yfinance_zero_chain_weight")

            dte = max(1.0, float((datetime.strptime(best_expiry, "%Y-%m-%d").date() - datetime.now().date()).days))

            rows: List[Dict[str, Any]] = []
            oi_used = False
            for frame, opt_type in ((best_chain.calls, "call"), (best_chain.puts, "put")):
                for _, row in frame.iterrows():
                    normalized = self._transform_chain_row(row.to_dict(), opt_type, dte)
                    if normalized["strike"] <= 0:
                        continue
                    oi_used = oi_used or bool(normalized.pop("_oi_present", False))
                    rows.append(normalized)

            if len(rows) < 4 or sum(r["open_interest"] for r in rows) <= 0:
                return self._backup_yahoo_options_chain(
                    ticker,
                    proxy,
                    "yfinance_zero_open_interest_and_volume",
                )

            return {
                "spot": spot,
                "dte": dte,
                "expiry": best_expiry,
                "chains": rows,
                "proxy": proxy,
                "weight_source": "open_interest" if oi_used else "volume",
                "source": "yfinance",
            }
        except Exception as exc:  # noqa: BLE001 - fail over instead of faulting the workspace
            return self._backup_yahoo_options_chain(
                ticker,
                proxy,
                f"yfinance_error:{type(exc).__name__}:{exc}",
            )

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
