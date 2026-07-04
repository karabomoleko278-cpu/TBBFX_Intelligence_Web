"""
MT5 tick diagnostic — ground-truth check for why CVD/OBI read zero.

Connects to the running MetaTrader 5 terminal and inspects, per symbol:
  * raw tick fields (time, bid, ask, last, volume, volume_real, flags)
  * decoded tick flags (BID/ASK quote vs BUY/SELL/LAST trade)
  * whether Level-2 depth (DOM / market book) is available

Run:  python -m tests.diag_mt5_ticks
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import MetaTrader5 as mt5
except ImportError:
    print("MetaTrader5 package not installed.")
    sys.exit(1)

SYMBOLS = ["XAUUSDm", "EURUSDm", "USTECm", "US30m", "GBPUSDm", "USDJPYm"]


def decode_flags(flags: int) -> str:
    names = []
    for name in ("TICK_FLAG_BID", "TICK_FLAG_ASK", "TICK_FLAG_LAST",
                 "TICK_FLAG_VOLUME", "TICK_FLAG_BUY", "TICK_FLAG_SELL"):
        bit = getattr(mt5, name, None)
        if bit is not None and (flags & bit):
            names.append(name.replace("TICK_FLAG_", ""))
    return "|".join(names) if names else f"raw={flags}"


def main() -> int:
    if not mt5.initialize():
        print(f"initialize() failed: {mt5.last_error()}")
        return 1

    info = mt5.terminal_info()
    acct = mt5.account_info()
    print(f"Terminal: {getattr(info, 'name', '?')}  connected={getattr(info, 'connected', '?')}")
    if acct:
        print(f"Account : {acct.login}  {acct.server}  ({acct.company})")
    print("=" * 78)

    for sym in SYMBOLS:
        si = mt5.symbol_info(sym)
        if si is None:
            print(f"\n{sym}: NOT FOUND (try without 'm' suffix or check Market Watch)")
            continue
        if not si.visible:
            mt5.symbol_select(sym, True)

        print(f"\n### {sym}  (digits={si.digits}, spread={si.spread})")
        # symbol_info volume fields
        print(f"  symbol_info: volume={si.volume} volumehigh={si.volumehigh} "
              f"session_deals={getattr(si, 'session_deals', '?')}")

        # Pull the most recent ticks
        ticks = mt5.copy_ticks_from(sym, datetime.now(), 10, mt5.COPY_TICKS_ALL)
        if ticks is None or len(ticks) == 0:
            print("  copy_ticks_from: NO TICKS returned")
        else:
            n_vol = sum(1 for t in ticks if t["volume"] > 0)
            n_volreal = sum(1 for t in ticks if t["volume_real"] > 0)
            n_last = sum(1 for t in ticks if t["last"] > 0)
            print(f"  ticks={len(ticks)}  with volume>0: {n_vol}  "
                  f"volume_real>0: {n_volreal}  last>0: {n_last}")
            for t in ticks[-3:]:
                print(f"    t={t['time']} bid={t['bid']} ask={t['ask']} last={t['last']} "
                      f"vol={t['volume']} vol_real={t['volume_real']} flags=[{decode_flags(int(t['flags']))}]")

        # Level-2 depth availability
        if mt5.market_book_add(sym):
            book = mt5.market_book_get(sym)
            if book:
                print(f"  DOM: AVAILABLE — {len(book)} levels")
            else:
                print("  DOM: subscribed but EMPTY (broker publishes no depth)")
            mt5.market_book_release(sym)
        else:
            print(f"  DOM: NOT AVAILABLE (market_book_add failed: {mt5.last_error()})")

    mt5.shutdown()
    print("\n" + "=" * 78)
    print("READING GUIDE:")
    print("  • If volume==0 on all ticks  -> CVD can't use tick volume (FX has none).")
    print("  • If flags show only BID|ASK -> these are QUOTES, not trades (no aggressor).")
    print("  • If DOM not available       -> true OBI impossible; needs a quote-based proxy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
