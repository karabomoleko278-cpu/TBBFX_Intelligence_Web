"""
Bytewax feature pipeline -> online feature cache.

Continuously streams the four refinery features — **CVD, OBI, Microprice and
GEX** — into the online feature cache (the C# SignalR Feature Store at
``/features/update``), which the web terminal and the .NET MAUI mobile app then
read in real time.

Two execution paths share the same logic:

* **Bytewax dataflow** (preferred): a ``SimplePollingSource`` polls the live
  :class:`~core.stream_processor.StreamProcessor` snapshots, a map step attaches
  the latest GEX reading from the local DB, and a ``DynamicSink`` ships each
  record to the online cache. Run with::

      pip install bytewax
      python -m bytewax.run core.feature_pipeline:flow

* **Asyncio fallback** (always available): identical streaming without the
  Bytewax runtime, so the pipeline runs even before bytewax is installed. Run::

      python -m core.feature_pipeline

Bytewax is an optional dependency; this module degrades gracefully when it is
not present (mirroring how :mod:`core.stream_processor` treats MetaTrader5).
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Any, Dict, List

import requests

from core.config import settings
from core.stream_processor import StreamProcessor
from core.state_db import get_state_db
from core.tbbfx_object import make_microstructure_object, pack_tbbfx_object, tbbfx_msgpack_headers

try:
    from bytewax.dataflow import Dataflow
    from bytewax import operators as op
    from bytewax.inputs import SimplePollingSource
    from bytewax.outputs import DynamicSink, StatelessSinkPartition
    BYTEWAX_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only when bytewax is absent
    BYTEWAX_AVAILABLE = False


# ----------------------------------------------------------------------
# Shared processor registry (one StreamProcessor per symbol)
# ----------------------------------------------------------------------
_processors: Dict[str, StreamProcessor] = {}
_processor_thread: threading.Thread | None = None


def _post_feature_envelope(url: str, envelope: Any, headers: Dict[str, str], timeout: float):
    """Send binary MessagePack first, with JSON fallback for legacy local stores."""
    binary_headers = tbbfx_msgpack_headers(headers)
    response = requests.post(url, data=pack_tbbfx_object(envelope), headers=binary_headers, timeout=timeout)
    if response.status_code in (400, 415):
        response = requests.post(url, json=envelope.to_dict(), headers=headers, timeout=timeout)
    return response


def _footprint_rows(proc: StreamProcessor, anchor_price: float, limit: int = 32) -> List[Dict[str, float]]:
    """Serialize nearest footprint rows for the C# confluence refinery."""
    rows: List[Dict[str, float]] = []
    try:
        ordered = sorted(
            proc.footprint.items(),
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


def _ensure_processors(symbols: List[str]) -> Dict[str, StreamProcessor]:
    """Start one background StreamProcessor per symbol (idempotent)."""
    global _processor_thread
    for sym in symbols:
        if sym not in _processors:
            _processors[sym] = StreamProcessor(symbol=sym)

    if _processor_thread is None:
        import asyncio

        def _runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            for proc in _processors.values():
                loop.create_task(proc.run_loop())
            loop.run_forever()

        _processor_thread = threading.Thread(target=_runner, daemon=True, name="tbbfx-streams")
        _processor_thread.start()
    return _processors


def _snapshot(symbols: List[str]) -> List[Dict[str, Any]]:
    """Current CVD / OBI / Microprice (+ latest persisted GEX) per symbol."""
    db = get_state_db()
    out: List[Dict[str, Any]] = []
    for sym, proc in _processors.items():
        if sym not in symbols:
            continue
        latest_gex = db.get_gex_history(sym, limit=1)
        net_gex = latest_gex[0]["net_gex"] if latest_gex else 0.0
        gamma_flip = latest_gex[0]["gamma_flip"] if latest_gex else 0.0
        microprice = float(proc.microprice)
        out.append({
            "symbol": sym,
            "cvd": float(proc.cvd),            # Cumulative Volume Delta
            "microprice": microprice,
            "obi": float(proc.obi),            # Order Block Imbalance
            "net_gex": float(net_gex),         # Gamma Exposure (from DB snapshot)
            "gamma_flip": float(gamma_flip),
            "price": microprice,
            "footprint_rows": _footprint_rows(proc, microprice),
            "depth": proc.depth,
            "timestamp": time.time(),
        })
    return out


def _ship_to_online_cache(record: Dict[str, Any]) -> None:
    """POST one feature record to the SignalR online feature cache."""
    try:
        headers = {}
        feature_key = getattr(settings, "TBBFX_FEATURE_UPDATE_KEY", "")
        if feature_key:
            headers["X-TBBFX-FEATURE-KEY"] = feature_key
        envelope = make_microstructure_object(
            record,
            provider="bytewax_feature_pipeline" if BYTEWAX_AVAILABLE else "async_feature_pipeline",
            route="feature_pipeline.online_cache",
        )
        _post_feature_envelope(
            settings.SIGNALR_URL,
            envelope,
            headers,
            settings.SIGNALR_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - never let a transient network error kill the stream
        pass


# ----------------------------------------------------------------------
# Bytewax components
# ----------------------------------------------------------------------
if BYTEWAX_AVAILABLE:

    class _SnapshotSource(SimplePollingSource):
        """Polls all live StreamProcessor snapshots at a fixed cadence."""

        def __init__(self, symbols: List[str], interval_ms: int = 500):
            super().__init__(interval=timedelta(milliseconds=interval_ms))
            self._symbols = symbols
            _ensure_processors(symbols)

        def next_item(self) -> List[Dict[str, Any]]:
            return _snapshot(self._symbols)

    class _OnlineCacheSink(StatelessSinkPartition):
        def write_batch(self, items: List[Dict[str, Any]]) -> None:
            for record in items:
                _ship_to_online_cache(record)

    class OnlineFeatureStoreSink(DynamicSink):
        def build(self, *args) -> StatelessSinkPartition:  # tolerant of API drift
            return _OnlineCacheSink()

    def build_flow(symbols: List[str] | None = None) -> "Dataflow":
        symbols = symbols or settings.WATCHLIST
        flow = Dataflow("tbbfx_feature_pipeline")
        # Emits a list[record] each tick -> flat_map expands to one record each.
        snapshots = op.input("snapshots", flow, _SnapshotSource(symbols))
        records = op.flat_map("expand", snapshots, lambda batch: batch)
        op.output("online_cache", records, OnlineFeatureStoreSink())
        return flow

    # Module-level handle for `python -m bytewax.run core.feature_pipeline:flow`
    flow = build_flow()


# ----------------------------------------------------------------------
# Asyncio fallback (always available)
# ----------------------------------------------------------------------
def run_fallback(symbols: List[str] | None = None, interval_ms: int = 500) -> None:
    """Stream features to the online cache without the Bytewax runtime."""
    symbols = symbols or settings.WATCHLIST
    _ensure_processors(symbols)
    print(f"[feature_pipeline] Asyncio fallback streaming {symbols} -> {settings.SIGNALR_URL}")
    try:
        while True:
            for record in _snapshot(symbols):
                _ship_to_online_cache(record)
            time.sleep(interval_ms / 1000.0)
    except KeyboardInterrupt:
        print("[feature_pipeline] stopped.")


def run(symbols: List[str] | None = None) -> None:
    if BYTEWAX_AVAILABLE:
        from bytewax.testing import run_main
        print("[feature_pipeline] Running Bytewax dataflow.")
        run_main(build_flow(symbols))
    else:
        print("[feature_pipeline] Bytewax not installed; using asyncio fallback "
              "(`pip install bytewax` to enable the dataflow runtime).")
        run_fallback(symbols)


if __name__ == "__main__":
    run()
