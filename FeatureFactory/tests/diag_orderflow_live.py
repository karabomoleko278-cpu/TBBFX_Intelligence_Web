"""Live proof that the quote-only order-flow proxy produces moving CVD/OBI.

Polls real MT5 top-of-book for each symbol for a few seconds and prints the
evolving CVD / OBI / microprice. Run: python -m tests.diag_orderflow_live
"""
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import MetaTrader5 as mt5
from core.stream_processor import StreamProcessor

SYMBOLS = ["XAUUSDm", "EURUSDm", "USTECm", "US30m", "GBPUSDm", "USDJPYm"]
SECONDS = 6

if not mt5.initialize():
    print("init failed", mt5.last_error()); sys.exit(1)

for sym in SYMBOLS:
    mt5.symbol_select(sym, True)
    sp = StreamProcessor(symbol=sym.rstrip("m"), mt5_symbol=sym)
    last = None
    updates = 0
    t_end = time.time() + SECONDS
    while time.time() < t_end:
        tk = mt5.symbol_info_tick(sym)
        if tk and tk.time_msc != last and (tk.bid > 0 or tk.ask > 0):
            last = tk.time_msc
            tv = float(getattr(tk, "volume_real", 0) or getattr(tk, "volume", 0) or 0.0)
            sp.process_quote(tk.bid, tk.ask, tv)
            updates += 1
        time.sleep(0.05)
    print(f"{sym:9s} updates={updates:4d}  CVD={sp.cvd:+8.1f}  OBI={sp.obi:+.4f}  "
          f"micro={sp.microprice:.5f}  footprint_levels={len(sp.footprint)}")

mt5.shutdown()
print("\nIf CVD/OBI are non-zero and updates>0, the proxy is working on live data.")
