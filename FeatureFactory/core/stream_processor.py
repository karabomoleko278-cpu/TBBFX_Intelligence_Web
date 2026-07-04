import asyncio
import time
import requests
import random
from datetime import datetime
from typing import Dict, List, Any, Optional
from core.config import settings

# Attempt to import MetaTrader 5 (only works on Windows with MT5 terminal installed)
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

class StreamProcessor:
    def __init__(self, symbol: str = "EURUSD", mt5_symbol: str = None):
        self.symbol = symbol
        # The symbol actually requested from the MT5 terminal may carry a broker
        # suffix (Exness uses 'm', e.g. 'XAUUSDm'); the clean `symbol` stays the
        # public key/label used by the feature store and terminal.
        self.mt5_symbol = mt5_symbol or symbol
        self.cvd: float = 0.0
        # Footprint structure: {price: {bid_volume: float, ask_volume: float}}
        self.footprint: Dict[float, Dict[str, float]] = {}
        self.microprice: float = 0.0
        self.obi: float = 0.0
        # Real broker DOM (Level II) ladder — filled from MT5 market_book_get when
        # the broker provides depth; otherwise stays empty and OBI uses tick-rule.
        self.depth: Dict[str, Any] = {"bids": [], "asks": []}
        self._book_ok: bool = False
        self.running: bool = False

        # Keep track of last processed tick time to avoid duplicate processing
        self.last_tick_time: int = 0

        # State for the quote-only tick-rule order-flow proxy (retail FX/CFD).
        self._prev_mid: float = None
        self._prev_dir: int = 0
        self._ema_mid: float = None
        
    def initialize_mt5(self) -> bool:
        """Attempts to initialize MetaTrader 5."""
        if not MT5_AVAILABLE:
            print("[StreamProcessor] MetaTrader5 package not available. Falling back to simulator.")
            return False
            
        if not mt5.initialize():
            print(f"[StreamProcessor] MT5 initialize failed, error code: {mt5.last_error()}. Falling back to simulator.")
            return False
            
        print("[StreamProcessor] MetaTrader 5 initialized successfully.")
        # Subscribe to the broker Depth-of-Market (Level II) for REAL L1-L5 OBI.
        # Many FX brokers expose DOM only for some symbols; if unavailable we
        # transparently keep the tick-rule OBI proxy.
        try:
            if mt5.market_book_add(self.mt5_symbol):
                self._book_ok = True
                print(f"[StreamProcessor] DOM subscribed for {self.mt5_symbol} — real L1-L5 OBI active.")
            else:
                print(f"[StreamProcessor] DOM unavailable for {self.mt5_symbol} (err {mt5.last_error()}) — using tick-rule OBI proxy.")
        except Exception as ex:
            print(f"[StreamProcessor] market_book_add failed: {ex} — using tick-rule OBI proxy.")
        return True

    def calculate_microprice(self, bid_price: float, bid_vol: float, ask_price: float, ask_vol: float) -> float:
        """
        Calculates the Microprice based on inside spread and volume:
        Microprice = (V_bid * P_ask + V_ask * P_bid) / (V_bid + V_ask)
        """
        total_vol = bid_vol + ask_vol
        if total_vol == 0:
            return (bid_price + ask_price) / 2.0
        return (bid_vol * ask_price + ask_vol * bid_price) / total_vol

    def calculate_obi(self, bid_depth: List[float], ask_depth: List[float]) -> float:
        """
        Calculates the Order Book Imbalance (OBI) across depth levels:
        OBI = (Sum(V_bid) - Sum(V_ask)) / (Sum(V_bid) + Sum(V_ask))
        """
        sum_bid = sum(bid_depth)
        sum_ask = sum(ask_depth)
        total = sum_bid + sum_ask
        if total == 0:
            return 0.0
        return (sum_bid - sum_ask) / total

    def _update_depth(self) -> bool:
        """Read the broker DOM via MT5 and compute a REAL L1-L5 OBI + ladder.

        Returns True if real depth was captured (and overrides ``self.obi`` with
        the genuine order-book imbalance + fills ``self.depth``); False otherwise,
        leaving the tick-rule proxy in place.
        """
        if not MT5_AVAILABLE or not self._book_ok:
            return False
        try:
            book = mt5.market_book_get(self.mt5_symbol)
            if not book:
                return False
            # MT5 book types: SELL=1, BUY=2, SELL_MARKET=3, BUY_MARKET=4.
            BUY  = getattr(mt5, "BOOK_TYPE_BUY", 2)
            BUYM = getattr(mt5, "BOOK_TYPE_BUY_MARKET", 4)
            SELL = getattr(mt5, "BOOK_TYPE_SELL", 1)
            SELLM = getattr(mt5, "BOOK_TYPE_SELL_MARKET", 3)
            bids, asks = [], []
            for e in book:
                t = getattr(e, "type", 0)
                vol = float(getattr(e, "volume_dbl", 0) or getattr(e, "volume", 0) or 0)
                price = float(getattr(e, "price", 0) or 0)
                if vol <= 0 or price <= 0:
                    continue
                if t in (BUY, BUYM):
                    bids.append({"price": price, "volume": vol})
                elif t in (SELL, SELLM):
                    asks.append({"price": price, "volume": vol})
            if not bids or not asks:
                return False
            bids.sort(key=lambda x: x["price"], reverse=True)   # best (highest) bid first
            asks.sort(key=lambda x: x["price"])                  # best (lowest) ask first
            bids, asks = bids[:5], asks[:5]
            self.depth = {"bids": bids, "asks": asks}
            sum_bid = sum(b["volume"] for b in bids)
            sum_ask = sum(a["volume"] for a in asks)
            total = sum_bid + sum_ask
            if total > 0:
                self.obi = (sum_bid - sum_ask) / total          # REAL L1-L5 OBI
            return True
        except Exception:
            return False

    def process_quote(self, bid: float, ask: float, tick_volume: float = 0.0):
        """
        Order-flow update for a QUOTE-ONLY feed (retail FX/CFD via MT5).

        Retail brokers like Exness publish bid/ask quotes with no exchange volume
        and no buy/sell aggressor flag, so classic CVD/footprint can't be formed
        directly. We use the standard tick-rule (Lee-Ready style) proxy:

          * direction = sign of mid-price change vs the previous quote
            (uptick => buyer-initiated, downtick => seller-initiated);
          * size      = broker tick_volume if > 0, else a synthetic 1 unit per
            tick (so CVD becomes a cumulative *tick* delta — a momentum proxy);
          * microprice is computed from a spread-implied book where the side with
            the tighter half-spread is treated as having more resting size, so
            OBI reflects quote pressure rather than (unavailable) real depth.

        This keeps CVD/OBI meaningful on a feed that has neither volume nor DOM.
        """
        mid = (bid + ask) / 2.0
        spread = max(ask - bid, 1e-12)

        # 1. Tick-rule direction from mid-price change.
        if self._prev_mid is None:
            direction = 0
        elif mid > self._prev_mid:
            direction = 1          # uptick -> buyer initiated
        elif mid < self._prev_mid:
            direction = -1         # downtick -> seller initiated
        else:
            direction = self._prev_dir  # unchanged -> carry previous (tick rule)
        self._prev_mid = mid
        self._prev_dir = direction

        size = tick_volume if tick_volume and tick_volume > 0 else 1.0

        # 2. CVD as cumulative signed tick flow.
        self.cvd += direction * size

        # 3. OBI proxy from quote pressure. A rising mid implies bid-side demand;
        #    we map normalised recent drift into a [-1, 1] imbalance. Spread is
        #    used to damp the signal when the market is wide/illiquid.
        drift = 0.0 if self._prev_mid is None else (mid - (self._ema_mid or mid))
        self._ema_mid = mid if self._ema_mid is None else (0.9 * self._ema_mid + 0.1 * mid)
        # tanh keeps it bounded; scale by price so it's unit-consistent across symbols
        import math
        self.obi = math.tanh((drift / spread) * 0.5)

        # 4. Spread-weighted microprice (tighter side = heavier).
        #    Weight each side inversely to its distance from mid.
        bid_w = 1.0 / max(mid - bid, 1e-9)
        ask_w = 1.0 / max(ask - mid, 1e-9)
        self.microprice = (bid_w * bid + ask_w * ask) / (bid_w + ask_w)

        # 5. Footprint by direction (synthetic volume).
        rp = round(mid, 5)
        if rp not in self.footprint:
            self.footprint[rp] = {"bid_volume": 0.0, "ask_volume": 0.0}
        if direction >= 0:
            self.footprint[rp]["ask_volume"] += size
        else:
            self.footprint[rp]["bid_volume"] += size
        if len(self.footprint) > 100:
            del self.footprint[sorted(self.footprint.keys())[0]]

    def process_tick(self, bid: float, ask: float, bid_vol: float, ask_vol: float, trade_price: float, trade_vol: float, side: str):
        """
        Processes a single tick, updates CVD, Microprice, Footprint, and OBI.
        """
        # 1. Update Microprice
        self.microprice = self.calculate_microprice(bid, bid_vol, ask, ask_vol)
        
        # 2. Update CVD (Cumulative Volume Delta)
        # side is either 'buy' (aggressive buying at Ask) or 'sell' (aggressive selling at Bid)
        delta = trade_vol if side == "buy" else -trade_vol
        self.cvd += delta
        
        # 3. Update Footprint Chart
        rounded_price = round(trade_price, 5)
        if rounded_price not in self.footprint:
            self.footprint[rounded_price] = {"bid_volume": 0.0, "ask_volume": 0.0}
            
        if side == "buy":
            self.footprint[rounded_price]["ask_volume"] += trade_vol
        else:
            self.footprint[rounded_price]["bid_volume"] += trade_vol
            
        # Limit footprint size to prevent memory bloat (keep last 100 levels)
        if len(self.footprint) > 100:
            sorted_prices = sorted(self.footprint.keys())
            del self.footprint[sorted_prices[0]]

    def _footprint_rows(self, anchor_price: float, limit: int = 32) -> List[Dict[str, float]]:
        """Return nearest footprint rows in a SignalR-friendly JSON shape."""
        rows: List[Dict[str, float]] = []
        try:
            ordered = sorted(
                self.footprint.items(),
                key=lambda item: abs(float(item[0]) - anchor_price)
            )[:limit]
            for price, volumes in ordered:
                bid_volume = float(volumes.get("bid_volume", 0.0))
                ask_volume = float(volumes.get("ask_volume", 0.0))
                rows.append({
                    "price": float(price),
                    "bid_volume": bid_volume,
                    "ask_volume": ask_volume,
                    "delta": ask_volume - bid_volume,
                })
        except Exception:
            return []
        return rows

    def push_to_feature_store(self, price: float):
        """Pushes current computed metrics to the C# SignalR feature store API."""
        try:
            # Retrieve the latest GEX and Gamma Flip from local state DB snapshots
            try:
                from core.state_db import get_state_db
                db = get_state_db()
                latest = db.get_gex_history(self.symbol, limit=1)
                net_gex = latest[0]["net_gex"] if latest and latest[0].get("net_gex") is not None else 0.0
                gamma_flip = latest[0]["gamma_flip"] if latest and latest[0].get("gamma_flip") is not None else 0.0
            except Exception:
                net_gex = 0.0
                gamma_flip = 0.0

            cvd_val = float(self.cvd) if self.cvd is not None else 0.0
            microprice_val = float(self.microprice) if self.microprice is not None else 0.0
            obi_val = float(self.obi) if self.obi is not None else 0.0
            price_val = float(price) if price is not None else 0.0

            payload = {
                "symbol": self.symbol,
                "cvd": cvd_val,
                "microprice": microprice_val,
                "obi": obi_val,
                "price": price_val,
                "timestamp": time.time(),
                "net_gex": float(net_gex),
                "gamma_flip": float(gamma_flip),
                "footprint_rows": self._footprint_rows(price_val),
                "depth": self.depth
            }
            # Async-like POST to avoid blocking the main stream loop
            resp = requests.post(settings.SIGNALR_URL, json=payload, timeout=0.5)
            if resp.status_code != 200:
                pass
        except Exception as e:
            # Fail silently to avoid interrupting stream loop
            print(f"[StreamProcessor] Error pushing to feature store: {e}")


    async def run_loop(self):
        """Main stream processor loop."""
        self.running = True
        mt5_active = self.initialize_mt5()
        
        # Pre-seed simulated prices if MT5 is offline
        sim_price = 1.08500
        sim_cvd = 0.0
        
        while self.running:
            try:
                if mt5_active:
                    # Use symbol_info_tick (the current top-of-book) rather than
                    # copy_ticks_from(datetime.now(), ...). The latter is unreliable
                    # because datetime.now() is LOCAL time while MT5 tick times are
                    # broker-SERVER time — a timezone offset means "now" is often in
                    # the future and returns no ticks. symbol_info_tick is always
                    # fresh and timezone-agnostic.
                    tick = mt5.symbol_info_tick(self.mt5_symbol)
                    if tick is not None and (tick.bid > 0 or tick.ask > 0):
                        # De-duplicate: only process genuinely new quotes.
                        if tick.time_msc != self.last_tick_time:
                            self.last_tick_time = tick.time_msc
                            bid = tick.bid
                            ask = tick.ask
                            # Retail FX/CFD feeds carry no exchange volume; use
                            # tick_volume (number of price changes) when present.
                            tick_vol = float(getattr(tick, "volume_real", 0) or getattr(tick, "volume", 0) or 0.0)

                            # Quote-only order-flow proxy (CVD/OBI/microprice).
                            self.process_quote(bid=bid, ask=ask, tick_volume=tick_vol)
                            # Overlay REAL broker DOM (L1-L5) when available; this
                            # overrides the tick-rule OBI with the genuine book.
                            self._update_depth()
                            self.push_to_feature_store(price=self.microprice)
                    await asyncio.sleep(0.1)  # poll top-of-book at ~10Hz
                else:
                    # SIMULATOR FALLBACK
                    # Generate simulated price movements and order book depth
                    spread = 0.00012
                    tick_dir = random.choice([-1, 1])
                    sim_price += tick_dir * 0.00005
                    bid = sim_price - spread / 2.0
                    ask = sim_price + spread / 2.0
                    
                    # Random trade sizes
                    trade_vol = random.uniform(1.0, 50.0)
                    side = random.choice(["buy", "sell"])
                    trade_price = ask if side == "buy" else bid
                    
                    # Simulated book depth
                    bid_depth = [random.uniform(10.0, 100.0) for _ in range(5)]
                    ask_depth = [random.uniform(10.0, 100.0) for _ in range(5)]
                    self.obi = self.calculate_obi(bid_depth, ask_depth)
                    
                    self.process_tick(
                        bid=bid,
                        ask=ask,
                        bid_vol=bid_depth[0],
                        ask_vol=ask_depth[0],
                        trade_price=trade_price,
                        trade_vol=trade_vol,
                        side=side
                    )
                    self.push_to_feature_store(price=trade_price)
                    
            except Exception as e:
                print(f"[StreamProcessor] Error in stream loop: {e}")
                
            await asyncio.sleep(0.2) # Sample frequency: 5Hz (200ms)

    def stop(self):
        self.running = False
        if MT5_AVAILABLE:
            try:
                if self._book_ok:
                    mt5.market_book_release(self.mt5_symbol)
            except Exception:
                pass
            mt5.shutdown()
        print("[StreamProcessor] Stream loop stopped.")
