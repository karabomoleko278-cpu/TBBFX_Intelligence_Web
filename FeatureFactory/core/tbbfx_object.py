"""Standardized TBBFX data container.

This mirrors the useful parts of OpenBB's OBBject response contract while
keeping the payload simple enough for FastAPI, SignalR, the web terminal, and
the MAUI client to consume consistently.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Generic, List, Optional, Type, TypeVar

from pydantic import BaseModel, Field


T = TypeVar("T")


class MarketMicrostructureStandard(BaseModel):
    """Strict transport contract for the live order-flow feature stream."""

    symbol: str = ""
    cvd: float = 0.0
    obi: float = 0.0
    microprice: float = 0.0
    price: float = 0.0
    timestamp: float = Field(default_factory=time.time)
    net_gex: float = 0.0
    gamma_flip: float = 0.0
    footprint_rows: List[Dict[str, float]] = Field(default_factory=list)
    depth: Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _model_to_dict(self)


class OptionsExposureSurface(BaseModel):
    """Strict transport contract for GEX and SVI volatility surface snapshots."""

    symbol: str = ""
    net_gex: Dict[str, float] = Field(default_factory=dict)
    gamma_flip: float = 0.0
    svi_parameters: Dict[str, float] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return _model_to_dict(self)


class TbbFxObject(BaseModel, Generic[T]):
    """Serializable response envelope for all market-data pipeline frames."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    results: List[T] = Field(default_factory=list)
    provider: str = "unknown"
    warnings: List[str] = Field(default_factory=list)
    extra: Dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain dictionary compatible with Pydantic v1 and v2."""
        if hasattr(self, "model_dump"):
            return self.model_dump()
        return self.dict()

    def to_df(self):  # type: ignore[no-untyped-def]
        """Convert the inner results array to a pandas DataFrame on demand."""
        import pandas as pd

        return pd.DataFrame([_json_safe(item) for item in self.results])


def _model_to_dict(model: BaseModel) -> Dict[str, Any]:
    """Pydantic v1/v2 compatible model dump helper."""
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")  # type: ignore[attr-defined]
    return model.dict()


def _coerce_model(item: Any, model_type: Optional[Type[BaseModel]]) -> Any:
    if model_type is None or isinstance(item, model_type):
        return item
    if isinstance(item, BaseModel):
        item = _model_to_dict(item)
    if hasattr(model_type, "model_validate"):
        return model_type.model_validate(item)  # type: ignore[attr-defined]
    return model_type.parse_obj(item)


def _as_results_list(results: Any, model_type: Optional[Type[BaseModel]] = None) -> List[Any]:
    if results is None:
        return []
    if isinstance(results, TbbFxObject):
        return [_coerce_model(item, model_type) for item in results.results]
    if isinstance(results, list):
        return [_coerce_model(item, model_type) for item in results]
    return [_coerce_model(results, model_type)]


def make_tbbfx_object(
    results: Any,
    *,
    provider: str,
    route: str,
    warnings: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
    object_id: Optional[str] = None,
    start_ns: Optional[int] = None,
    result_model: Optional[Type[BaseModel]] = None,
) -> TbbFxObject:
    """Build a standardized response envelope with execution metadata."""
    now_ns = time.perf_counter_ns()
    metadata: Dict[str, Any] = {
        "route": route,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "duration_ns": max(0, now_ns - start_ns) if start_ns else 0,
    }
    if extra:
        metadata.update(extra)

    return TbbFxObject(
        id=object_id or str(uuid.uuid4()),
        results=_as_results_list(results, result_model),
        provider=provider or "unknown",
        warnings=list(warnings or []),
        extra=metadata,
    )


def make_microstructure_object(
    results: Any,
    *,
    provider: str,
    route: str,
    warnings: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
    object_id: Optional[str] = None,
    start_ns: Optional[int] = None,
) -> TbbFxObject[MarketMicrostructureStandard]:
    """Build a validated live order-flow envelope."""
    return make_tbbfx_object(
        results,
        provider=provider,
        route=route,
        warnings=warnings,
        extra=extra,
        object_id=object_id,
        start_ns=start_ns,
        result_model=MarketMicrostructureStandard,
    )


def make_options_exposure_object(
    results: Any,
    *,
    provider: str,
    route: str,
    warnings: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
    object_id: Optional[str] = None,
    start_ns: Optional[int] = None,
) -> TbbFxObject[OptionsExposureSurface]:
    """Build a validated options exposure envelope."""
    return make_tbbfx_object(
        results,
        provider=provider,
        route=route,
        warnings=warnings,
        extra=extra,
        object_id=object_id,
        start_ns=start_ns,
        result_model=OptionsExposureSurface,
    )


def _json_safe(value: Any) -> Any:
    """Recursively convert Pydantic/datetime objects into MessagePack-safe primitives."""
    if isinstance(value, BaseModel):
        return _model_to_dict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


def to_transport_dict(value: Any) -> Dict[str, Any]:
    """Return a primitive dictionary safe for JSON or MessagePack transport."""
    if isinstance(value, TbbFxObject):
        return _json_safe(value)
    if isinstance(value, dict):
        return _json_safe(value)
    return _json_safe(make_tbbfx_object(value, provider="unknown", route="transport.raw"))


def pack_tbbfx_object(value: Any) -> bytes:
    """Serialize a TbbFxObject-compatible payload as MessagePack bytes."""
    import msgpack

    return msgpack.packb(to_transport_dict(value), use_bin_type=True)


def tbbfx_msgpack_headers(headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Apply the binary transport content type without losing auth headers."""
    out = dict(headers or {})
    out["Content-Type"] = "application/x-msgpack"
    return out


def is_tbbfx_object(value: Any) -> bool:
    return isinstance(value, dict) and "results" in value and "provider" in value and "extra" in value


def unwrap_tbbfx_results(value: Any) -> Any:
    """Return the inner payload from a TbbFxObject-like dict if present."""
    if isinstance(value, TbbFxObject):
        return value.results[0] if len(value.results) == 1 else value.results
    if is_tbbfx_object(value):
        results = value.get("results") or []
        return results[0] if len(results) == 1 else results
    return value
