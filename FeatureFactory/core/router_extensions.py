"""OpenBB-inspired command router helpers for FeatureFactory routes."""

from __future__ import annotations

import inspect
import time
from functools import wraps
from typing import Any, Callable, Dict, Optional, Type

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from core.query_params import QueryParams
from core.state_db import get_state_db
from core.tbbfx_object import TbbFxObject, is_tbbfx_object, make_tbbfx_object, pack_tbbfx_object


def _model_to_dict(model: BaseModel) -> Dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")  # type: ignore[attr-defined]
    return model.dict()


def _extract_param_payload(func: Callable[..., Any], args: tuple, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    signature = inspect.signature(func)
    bound = signature.bind_partial(*args, **kwargs)
    payload = dict(bound.arguments)
    payload.pop("self", None)
    payload.pop("request", None)
    return payload


def _validate_query(query_model: Type[QueryParams], payload: Dict[str, Any]) -> QueryParams:
    fields = getattr(query_model, "model_fields", None) or getattr(query_model, "__fields__", {})
    query_payload = {key: value for key, value in payload.items() if key in fields and value is not None}
    return query_model(**query_payload)


def _validation_detail(exc: ValidationError) -> list:
    """Return FastAPI-safe validation errors without raw exception objects."""
    safe_errors = []
    for error in exc.errors():
        item = dict(error)
        if "ctx" in item:
            item["ctx"] = {key: str(value) for key, value in item["ctx"].items()}
        safe_errors.append(item)
    return safe_errors


def _audit_invocation(
    *,
    route: str,
    provider: str,
    query: QueryParams,
    func_name: str,
) -> Optional[str]:
    try:
        query_dict = _model_to_dict(query)
        symbol = str(query_dict.get("symbol") or "SYSTEM").upper()
        get_state_db().record_governance_audit(
            symbol=symbol,
            action_taken="Tool_Invoked",
            data_lineage_source=route,
            active_trade_parameters=query_dict,
            execution_telemetry={
                "provider": provider,
                "function": func_name,
                "router": "tbbfx_router_command",
                "read_only": True,
            },
        )
        return None
    except Exception as exc:  # noqa: BLE001
        return f"Governance audit write failed for {route}: {exc}"


def _wrap_result(
    result: Any,
    *,
    provider: str,
    route: str,
    warnings: list,
    start_ns: int,
    query: QueryParams,
) -> Dict[str, Any]:
    if isinstance(result, TbbFxObject):
        return result.to_dict()
    if is_tbbfx_object(result):
        if warnings:
            result.setdefault("warnings", []).extend(warnings)
        return result
    payload = result
    envelope_warnings = list(warnings)
    if isinstance(payload, dict) and isinstance(payload.get("warnings"), list):
        envelope_warnings.extend(str(item) for item in payload.get("warnings", []))
        payload = dict(payload)
        payload.pop("warnings", None)
    return make_tbbfx_object(
        payload,
        provider=provider,
        route=route,
        warnings=envelope_warnings,
        start_ns=start_ns,
        extra={
            "validated_query": _model_to_dict(query),
            "transport": "json-compatible-envelope",
            "messagepack_supported": True,
        },
    ).to_dict()


def tbbfx_router_command(
    *,
    query_model: Type[QueryParams],
    provider: str,
    route: str,
    messagepack: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Validate, audit, time, and envelope a FastAPI command handler.

    `messagepack=True` is available for binary internal pipelines, while public
    browser-facing routes keep returning the JSON-compatible TbbFxObject shape.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            start_ns = time.perf_counter_ns()
            warnings = []
            try:
                query = _validate_query(query_model, _extract_param_payload(func, args, kwargs))
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc

            audit_warning = _audit_invocation(
                route=route,
                provider=provider,
                query=query,
                func_name=func.__name__,
            )
            if audit_warning:
                warnings.append(audit_warning)

            try:
                result = await func(*args, **kwargs)
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=500, detail=f"{route} failed: {exc}") from exc

            envelope = _wrap_result(
                result,
                provider=provider,
                route=route,
                warnings=warnings,
                start_ns=start_ns,
                query=query,
            )
            return pack_tbbfx_object(envelope) if messagepack else envelope

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            start_ns = time.perf_counter_ns()
            warnings = []
            try:
                query = _validate_query(query_model, _extract_param_payload(func, args, kwargs))
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=_validation_detail(exc)) from exc

            audit_warning = _audit_invocation(
                route=route,
                provider=provider,
                query=query,
                func_name=func.__name__,
            )
            if audit_warning:
                warnings.append(audit_warning)

            try:
                result = func(*args, **kwargs)
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=500, detail=f"{route} failed: {exc}") from exc

            envelope = _wrap_result(
                result,
                provider=provider,
                route=route,
                warnings=warnings,
                start_ns=start_ns,
                query=query,
            )
            return pack_tbbfx_object(envelope) if messagepack else envelope

        return async_wrapper if inspect.iscoroutinefunction(func) else sync_wrapper

    return decorator
