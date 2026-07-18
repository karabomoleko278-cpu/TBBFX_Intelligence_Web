import asyncio
import copy
import csv
import ipaddress
import math
import time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from datetime import datetime
from core.math_greeks import fetch_local_gex_data
from core.stream_processor import StreamProcessor
from core.governance_agent import execute_agent_assessment, MicrostructureAssessment, router as governance_router
from core.options_exposure_engine import OptionsExposureEngine
from core.state_db import get_state_db
from core.ml_optimizer import ExecutionOptimizer
from core.momentum import MomentumScorer, label_for
from core.config import settings
from core.macro_intelligence import (
    build_geopolitical_feed_live,
    build_macro_calendar_live,
    build_macro_geopolitical_intelligence_live,
)
from core.news_aggregator import get_news_aggregator
from core.sentiment_engine import TbbFxSentimentEngine
from core.cot_positioning import CotPositioningEngine, get_cot_positioning_engine
from core.liquidity_engine import FedNetLiquidityEngine, get_fed_net_liquidity_engine
from core.yield_spread_engine import YieldSpreadEngine, get_yield_spread_engine
from core.yield_curve_engine import YieldCurveEngine, get_yield_curve_engine
from core.regime_handshake import get_macro_regime_handshake
from core.cache_manager import (
    HIGH_FREQUENCY_TTL_SECONDS,
    LOW_FREQUENCY_TTL_SECONDS,
    OFFLINE_RETRY_TTL_SECONDS,
    get_local_cache,
)
from core.fallback_router import ResilientFallbackRouter
from core.public_series_fallback import PublicFredSeriesFallbackFetcher
from core.public_cot_fallback import PublicCftcFallbackFetcher
from core.query_params import (
    EmptyQuery,
    GeopoliticalNewsQuery,
    GovernanceQuery,
    MacroEconomicQuery,
    MarketDataQuery,
    OptimizationQuery,
    ValidationSuiteQuery,
)
from core.router_extensions import tbbfx_router_command
from core.rate_limiter import (
    TbbFxIpRateLimiter,
    cache_policy_for,
    is_public_market_path,
    resolve_client_ip,
    resolve_forwarded_ip,
)
from core.tbbfx_object import make_tbbfx_object, pack_tbbfx_object, to_transport_dict
from core.validation_suite import get_validation_suite_snapshot
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

public_rate_limiter = TbbFxIpRateLimiter(
    permit_limit=settings.PUBLIC_RATE_LIMIT_PER_MINUTE,
    window_seconds=60,
)

_PUBLIC_TERMINAL_ORIGINS = {
    "https://tbbfx-intelligence-web.pages.dev",
}


def _is_private_peer(request: Request) -> bool:
    if request.client is None:
        return False
    if request.client.host.strip().lower() in {"localhost", "testclient"}:
        return True
    try:
        return ipaddress.ip_address(request.client.host).is_private or ipaddress.ip_address(
            request.client.host
        ).is_loopback
    except ValueError:
        return False


def _is_public_gateway_request(request: Request) -> bool:
    """Identify public ingress without weakening local operator workflows."""
    if request.headers.get("x-tbbfx-public-gateway", "").strip() == "1":
        return True

    origin = request.headers.get("origin", "").strip().lower().rstrip("/")
    if origin in _PUBLIC_TERMINAL_ORIGINS:
        return True
    if origin.endswith(".tbbfx-intelligence-web.pages.dev"):
        return True

    # A direct non-private connection is treated as public even if a proxy
    # marker is missing. Localhost/private-LAN development remains interactive.
    return not _is_private_peer(request)


def _public_read_only_denial(request: Request) -> Response:
    envelope = make_tbbfx_object(
        [],
        provider="feature_factory_security",
        route="security.public_read_only",
        warnings=["The public monitor accepts read-only GET and HEAD requests only."],
        extra={
            "status": 403,
            "system_mode": "PUBLIC READ-ONLY MONITOR",
            "attempted_method": request.method,
        },
    )
    headers = {
        "Cache-Control": "private, no-store",
        "CDN-Cache-Control": "no-store",
        "X-TBBFX-System-Mode": "public-read-only",
        "Vary": "Accept, Origin",
    }
    if "application/x-msgpack" in request.headers.get("accept", "").lower():
        return Response(
            content=pack_tbbfx_object(envelope),
            status_code=403,
            media_type="application/x-msgpack",
            headers=headers,
        )
    return JSONResponse(
        content=to_transport_dict(envelope),
        status_code=403,
        headers=headers,
    )


def _merge_vary_header(response: Response, value: str) -> None:
    current = {item.strip() for item in response.headers.get("Vary", "").split(",") if item.strip()}
    current.add(value)
    response.headers["Vary"] = ", ".join(sorted(current))


@app.middleware("http")
async def production_gateway_guardrails(request: Request, call_next):
    """Apply the final per-IP limit and explicit CDN cache contract."""
    is_public_gateway = _is_public_gateway_request(request)
    if is_public_gateway and request.method not in {"GET", "HEAD"}:
        return _public_read_only_denial(request)

    decision = None
    if request.method not in {"HEAD", "OPTIONS"} and is_public_market_path(request.url.path):
        client_ip = resolve_client_ip(request)
        decision = public_rate_limiter.check(client_ip)
        if not decision.allowed:
            envelope = make_tbbfx_object(
                [],
                provider="feature_factory_security",
                route="security.rate_limit",
                warnings=["Public request limit exceeded. Retry after the advertised interval."],
                extra={
                    "status": 429,
                    "retry_after_seconds": decision.retry_after_seconds,
                },
            )
            headers = {
                "Retry-After": str(decision.retry_after_seconds),
                "X-RateLimit-Limit": str(decision.limit),
                "X-RateLimit-Remaining": "0",
                "Cache-Control": "private, no-store",
                "CDN-Cache-Control": "no-store",
                "Vary": "Accept, Origin",
            }
            if "application/x-msgpack" in request.headers.get("accept", "").lower():
                return Response(
                    content=pack_tbbfx_object(envelope),
                    status_code=429,
                    media_type="application/x-msgpack",
                    headers=headers,
                )
            return JSONResponse(
                content=to_transport_dict(envelope),
                status_code=429,
                headers=headers,
            )

    response = await call_next(request)
    _merge_vary_header(response, "Accept")
    _merge_vary_header(response, "Origin")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    if is_public_gateway:
        response.headers["X-TBBFX-System-Mode"] = "public-read-only"

    if decision is not None:
        response.headers["X-RateLimit-Limit"] = str(decision.limit)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)

    if response.status_code >= 400:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["CDN-Cache-Control"] = "no-store"
    else:
        browser_policy, cdn_policy = cache_policy_for(request)
        response.headers["Cache-Control"] = browser_policy
        if cdn_policy:
            response.headers["CDN-Cache-Control"] = cdn_policy
        else:
            response.headers["CDN-Cache-Control"] = "no-store"

    return response

app.include_router(governance_router)

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
sentiment_engine = TbbFxSentimentEngine()
cot_positioning_engine = get_cot_positioning_engine()
liquidity_engine = get_fed_net_liquidity_engine()
yield_spread_engine = get_yield_spread_engine()
yield_curve_engine = get_yield_curve_engine()
macro_regime_handshake = get_macro_regime_handshake()
macro_cache = get_local_cache()
macro_fallback_router = ResilientFallbackRouter()
sentiment_refresh_lock = asyncio.Lock()
public_series_fallback = PublicFredSeriesFallbackFetcher()
public_cot_fallback = PublicCftcFallbackFetcher()
secondary_cot_positioning_engine = CotPositioningEngine(
    source_fetcher=public_cot_fallback,
    cache_ttl_seconds=LOW_FREQUENCY_TTL_SECONDS,
)
secondary_liquidity_engine = FedNetLiquidityEngine(
    series_fetcher=public_series_fallback,
    cache_ttl_seconds=LOW_FREQUENCY_TTL_SECONDS,
)
secondary_yield_spread_engine = YieldSpreadEngine(
    series_fetcher=public_series_fallback,
    cache_ttl_seconds=HIGH_FREQUENCY_TTL_SECONDS,
)
secondary_yield_curve_engine = YieldCurveEngine(
    series_fetcher=public_series_fallback,
    cache_ttl_seconds=LOW_FREQUENCY_TTL_SECONDS,
)
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


def _macro_payload_usable(payload: Dict[str, Any]) -> bool:
    """Reject provider-shaped error packets while accepting valid zero values."""
    status = str(payload.get("status") or "").strip().lower()
    if status in {"unavailable", "offline", "error", "failed"}:
        return False
    for key in ("positions", "spreads", "historical_weekly", "symbols", "items"):
        value = payload.get(key)
        if isinstance(value, (list, dict)) and bool(value):
            return True
    return any(
        payload.get(key) is not None
        for key in ("net_liquidity_millions", "calculated_slope_bps", "sentiment_score")
    )


def _filter_macro_payload(payload: Dict[str, Any], symbol: Optional[str]) -> Dict[str, Any]:
    """Apply symbol filtering after one globally cached provider request."""
    result = copy.deepcopy(payload)
    if not symbol:
        return result
    normalized = _clean_symbol(symbol)
    for key in ("positions", "spreads"):
        values = result.get(key)
        if isinstance(values, list):
            result[key] = [
                item for item in values
                if isinstance(item, dict) and _clean_symbol(str(item.get("symbol") or "")) == normalized
            ]
    symbols = result.get("symbols")
    if isinstance(symbols, list):
        result["symbols"] = [
            item for item in symbols
            if isinstance(item, dict) and _clean_symbol(str(item.get("symbol") or "")) == normalized
        ]
    elif isinstance(symbols, dict):
        match = next(
            (
                value for key, value in symbols.items()
                if _clean_symbol(str(key)) == normalized
            ),
            None,
        )
        result["symbols"] = [] if match is None else [match]
    result["requested_symbol"] = normalized
    return result


def _cached_macro_snapshot(cache_key: str) -> Optional[Dict[str, Any]]:
    cached, metadata = macro_cache.get_with_metadata(cache_key)
    if not isinstance(cached, dict) or metadata is None:
        return None
    cached["cache_status"] = "HIT"
    cached["cache_age_seconds"] = round(float(metadata["age_seconds"]), 3)
    cached["cache_ttl_remaining_seconds"] = round(float(metadata["ttl_remaining_seconds"]), 3)
    return cached


def _guarded_macro_snapshot_sync(
    *,
    cache_key: str,
    ttl_seconds: int,
    route: str,
    primary,
    secondary=None,
) -> Dict[str, Any]:
    """Serve memory, then primary/secondary/durable providers without raising."""
    cached = _cached_macro_snapshot(cache_key)
    if cached is not None:
        return cached

    # Recheck after acquiring the per-key lock. Only one concurrent request is
    # allowed to call upstream providers when an entry expires.
    with macro_cache.refresh_lock(cache_key):
        cached = _cached_macro_snapshot(cache_key)
        if cached is not None:
            return cached

        def durable_save(payload: Dict[str, Any]) -> None:
            db.save_macro_fallback_state(cache_key, payload, provider=payload.get("provider"))

        payload = macro_fallback_router.execute(
            route=route,
            primary=primary,
            secondary=secondary,
            validator=_macro_payload_usable,
            durable_load=lambda: db.get_macro_fallback_state(cache_key),
            durable_save=durable_save,
        )
        payload["cache_status"] = "MISS_REFRESHED"
        ttl = (
            OFFLINE_RETRY_TTL_SECONDS
            if payload.get("data_status") == "SERVICE_TEMPORARILY_OFFLINE"
            else ttl_seconds
        )
        macro_cache.set(cache_key, payload, ttl)
        return payload


async def _guarded_macro_snapshot(**kwargs) -> Dict[str, Any]:
    return await asyncio.to_thread(_guarded_macro_snapshot_sync, **kwargs)


async def _refresh_sentiment_snapshot(cache_key: str) -> Dict[str, Any]:
    failures: List[str] = []
    try:
        news_snapshot = await get_news_aggregator().snapshot(limit=150)
        analysis = sentiment_engine.weighted_sentiment(news_snapshot.get("items", []))
        analysis["warnings"] = list(
            dict.fromkeys((news_snapshot.get("warnings", []) or []) + (analysis.get("warnings", []) or []))
        )
        analysis["provider"] = "multi_provider_news_weighted_lexicon"
        analysis["last_updated"] = (
            news_snapshot.get("last_updated")
            or news_snapshot.get("generated_at")
            or datetime.now().astimezone().isoformat()
        )
        analysis["data_status"] = news_snapshot.get("data_status", "LIVE_PRIMARY")
        analysis["status_warning"] = news_snapshot.get("status_warning")
        if news_snapshot.get("items"):
            analysis["status"] = "available"
            db.save_macro_fallback_state(cache_key, analysis, provider=analysis["provider"])
        else:
            failures.extend(analysis["warnings"] or ["No usable news observations were returned."])
            analysis = db.get_macro_fallback_state(cache_key) or {}
            if analysis:
                analysis["data_status"] = "FALLBACK_REDUNDANCY_ACTIVE"
                analysis["status_warning"] = "FALLBACK_REDUNDANCY_ACTIVE"
                analysis["fallback_source"] = "durable_local_state"
                analysis["warnings"] = list(dict.fromkeys((analysis.get("warnings", []) or []) + failures))
            else:
                from core.fallback_router import service_offline_payload
                analysis = service_offline_payload("api.macro.sentiment", failures)
    except Exception as exc:  # provider boundaries must not break frontend polling
        failures.append(f"Sentiment providers failed: {exc}")
        analysis = db.get_macro_fallback_state(cache_key) or {}
        if analysis:
            analysis["data_status"] = "FALLBACK_REDUNDANCY_ACTIVE"
            analysis["status_warning"] = "FALLBACK_REDUNDANCY_ACTIVE"
            analysis["fallback_source"] = "durable_local_state"
            analysis["warnings"] = list(dict.fromkeys((analysis.get("warnings", []) or []) + failures))
        else:
            from core.fallback_router import service_offline_payload
            analysis = service_offline_payload("api.macro.sentiment", failures)

    analysis["read_only"] = True
    analysis["advisory_only"] = True
    analysis["execution_mutation_allowed"] = False
    analysis["cache_status"] = "MISS_REFRESHED"
    ttl = (
        OFFLINE_RETRY_TTL_SECONDS
        if analysis.get("data_status") == "SERVICE_TEMPORARILY_OFFLINE"
        else HIGH_FREQUENCY_TTL_SECONDS
    )
    macro_cache.set(cache_key, analysis, ttl)
    return analysis


async def _guarded_sentiment_snapshot() -> Dict[str, Any]:
    cache_key = "macro:sentiment:watchlist"
    cached = _cached_macro_snapshot(cache_key)
    if cached is not None:
        return cached

    # The news aggregator is asynchronous, so use a dedicated async refresh
    # lock and recheck memory before touching any upstream feed.
    async with sentiment_refresh_lock:
        cached = _cached_macro_snapshot(cache_key)
        if cached is not None:
            return cached
        return await _refresh_sentiment_snapshot(cache_key)


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
    print(f"[FeatureFactory] Starting process role={settings.PROCESS_ROLE}.")
    if settings.RUN_NEWS_AGGREGATOR:
        await get_news_aggregator().start()
    else:
        print("[FeatureFactory] News aggregator disabled for this replica.")

    if settings.RUN_STREAM_PROCESSORS:
        print("[FeatureFactory] Launching leader stream processors...")
    for ticker in settings.WATCHLIST if settings.RUN_STREAM_PROCESSORS else []:
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
    if settings.RUN_STREAM_PROCESSORS and settings.ENABLE_STARTUP_SCANNER:
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
    if settings.RUN_NEWS_AGGREGATOR:
        await get_news_aggregator().stop()
    print("[FeatureFactory] All streams shut down.")

# ==========================================
# REST API ENDPOINTS
# ==========================================

@app.get("/")
def read_root():
    return {
        "status": "online",
        "service": "TBBFX Centralized Feature Factory",
        "active_symbols": settings.WATCHLIST,
        "process_role": settings.PROCESS_ROLE,
        "stream_processors": settings.RUN_STREAM_PROCESSORS,
        "news_aggregator": settings.RUN_NEWS_AGGREGATOR,
    }

@app.get("/api/exposure/{symbol}")
@tbbfx_router_command(query_model=MarketDataQuery, provider="options_exposure_engine", route="api.exposure")
def get_options_exposure(symbol: str):
    """Net GEX per strike, the Gamma Flip price and the SVI smile - persisted to the local DB."""
    sym = symbol.upper()
    return exposure_engine.analyze(sym)

@app.get("/api/scan")
@tbbfx_router_command(query_model=EmptyQuery, provider="options_exposure_engine", route="api.scan")
def scan_watchlist():
    """Runs the automated gamma-flip scanner across the configured watchlist."""
    return {"scanned": settings.WATCHLIST, "results": exposure_engine.scan_watchlist(settings.WATCHLIST)}

@app.get("/api/history/gex/{symbol}")
@tbbfx_router_command(query_model=MarketDataQuery, provider="sqlite_state_db", route="api.history.gex")
def get_gex_history(symbol: str, limit: int = 200):
    """Historical GEX / gamma-flip snapshots preserved across restarts."""
    return {"symbol": symbol.upper(), "history": db.get_gex_history(symbol.upper(), limit=limit)}

@app.get("/api/svi/{symbol}")
@tbbfx_router_command(query_model=MarketDataQuery, provider="sqlite_state_db", route="api.svi")
def get_svi_params(symbol: str):
    """Most-recent persisted SVI volatility parameters for a symbol."""
    params = db.get_latest_svi_parameters(symbol.upper())
    if not params:
        raise HTTPException(status_code=404, detail=f"No persisted SVI parameters for {symbol.upper()} yet.")
    return params

@app.post("/api/optimize")
@tbbfx_router_command(query_model=OptimizationQuery, provider="ml_optimizer", route="api.optimize")
def run_optimization(symbol: str = None):
    """Runs the ML execution optimizer over persisted training data."""
    return optimizer.optimize_from_store(db, ticker=symbol.upper() if symbol else None)

@app.post("/api/assessment/{symbol}")
@tbbfx_router_command(query_model=GovernanceQuery, provider="openbb_governance_agent", route="api.assessment")
async def get_agent_assessment(symbol: str):
    """Triggers the governance agent to run a read-only microstructure assessment."""
    sym = symbol.upper()
    return await execute_agent_assessment(sym)

@app.get("/api/features/{symbol}")
@tbbfx_router_command(query_model=MarketDataQuery, provider="stream_processor", route="api.features")
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
        "footprint_count": len(proc.footprint),
    }

@app.get("/api/candles/{symbol}/{timeframe}")
@tbbfx_router_command(query_model=MarketDataQuery, provider="market_data_router", route="api.candles")
def get_candles(symbol: str, timeframe: str, count: int = 240):
    """Real per-symbol OHLC candles for the order-flow workspace."""
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
            "warnings": [f"MT5 candles unavailable for {sym} {tf}; using exported CSV history."],
        }

    raise HTTPException(status_code=404, detail=f"No candle history available for {sym} {tf}")

@app.get("/api/momentum")
@tbbfx_router_command(query_model=EmptyQuery, provider="momentum_scorer", route="api.momentum")
def get_market_momentum():
    """Market-wide Composite Market Momentum (terminal panel 2B.3)."""
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
@tbbfx_router_command(query_model=MarketDataQuery, provider="momentum_scorer", route="api.momentum.symbol")
def get_symbol_momentum(symbol: str):
    """Per-symbol Composite Market Momentum from live CVD/OBI + persisted GEX lean."""
    return _momentum_for(symbol)

@app.get("/api/greeks/{symbol}")
@tbbfx_router_command(query_model=MarketDataQuery, provider="options_exposure_engine", route="api.greeks")
def get_greek_exposures(symbol: str):
    """Real per-symbol higher-order greek exposure - DEX/VEX/CHEX."""
    sym = symbol.upper()
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

@app.get("/api/volprofile/{symbol}")
@tbbfx_router_command(query_model=MarketDataQuery, provider="options_exposure_engine", route="api.volprofile")
def get_volume_profile(symbol: str, buckets: int = 26):
    """Real volume-by-price profile + recent closes for the active symbol."""
    return exposure_engine.volume_profile(symbol.upper(), buckets=buckets)

@app.get("/api/macro")
@tbbfx_router_command(query_model=EmptyQuery, provider="macro_router", route="api.macro")
def get_macro():
    """Real macro context for the terminal header."""
    from core import massive
    y10 = massive.latest_treasury_10y()
    return {
        "us10y": y10,
        "source": "massive" if y10 is not None else "unavailable",
        "timestamp": time.time(),
        "warnings": [] if y10 is not None else ["10Y treasury source unavailable; frontend may retain prior value."],
    }


@app.get("/api/macro/calendar")
@tbbfx_router_command(
    query_model=MacroEconomicQuery,
    provider="macro_intelligence_router",
    route="api.macro.calendar",
    messagepack=True,
)
async def get_macro_calendar(
    request: Request,
    symbol: Optional[str] = None,
    importance: Optional[str] = None,
    country: Optional[str] = None,
    limit: int = 150,
):
    """Read-only macro calendar milestones for the geographic Macro Map."""
    return await build_macro_calendar_live(
        symbol=symbol,
        importance=importance,
        country=country,
        limit=limit,
    )


@app.get("/api/macro/validation-suite")
@tbbfx_router_command(
    query_model=ValidationSuiteQuery,
    provider="immutable_validation_snapshot",
    route="api.macro.validation_suite",
    messagepack=True,
)
def get_macro_validation_suite(request: Request, symbol: str):
    """Return a public, historical OOS scorecard with no execution capability."""
    return get_validation_suite_snapshot(symbol)


@app.get("/api/macro/geopolitical-feed")
@tbbfx_router_command(
    query_model=GeopoliticalNewsQuery,
    provider="macro_intelligence_router",
    route="api.macro.geopolitical_feed",
    messagepack=True,
)
async def get_macro_geopolitical_feed(
    request: Request,
    symbol: Optional[str] = None,
    keywords: Optional[str] = None,
    category: Optional[str] = None,
    country: Optional[str] = None,
    source: Optional[str] = None,
    min_latitude: Optional[float] = None,
    max_latitude: Optional[float] = None,
    min_longitude: Optional[float] = None,
    max_longitude: Optional[float] = None,
    limit: int = 150,
):
    """Read-only geopolitical/event stream for the geographic Macro Map."""
    return await build_geopolitical_feed_live(
        symbol=symbol,
        keywords=keywords,
        category=category,
        country=country,
        source=source,
        min_latitude=min_latitude,
        max_latitude=max_latitude,
        min_longitude=min_longitude,
        max_longitude=max_longitude,
        limit=limit,
    )


@app.get("/api/macro/geopolitical-intelligence/{symbol}")
@tbbfx_router_command(
    query_model=GeopoliticalNewsQuery,
    provider="macro_intelligence_router",
    route="api.macro.geopolitical_intelligence",
    messagepack=True,
)
async def get_macro_geopolitical_intelligence(request: Request, symbol: str, limit: int = 150):
    """Read-only combined macro/geopolitical map packet for one watchlist symbol."""
    return await build_macro_geopolitical_intelligence_live(symbol=symbol, limit=limit)


@app.get("/api/macro/geospatial-nodes")
@tbbfx_router_command(
    query_model=GeopoliticalNewsQuery,
    provider="macro_intelligence_router",
    route="api.macro.geospatial_nodes",
    messagepack=True,
)
async def get_macro_geospatial_nodes(request: Request, symbol: Optional[str] = None, limit: int = 150):
    """Read-only geospatial node packet for the 3D Macro Map globe."""
    return await build_macro_geopolitical_intelligence_live(symbol=symbol, limit=limit)


@app.get("/api/macro/sentiment")
@tbbfx_router_command(
    query_model=MacroEconomicQuery,
    provider="macro_sentiment_router",
    route="api.macro.sentiment",
    messagepack=True,
)
async def get_macro_sentiment(
    request: Request,
    symbol: Optional[str] = None,
    limit: int = 150,
):
    """Return read-only, severity-weighted news sentiment for the watchlist."""
    analysis = await _guarded_sentiment_snapshot()
    return _filter_macro_payload(analysis, symbol)


@app.get("/api/macro/cot-positioning")
@tbbfx_router_command(
    query_model=MacroEconomicQuery,
    provider="cftc_positioning_router",
    route="api.macro.cot_positioning",
    messagepack=True,
)
async def get_macro_cot_positioning(
    request: Request,
    symbol: Optional[str] = None,
    limit: int = 52,
):
    """Return read-only CFTC positioning and 52-week percentile telemetry."""
    packet = await _guarded_macro_snapshot(
        cache_key="macro:cot-positioning:watchlist",
        ttl_seconds=LOW_FREQUENCY_TTL_SECONDS,
        route="api.macro.cot_positioning",
        primary=lambda: cot_positioning_engine.snapshot(None),
        secondary=lambda: secondary_cot_positioning_engine.snapshot(None),
    )
    return _filter_macro_payload(packet, symbol)


@app.get("/api/macro/liquidity-index")
@tbbfx_router_command(
    query_model=EmptyQuery,
    provider="fred_net_liquidity",
    route="api.macro.liquidity_index",
    messagepack=True,
)
async def get_macro_liquidity_index(request: Request):
    """Return read-only USD net-liquidity context for the macro workspace."""
    return await _guarded_macro_snapshot(
        cache_key="macro:liquidity-index",
        ttl_seconds=LOW_FREQUENCY_TTL_SECONDS,
        route="api.macro.liquidity_index",
        primary=liquidity_engine.snapshot,
        secondary=secondary_liquidity_engine.snapshot,
    )


@app.get("/api/macro/yield-spreads")
@tbbfx_router_command(
    query_model=MacroEconomicQuery,
    provider="fred_yield_spreads",
    route="api.macro.yield_spreads",
    messagepack=True,
)
async def get_macro_yield_spreads(
    request: Request,
    symbol: Optional[str] = None,
    limit: int = 150,
):
    """Return read-only sovereign 10-year spread context for supported FX pairs."""
    packet = await _guarded_macro_snapshot(
        cache_key="macro:yield-spreads:watchlist",
        ttl_seconds=HIGH_FREQUENCY_TTL_SECONDS,
        route="api.macro.yield_spreads",
        primary=lambda: yield_spread_engine.snapshot(None),
        secondary=lambda: secondary_yield_spread_engine.snapshot(None),
    )
    return _filter_macro_payload(packet, symbol)


@app.get("/api/macro/yield-curve")
@tbbfx_router_command(
    query_model=EmptyQuery,
    provider="fred_yield_curve",
    route="api.macro.yield_curve",
    messagepack=True,
)
async def get_macro_yield_curve(request: Request):
    """Return the read-only US 10Y-2Y curve and inversion advisory."""
    return await _guarded_macro_snapshot(
        cache_key="macro:yield-curve",
        ttl_seconds=LOW_FREQUENCY_TTL_SECONDS,
        route="api.macro.yield_curve",
        primary=yield_curve_engine.snapshot,
        secondary=secondary_yield_curve_engine.snapshot,
    )


@app.get("/api/macro/regime-state")
@tbbfx_router_command(
    query_model=EmptyQuery,
    provider="tbbfx_read_only_macro_handshake",
    route="api.macro.regime_state",
    messagepack=True,
)
async def get_macro_regime_state(request: Request):
    """Synthesize mixed-frequency macro context without mutating execution state."""
    news_snapshot = await get_news_aggregator().snapshot(limit=150)
    sentiment, cot_positioning, liquidity, yield_curve, yield_spreads = await asyncio.gather(
        _guarded_sentiment_snapshot(),
        _guarded_macro_snapshot(
            cache_key="macro:cot-positioning:watchlist",
            ttl_seconds=LOW_FREQUENCY_TTL_SECONDS,
            route="api.macro.cot_positioning",
            primary=lambda: cot_positioning_engine.snapshot(None),
            secondary=lambda: secondary_cot_positioning_engine.snapshot(None),
        ),
        _guarded_macro_snapshot(
            cache_key="macro:liquidity-index",
            ttl_seconds=LOW_FREQUENCY_TTL_SECONDS,
            route="api.macro.liquidity_index",
            primary=liquidity_engine.snapshot,
            secondary=secondary_liquidity_engine.snapshot,
        ),
        _guarded_macro_snapshot(
            cache_key="macro:yield-curve",
            ttl_seconds=LOW_FREQUENCY_TTL_SECONDS,
            route="api.macro.yield_curve",
            primary=yield_curve_engine.snapshot,
            secondary=secondary_yield_curve_engine.snapshot,
        ),
        _guarded_macro_snapshot(
            cache_key="macro:yield-spreads:watchlist",
            ttl_seconds=HIGH_FREQUENCY_TTL_SECONDS,
            route="api.macro.yield_spreads",
            primary=lambda: yield_spread_engine.snapshot(None),
            secondary=lambda: secondary_yield_spread_engine.snapshot(None),
        ),
    )
    sentiment.setdefault("source_frequency", "NEWS_INTRADAY")
    sentiment.setdefault("refresh_cadence_seconds", 60)
    regime = macro_regime_handshake.evaluate(
        sentiment=sentiment,
        cot_positioning=cot_positioning,
        liquidity=liquidity,
        yield_curve=yield_curve,
        yield_spreads=yield_spreads,
        news_items=news_snapshot.get("items", []),
    )
    component_warnings = []
    for packet in (news_snapshot, sentiment, cot_positioning, liquidity, yield_curve, yield_spreads):
        component_warnings.extend(packet.get("warnings", []) or [])
    regime["warnings"] = list(dict.fromkeys((regime.get("warnings", []) or []) + component_warnings))
    return regime


# ==========================================
# WEBSOCKET STREAMING GATEWAY
# ==========================================

@app.websocket("/ws/features")
async def websocket_endpoint(websocket: WebSocket):
    direct = websocket.client.host if websocket.client else "unknown"
    client_ip = resolve_forwarded_ip(direct, websocket.headers)
    decision = public_rate_limiter.check(f"websocket:{client_ip}")
    if not decision.allowed:
        await websocket.close(code=1008, reason="Public connection rate limit exceeded.")
        return
    await manager.connect(websocket)
    try:
        while True:
            # Keep connection alive, listen for any messages
            data = await websocket.receive_text()
            # Echo back keep-alive or handle requests
            await websocket.send_json({"type": "pong", "payload": data})
    except WebSocketDisconnect:
        manager.disconnect(websocket)
