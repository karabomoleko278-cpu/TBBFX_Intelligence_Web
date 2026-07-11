import os
from typing import List

_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_CORE_DIR)  # FeatureFactory/

# ---------------------------------------------------------------------------
# Instrument universe — matches the symbols traded in the TBBFX MAUI app
# (HomeViewModel watchlist) plus the MT5 charts (USDJPY).
#
# Spot FX / metals / indices have NO free listed-options chain, so dealer GEX
# is computed on the most liquid US-listed ETF *proxy* for each instrument
# (the same GLD/QQQ/DIA mapping the MAUI app already uses for its MT5 bridge).
# Order-flow features (CVD/OBI/Microprice) still stream on the real instrument.
# ---------------------------------------------------------------------------
DEFAULT_WATCHLIST = "XAUUSD,USTEC,US30,EURUSD,GBPUSD,USDJPY"

SYMBOL_PROXIES = {
    "XAUUSD": "GLD",   # Gold       -> SPDR Gold Shares
    "USTEC":  "QQQ",   # Nasdaq 100 -> Invesco QQQ
    "NAS100": "QQQ",   # alias
    "US30":   "DIA",   # Dow Jones  -> SPDR Dow Jones (DIA)
    "US500":  "SPY",   # S&P 500    -> SPDR S&P 500
    "SPX500": "SPY",   # alias
    "EURUSD": "FXE",   # Euro       -> Invesco CurrencyShares Euro
    "GBPUSD": "FXB",   # Cable      -> Invesco CurrencyShares British Pound
    "USDJPY": "FXY",   # Yen        -> Invesco CurrencyShares Japanese Yen (inverse)
}


def resolve_options_proxy(symbol: str) -> str:
    """Map a tradeable instrument to the ETF whose options we model for GEX.

    Strips a trailing broker suffix (e.g. Exness 'XAUUSDm' -> 'XAUUSD') and
    falls back to the symbol itself when it already has a listed-options chain.
    """
    s = symbol.upper().strip()
    if s.endswith("M") and s[:-1] in SYMBOL_PROXIES:  # broker suffix, e.g. XAUUSDm
        s = s[:-1]
    return SYMBOL_PROXIES.get(s, s)


class Settings:
    def __init__(self):
        self.WATCHLIST: List[str] = os.getenv("WATCHLIST", DEFAULT_WATCHLIST).split(",")
        # Broker symbol suffix for the live MT5 tick source (Exness uses 'm').
        self.MT5_SYMBOL_SUFFIX: str = os.getenv("MT5_SYMBOL_SUFFIX", "m")
        self.MIN_NOTIONAL: float = float(os.getenv("MIN_NOTIONAL", "100000.0"))
        self.SIGNALR_URL: str = os.getenv("SIGNALR_URL", "http://127.0.0.1:5000/features/update")
        self.SIGNALR_TIMEOUT_SECONDS: float = float(os.getenv("SIGNALR_TIMEOUT_SECONDS", "2.0"))
        self.SIGNALR_ERROR_LOG_INTERVAL_SECONDS: float = float(
            os.getenv("SIGNALR_ERROR_LOG_INTERVAL_SECONDS", "15.0")
        )
        self.RISK_MAX_ORDER_SIZE: int = int(os.getenv("RISK_MAX_ORDER_SIZE", "1000"))
        self.TBBFX_FEATURE_UPDATE_KEY: str = os.getenv("TBBFX_FEATURE_UPDATE_KEY", "")

        # Free, keyless data only by default. These remain "DEMO_KEY" so no paid
        # provider is ever contacted unless the operator explicitly supplies a key.
        self.FLASHALPHA_API_KEY: str = os.getenv("FLASHALPHA_API_KEY", "DEMO_KEY")
        self.MASSIVE_API_KEY: str = os.getenv("MASSIVE_API_KEY", "DEMO_KEY")

        # Options / GEX modelling
        self.RISK_FREE_RATE: float = float(os.getenv("RISK_FREE_RATE", "0.05"))
        self.CONTRACT_MULTIPLIER: int = int(os.getenv("CONTRACT_MULTIPLIER", "100"))

        # Local state database (SQLite, stdlib — no extra dependency, free)
        self.DB_PATH: str = os.getenv(
            "TBBFX_DB_PATH", os.path.join(_PROJECT_DIR, "data", "tbbfx_state.db")
        )

        # Automated gamma-flip scanner
        self.GAMMA_FLIP_ALERT_PCT: float = float(os.getenv("GAMMA_FLIP_ALERT_PCT", "0.01"))
        # Generous default interval so the free yfinance feed is never hammered
        # (and so the scanner does not starve foreground /api/exposure requests).
        self.SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "300"))
        # Auto-start the background scanner on app startup? Off by default; the
        # /api/scan endpoint and /api/exposure still work on demand either way.
        self.ENABLE_STARTUP_SCANNER: bool = os.getenv("ENABLE_STARTUP_SCANNER", "0") == "1"
        # Seconds to cache a per-symbol GEX analysis (avoids re-scanning yfinance
        # on every request / dashboard refresh).
        self.EXPOSURE_CACHE_TTL: int = int(os.getenv("EXPOSURE_CACHE_TTL", "120"))

settings = Settings()
