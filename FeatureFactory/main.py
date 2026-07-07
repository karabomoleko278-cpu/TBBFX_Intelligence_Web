import asyncio
import csv
import math
import time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from datetime import datetime
from core.math_greeks import fetch_local_gex_data
from core.stream_processor import StreamProcessor
from core.governance_agent import execute_agent_assessment, MicrostructureAssessment
from core.options_exposure_engine import OptionsExposureEngine
from core.state_db import get_state_db
from core.ml_optimizer import ExecutionOptimizer
from core.momentum import MomentumScorer, label_for
from core.config import settings
from pathlib import Path
from typing import List, Dict, Any, Optional

app = FastAPI(
    title="TBBFX Centralized Feature Factory",
    description="Centralized data refinery for options exposures, microstructure metrics, and agentic governance.",
    version="1.0.0"
)

# Enable CORS for MAUI, the local web terminal, and the hosted Cloudflare
# terminal when it is explicitly opened with ?local=1 on the operator laptop.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5000",
        "http://127.0.0.1:5000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:8010",
        "http://127.0.0.1:8010",
        "https://tbbfx-intelligence-web.pages.dev",
    ],
    allow_origin_regex=r"https://.*\.tbbfx-intelligence-web\.pages\.dev",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    allow_private_network=True,
)

# Background task and stream processor references
stream_tasks: Dict[str, asyncio.Task] = {}
processors: Dict[str, StreamProcessor] = {}

# Active websocket connections
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                # If a client is disconnected or fails, handle silently
                pass

manager = ConnectionManager()

# Durable local state + options-exposure engine + ML optimizer (shared singletons)
db = get_state_db()
exposure_engine = OptionsExposureEngine(db=db)
optimizer = ExecutionOptimizer()
momentum_scorer = MomentumScorer()

_TF_TO_MT5 = {
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
    "W1": "TIMEFRAME_W1",
}

_TF_SECONDS = {
    "M5": 300,
    "M15": 900,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
    "W1": 604800,
}

_SOLUTION_ROOT = Path(__file__).resolve().parents[2] / "TBBFX_Solution"
if not _SOLUTION_ROOT.exists():
    _SOLUTION_ROOT = Path(r"C:\Users\Dineo Lebese\source\repos\TBBFX_Solution")


def _clean_symbol(symbol: str) -> str:
    sym = symbol.upper().strip()
    return sym[:-1] if sym.endswith("M") and sym[:-1] in settings.WATCHLIST else sym


def _resolve_mt5_symbol(symbol: str) -> str:
    sym = _clean_symbol(symbol)
    return sym + settings.MT5_SYMBOL_SUFFIX if settings.MT5_SYMBOL_SUFFIX else sym


def _csv_history_path(symbol: str) -> Optional[Path]:
    sym = _clean_symbol(symbol)
    scripts = _SOLUTION_ROOT / "Scripts"
    candidates = [
        scripts / f"{sym}{settings.MT5_SYMBOL_SUFFIX}_Historical_Data.csv",
        scripts / f"{sym}m_Historical_Data.csv",
        scripts / f"{sym}_Historical_Data.csv",
    ]
    return next((p for p in candidates if p.exists()), None)


def _read_csv_m5(symbol: str) -> List[Dict[str, Any]]:
    path = _csv_history_path(symbol)
    if path is None:
        return []
    rows: List[Dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                # Treat exporter timestamps as UTC-like epoch seconds for the browser chart.
                dt = datetime.fromisoformat(str(row["Timestamp"]).replace("Z", "+00:00"))
                rows.append({
                    "time": int(dt.timestamp()),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": float(row.get("Volume") or 0),
                })
            except Exception:
                continue
    return rows


def _aggregate_candles(rows: List[Dict[str, Any]], tf: str) -> List[Dict[str, Any]]:
    seconds = _TF_SECONDS.get(tf, 300)
    if tf == "M5":
        return rows
    buckets: Dict[int, Dict[str, Any]] = {}
    for r in rows:
        bucket = int(math.floor(r["time"] / seconds) * seconds)
        b = buckets.get(bucket)
        if b is None:
            buckets[bucket] = {
                "time": bucket,
                "open": r["open"],
                "high": r["high"],
                "low": r["low"],
                "close": r["close"],
                "volume": r.get("volume", 0),
            }
        else:
            b["high"] = max(b["high"], r["high"])
            b["low"] = min(b["low"], r["low"])
            b["close"] = r["close"]
            b["volume"] = b.get("volume", 0) + r.get("volume", 0)
    return [buckets[k] for k in sorted(buckets)]


def _mt5_candles(symbol: str, tf: str, count: int) -> Optional[List[Dict[str, Any]]]:
    try:
        import MetaTrader5 as mt5
    except Exception:
        return None
    timeframe_name = _TF_TO_MT5.get(tf)
    if timeframe_name is None:
        return None
    if not mt5.initialize():
        return None
    try:
        mt5_symbol = _resolve_mt5_symbol(symbol)
        if not mt5.symbol_select(mt5_symbol, True):
            # Some brokers expose the clean name; try it before giving up.
            mt5_symbol = _clean_symbol(symbol)
            mt5.symbol_select(mt5_symbol, True)
        timeframe_value = getattr(mt5, timeframe_name, None)
        copy_rates = getattr(mt5, "copy_rates_from_pos", None)
        if timeframe_value is None or copy_rates is None:
            return None
        rates = copy_rates(mt5_symbol, timeframe_value, 0, max(10, min(count, 1200)))
        if rates is None or len(rates) == 0:
            return None
        candles = []
        for r in rates:
            candles.append({
                "time": int(r["time"]),
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "volume": float(r["tick_volume"]),
            })
        return candles
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass


def _momentum_for(symbol: str) -> Dict[str, Any]:
    """Score one symbol from live order flow (CVD/OBI in memory) + the latest
    persisted GEX snapshot. Reads the DB only (no blocking yfinance call in the
    request path)."""
    sym = symbol.upper()
    proc = processors.get(sym)
    obi = proc.obi if proc is not None else None
    cvd = proc.cvd if proc is not None else None
    hist = db.get_gex_history(sym, limit=1)
    gex_snapshot = hist[0] if hist else None
    result = momentum_scorer.score(sym, obi=obi, cvd=cvd, gex_snapshot=gex_snapshot)
    result["monitored"] = proc is not None
    return result

@app.on_event("startup")
async def startup_event():
    """Initializes real-time streaming tasks on startup."""
    print("[FeatureFactory] Starting up and launching stream processors...")
    for ticker in settings.WATCHLIST:
        # Stream from the broker-suffixed symbol (e.g. 'XAUUSDm') but key/label
        # everything by the clean symbol ('XAUUSD').
        mt5_sym = ticker + settings.MT5_SYMBOL_SUFFIX if settings.MT5_SYMBOL_SUFFIX else ticker
        processor = StreamProcessor(symbol=ticker, mt5_symbol=mt5_sym)
        processors[ticker] = processor

        # Start the processing loop in the background
        task = asyncio.create_task(processor.run_loop())
        stream_tasks[ticker] = task

        # Start a broadcaster task to push metrics to websocket clients
        asyncio.create_task(broadcast_ticker_metrics(ticker, processor))

    # Start the automated gamma-flip scanner across the watchlist — only if
    # explicitly enabled, so the free yfinance feed isn't hammered and the
    # scanner never starves foreground /api/exposure requests on startup.
    if settings.ENABLE_STARTUP_SCANNER:
        print("[FeatureFactory] Startup gamma-flip scanner ENABLED.")
        asyncio.create_task(exposure_engine.run_scanner_loop(settings.WATCHLIST))
    else:
        print("[FeatureFactory] Startup scanner disabled (set ENABLE_STARTUP_SCANNER=1 to enable). "
              "Use GET /api/scan on demand.")

async def broadcast_ticker_metrics(ticker: str, processor: StreamProcessor):
    """Periodically broadcasts the processor's metrics to active WebSocket connections."""
    while True:
        if not processor.running:
            break
        
        payload = {
            "symbol": ticker,
            "cvd": processor.cvd,
            "microprice": processor.microprice,
            "obi": processor.obi,
            "depth": processor.depth,  # real broker L1-L5 ladder when DOM available, else empty
            "footprints": {str(k): v for k, v in list(processor.footprint.items())[-5:]} # latest 5 levels
        }
        await manager.broadcast(payload)
        await asyncio.sleep(0.5) # Broadcast at 2Hz

@app.on_event("shutdown")
async def shutdown_event():
    """Stops all streams on shutdown."""
    print("[FeatureFactory] Shutting down streams...")
    for ticker, processor in processors.items():
        processor.stop()
    for ticker, task in stream_tasks.items():
        task.cancel()
    print("[FeatureFactory] All streams shut down.")

# ==========================================
# REST API ENDPOINTS
# ==========================================

@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "TBBFX Centralized Feature Factory",
        "active_symbols": settings.WATCHLIST
    }

@app.get("/api/exposure/{symbol}")
def get_options_exposure(symbol: str):
    """Net GEX per strike, the Gamma Flip price and the SVI smile — persisted to the local DB."""
    sym = symbol.upper()
    try:
        return exposure_engine.analyze(sym)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch exposure metrics: {str(e)}")

@app.get("/api/scan")
def scan_watchlist():
    """Runs the automated gamma-flip scanner across the configured watchlist."""
    try:
        return {"scanned": settings.WATCHLIST, "results": exposure_engine.scan_watchlist(settings.WATCHLIST)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Scan failed: {str(e)}")

@app.get("/api/history/gex/{symbol}")
def get_gex_history(symbol: str, limit: int = 200):
    """Historical GEX / gamma-flip snapshots preserved across restarts."""
    return {"symbol": symbol.upper(), "history": db.get_gex_history(symbol.upper(), limit=limit)}

@app.get("/api/svi/{symbol}")
def get_svi_params(symbol: str):
    """Most-recent persisted SVI volatility parameters for a symbol."""
    params = db.get_latest_svi_parameters(symbol.upper())
    if not params:
        raise HTTPException(status_code=404, detail=f"No persisted SVI parameters for {symbol.upper()} yet.")
    return params

@app.post("/api/optimize")
def run_optimization(symbol: str = None):
    """Runs the ML execution optimizer over persisted training data."""
    try:
        return optimizer.optimize_from_store(db, ticker=symbol.upper() if symbol else None)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Optimization failed: {str(e)}")

@app.post("/api/assessment/{symbol}", response_model=MicrostructureAssessment)
async def get_agent_assessment(symbol: str):
    """Triggers the Google Antigravity Agent to run a microstructure assessment."""
    sym = symbol.upper()
    try:
        assessment = await execute_agent_assessment(sym)
        # Calculate a mock composite momentum score locally based on recent values
        # score = w_gex * S_gex + w_delta * S_delta + w_squeeze * S_squeeze + ...
        # (Inside assessment, our mock sdk outputs realistic values)
        return assessment
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent execution failed: {str(e)}")

@app.get("/api/features/{symbol}")
def get_latest_microstructure_features(symbol: str):
    """Gets the current OBI, CVD, and Microprice features directly from memory."""
    sym = symbol.upper()
    if sym not in processors:
        raise HTTPException(status_code=404, detail=f"Symbol {sym} is not currently monitored in streams.")
        
    proc = processors[sym]
    return {
        "symbol": sym,
        "cvd": proc.cvd,
        "microprice": proc.microprice,
        "obi": proc.obi,
        "footprint_count": len(proc.footprint)
    }

@app.get("/api/candles/{symbol}/{timeframe}")
def get_candles(symbol: str, timeframe: str, count: int = 240):
    """Real per-symbol OHLC candles for the order-flow workspace.

    Primary source is the connected MT5 terminal, so the web terminal uses the
    same broker candles as the operator's trading account. If MT5 is unavailable
    we return the latest exported historical CSV as a clearly-labelled fallback
    instead of letting the frontend fabricate a generic chart.
    """
    sym = _clean_symbol(symbol)
    tf = timeframe.upper().replace("1H", "H1").replace("4H", "H4").replace("1D", "D1").replace("1W", "W1")
    if tf not in _TF_SECONDS:
        raise HTTPException(status_code=400, detail=f"Unsupported timeframe: {timeframe}")

    limit = max(20, min(int(count or 240), 1200))
    candles = _mt5_candles(sym, tf, limit)
    if candles:
        return {
            "symbol": sym,
            "timeframe": tf,
            "source": "mt5",
            "authentic": True,
            "candles": candles[-limit:],
        }

    csv_rows = _aggregate_candles(_read_csv_m5(sym), tf)
    if csv_rows:
        return {
            "symbol": sym,
            "timeframe": tf,
            "source": "csv_historical_fallback",
            "authentic": True,
            "stale": True,
            "candles": csv_rows[-limit:],
        }

    raise HTTPException(status_code=404, detail=f"No candle history available for {sym} {tf}")

@app.get("/api/momentum")
def get_market_momentum():
    """Market-wide Composite Market Momentum (terminal panel 2B.3): the average
    of the per-symbol composite scores across all monitored watchlist symbols."""
    per_symbol = [_momentum_for(sym) for sym in processors.keys()]
    if not per_symbol:
        base = momentum_scorer.score("MARKET")
        base["monitored_count"] = 0
        return base
    avg = sum(s["score"] for s in per_symbol) / len(per_symbol)
    avg = round(avg, 1)
    return {
        "score": avg,
        "label": label_for(avg),
        "monitored_count": len(per_symbol),
        "symbols": {s["symbol"]: s["score"] for s in per_symbol},
        "timestamp": time.time(),
    }

@app.get("/api/momentum/{symbol}")
def get_symbol_momentum(symbol: str):
    """Per-symbol Composite Market Momentum (0-100) from live CVD/OBI + the
    latest persisted GEX lean. Consumed by the MAUI BiasGrid momentum column."""
    return _momentum_for(symbol)

@app.get("/api/greeks/{symbol}")
def get_greek_exposures(symbol: str):
    """Real per-symbol higher-order greek exposure — DEX/VEX/CHEX, each in
    [-1, 1] — aggregated from the same free options chain the GEX uses
    (terminal panel 2B.2). Massive options can replace the source later."""
    sym = symbol.upper()
    try:
        a = exposure_engine.analyze(sym)
        return {
            "symbol": sym,
            "options_proxy": a.get("options_proxy", sym),
            "dex": a.get("dex", 0.0),
            "vex": a.get("vex", 0.0),
            "chex": a.get("chex", 0.0),
            "dte": a.get("dte"),
            "timestamp": a.get("timestamp"),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Greeks fetch failed: {str(e)}")

@app.get("/api/volprofile/{symbol}")
def get_volume_profile(symbol: str, buckets: int = 26):
    """Real volume-by-price profile + recent closes for the active symbol
    (Massive FX/metal aggregates when entitled, else yfinance proxy) — terminal
    liquidity heatmap 2A.1."""
    try:
        return exposure_engine.volume_profile(symbol.upper(), buckets=buckets)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Volume profile failed: {str(e)}")

@app.get("/api/macro")
def get_macro():
    """Real macro context for the terminal header — US 10-year treasury yield
    from Massive when entitled, else null (header keeps its prior value)."""
    from core import massive
    y10 = massive.latest_treasury_10y()
    return {
        "us10y": y10,
        "source": "massive" if y10 is not None else "unavailable",
        "timestamp": time.time(),
    }

# ==========================================
# WEBSOCKET STREAMING GATEWAY
# ==========================================

@app.websocket("/ws/features")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive, listen for any messages
            data = await websocket.receive_text()
            # Echo back keep-alive or handle requests
            await websocket.send_json({"type": "pong", "payload": data})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
