# TBBFX Intelligence Web

An autonomous options-microstructure intelligence stack: a Python **Centralized
Feature Factory** computes options exposure (GEX), volatility smiles (SVI) and
order-flow microstructure (CVD / OBI / Microprice), an autonomous agent layer
recommends execution stances behind **blocking risk gateways**, a C# **SignalR
Feature Store** is the online cache, and a dark/neon **web terminal** + **.NET
MAUI mobile client** consume it in real time.

> **Cost:** runs entirely on **free, keyless data** (yfinance / Yahoo Finance).
> No paid market-data subscription is required or contacted.

---

## Architecture

```
                ┌──────────────────────── FeatureFactory (Python / FastAPI) ───────────────────────┐
 yfinance ─────▶│  OptionsExposureEngine   →  Net GEX/strike, Gamma-Flip scanner, SVI smile        │
 (free)         │  StreamProcessor         →  CVD · OBI · Microprice (MT5 or simulator)             │
                │  governance_agent        →  autonomous agent + Decide/Transform risk gateways     │
                │  ExecutionOptimizer (ML) →  frequency + win-rate-band penalised optimisation       │
                │  StateDatabase (SQLite)  →  durable SVI params + training data + GEX history        │
                │  feature_pipeline (Bytewax) ─── streams CVD/OBI/Microprice/GEX ──┐                 │
                └──────────────────────────────────────────────────────────────────┼────────────────┘
                                                                                    ▼  POST /features/update
                                                  ┌──── SignalRFeatureStore (C# / .NET 10) ────┐
                                                  │  Online feature cache (in-memory)           │
                                                  │  MarketPulseHub  → Volume Delta + OBI push  │
                                                  └──────────────┬───────────────┬─────────────┘
                                                                 ▼               ▼
                                                   terminal/*.html        mobile/MarketPulseClient.cs
                                                   (web terminal)         (.NET MAUI companion app)
```

---

## Deliverables map

| # | Requirement | Where |
|---|-------------|-------|
| 1 | `OptionsExposureEngine` — Net GEX/strike from yfinance OI + automated Gamma-Flip scanner | [FeatureFactory/core/options_exposure_engine.py](FeatureFactory/core/options_exposure_engine.py) |
| 2 | Local state DB (SQLite) for training data + SVI params, preserved across restarts | [FeatureFactory/core/state_db.py](FeatureFactory/core/state_db.py) |
| 3 | `MarketPulseHub` — streams Volume Delta + OBI to web & handles MAUI clients | [SignalRFeatureStore/Hubs/MarketPulseHub.cs](SignalRFeatureStore/Hubs/MarketPulseHub.cs) |
| 4 | Dark/neon terminal, high-contrast tokens, no overlapping boundaries | [terminal/TBBFX Intelligence Terminal.html](terminal/TBBFX%20Intelligence%20Terminal.html) |
| + | Risk Gateways (blocking **Decide** / **Transform**) | [FeatureFactory/core/governance_agent.py](FeatureFactory/core/governance_agent.py) |
| + | Feature Store streaming via **Bytewax** | [FeatureFactory/core/feature_pipeline.py](FeatureFactory/core/feature_pipeline.py) |
| + | Mobile Sync (.NET MAUI) | [mobile/MarketPulseClient.cs](mobile/MarketPulseClient.cs) |
| + | Uncapped ML w/ frequency + win-rate constraints | [FeatureFactory/core/ml_optimizer.py](FeatureFactory/core/ml_optimizer.py) |
| + | Local Greek matrix / butterfly-arb-free SVI (numerical) | [FeatureFactory/core/math_greeks.py](FeatureFactory/core/math_greeks.py) |

---

## Running it

### 1. Python Feature Factory
```bash
cd FeatureFactory
pip install -r requirements.txt
uvicorn main:app --reload          # http://localhost:8000  (docs at /docs)
```
Key endpoints: `GET /api/exposure/{sym}`, `GET /api/scan`, `GET /api/history/gex/{sym}`,
`GET /api/svi/{sym}`, `POST /api/assessment/{sym}`, `POST /api/optimize`, `WS /ws/features`.

### 2. C# SignalR Feature Store
```bash
cd SignalRFeatureStore
dotnet run                         # http://localhost:5000
```
MarketPulseHub: `/hub/marketpulse` · MAUI query: `GET /features/latest/{sym}`.

### 3. Web terminal
Open `terminal/TBBFX Intelligence Terminal.html` directly, or serve the folder.
It connects to `ws://localhost:8000/ws/features`; if the backend is offline it
transparently runs a simulator and the badge reads **SIMULATED FEED**.

### 4. Bytewax feature pipeline (optional runtime)
```bash
pip install bytewax
python -m bytewax.run core.feature_pipeline:flow
# or, without bytewax installed, the asyncio fallback:
python -m core.feature_pipeline
```

### 5. Mobile (.NET MAUI)
Add `mobile/MarketPulseClient.cs` to the `TBBFX.App` project, add the
`Microsoft.AspNetCore.SignalR.Client` package, and register it in `MauiProgram.cs`
(see the header comment in that file).

### Tests
```bash
cd FeatureFactory
python -m pytest tests -q          # 13 tests
```

---

## Instrument universe & the options-proxy mapping

The stack is configured for the symbols you actually trade (the MAUI app
watchlist + your MT5 charts):

| Instrument | Order flow (CVD/OBI/microprice) | GEX / Gamma-Flip via options proxy |
|-----------|--------------------------------|-------------------------------------|
| XAUUSD (Gold) | native (MT5 `XAUUSDm`) | **GLD** |
| USTEC (Nasdaq 100) | native (`USTECm`) | **QQQ** |
| US30 (Dow) | native (`US30m`) | **DIA** |
| EURUSD | native (`EURUSDm`) | **FXE** |
| GBPUSD | native (`GBPUSDm`) | **FXB** |
| USDJPY | native (`USDJPYm`) | **FXY** |

**Why proxies?** Spot FX, metals and indices have **no free listed-options
chain**. Dealer gamma is therefore modelled on the most liquid US-listed ETF
that tracks the same underlying — the exact GLD/QQQ/DIA mapping your MAUI app
already uses for its MT5 bridge fallback. Order-flow features still stream on the
real instrument. The mapping lives in `core/config.py::SYMBOL_PROXIES` and the
API/terminal label every GEX reading with which proxy produced it.

**Open-interest caveat:** Yahoo's free feed frequently returns
`openInterest = 0` intraday even for very liquid names (e.g. GLD, QQQ). The
engine detects this and falls back to **traded volume** as the gamma weight, and
picks the expiry with the most real activity rather than the front contract.
Each reading reports `weight_source: "open_interest"` or `"volume"` so you always
know which was used. Configure via `WATCHLIST` and `MT5_SYMBOL_SUFFIX` env vars.

## Honest engineering notes

These are deliberate, documented design choices — not hidden assumptions:

1. **"Google Antigravity SDK" is a local shim.** There is no public,
   pip-installable Google Antigravity *trading-agent* SDK with Decide/Transform
   lifecycle hooks. `google_antigravity/` is a local interface shim that models
   that lifecycle so the risk gateways are real and testable. The deterministic
   parts (tool execution, Decide/Transform, structured output) run for real; the
   LLM "reasoning" step is simulated. Swap in a real vendor SDK by re-pointing
   the import.

2. **The win-rate band does not guarantee a live win rate.** The optimizer
   penalises in-sample win rates outside 65–68% and penalises any 24h window
   with zero trades. This *steers the search* on historical data; realised live
   win rate is a property of genuine edge in the features, not of the penalty.

3. **Free data caveats.** yfinance is free and keyless but unofficial,
   rate-limited and delayed. Treat figures as indicative, not execution-grade.

4. **Risk gateways are guardrails, not trade authorisation.** Decide blocks
   unauthorised symbols / grossly oversized orders; Transform clamps size to the
   configured cap. Nothing in this repo connects to a live broker.

5. **CVD/OBI on retail FX are tick-rule proxies, not exchange data.** Diagnostics
   against the live Exness MT5 feed confirmed it publishes **quote-only ticks**
   (`BID|ASK` flags), **zero volume**, and **no Depth-of-Market**. So:
   * **CVD** is a cumulative *signed-tick* flow (Lee-Ready style: uptick =
     buyer-initiated), not true volume delta. It accumulates into a momentum
     line over minutes.
   * **OBI** is a spread-damped quote-pressure proxy in [-1, 1], not real
     order-book depth.
   * **Microprice** is spread-weighted (tighter side = heavier).
   Live fetch uses `symbol_info_tick` (not `copy_ticks_from(datetime.now())`,
   which silently returned nothing due to a local-vs-broker-server timezone
   offset). Run `python -m tests.diag_mt5_ticks` to re-inspect your feed.
