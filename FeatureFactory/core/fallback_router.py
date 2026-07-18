"""Read-only macro data failover orchestration.

Provider failures are converted into explicit warning states instead of being
allowed to terminate API or polling threads. No execution settings are read or
written by this module.
"""

from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple


Payload = Dict[str, Any]
ProviderCall = Callable[[], Payload]


def _local_timestamp() -> str:
    return datetime.now().astimezone().isoformat()


def _warnings(payload: Payload) -> List[str]:
    return [str(item) for item in payload.get("warnings", []) if str(item).strip()]


def _with_fallback_status(payload: Payload, source: str, failures: List[str]) -> Payload:
    result = copy.deepcopy(payload)
    result["data_status"] = "FALLBACK_REDUNDANCY_ACTIVE"
    result["fallback_source"] = source
    result["status_warning"] = "FALLBACK_REDUNDANCY_ACTIVE"
    result.setdefault("last_updated", _local_timestamp())
    result["warnings"] = list(dict.fromkeys(_warnings(result) + failures))
    result["read_only"] = True
    result["advisory_only"] = True
    result["execution_mutation_allowed"] = False
    return result


def service_offline_payload(route: str, failures: Optional[List[str]] = None) -> Payload:
    updated = _local_timestamp()
    return {
        "status": "unavailable",
        "data_status": "SERVICE_TEMPORARILY_OFFLINE",
        "status_warning": "SERVICE_TEMPORARILY_OFFLINE",
        "last_updated": updated,
        "generated_at": updated,
        "route": route,
        "results": [],
        "warnings": list(dict.fromkeys(failures or [f"{route} providers are temporarily unavailable."])),
        "read_only": True,
        "advisory_only": True,
        "execution_mutation_allowed": False,
    }


class ResilientFallbackRouter:
    """Run primary, secondary and durable providers in a strict order."""

    @staticmethod
    def _call(label: str, provider: Optional[ProviderCall]) -> Tuple[Optional[Payload], Optional[str]]:
        if provider is None:
            return None, None
        try:
            payload = provider()
            if not isinstance(payload, dict):
                raise TypeError(f"{label} returned {type(payload).__name__}, expected dict")
            return payload, None
        except Exception as exc:  # noqa: BLE001 - provider boundaries must never escape
            return None, f"{label} failed: {exc}"

    def execute(
        self,
        *,
        route: str,
        primary: ProviderCall,
        validator: Callable[[Payload], bool],
        secondary: Optional[ProviderCall] = None,
        durable_load: Optional[ProviderCall] = None,
        durable_save: Optional[Callable[[Payload], None]] = None,
        offline_factory: Optional[Callable[[List[str]], Payload]] = None,
    ) -> Payload:
        failures: List[str] = []

        primary_payload, primary_error = self._call("primary provider", primary)
        if primary_error:
            failures.append(primary_error)
        if primary_payload is not None and validator(primary_payload):
            primary_payload = copy.deepcopy(primary_payload)
            primary_payload.setdefault("data_status", "LIVE_PRIMARY")
            primary_payload.setdefault("status_warning", None)
            if durable_save is not None:
                try:
                    durable_save(primary_payload)
                except Exception as exc:  # noqa: BLE001
                    primary_payload.setdefault("warnings", []).append(f"Durable snapshot write failed: {exc}")
            return primary_payload
        if primary_payload is not None:
            failures.extend(_warnings(primary_payload) or ["Primary provider returned no usable observations."])

        secondary_payload, secondary_error = self._call("secondary provider", secondary)
        if secondary_error:
            failures.append(secondary_error)
        if secondary_payload is not None and validator(secondary_payload):
            result = _with_fallback_status(secondary_payload, "secondary_provider", failures)
            if durable_save is not None:
                try:
                    durable_save(result)
                except Exception as exc:  # noqa: BLE001
                    result.setdefault("warnings", []).append(f"Durable snapshot write failed: {exc}")
            return result
        if secondary_payload is not None:
            failures.extend(_warnings(secondary_payload) or ["Secondary provider returned no usable observations."])

        durable_payload, durable_error = self._call("durable local state", durable_load)
        if durable_error:
            failures.append(durable_error)
        if durable_payload is not None and validator(durable_payload):
            return _with_fallback_status(durable_payload, "durable_local_state", failures)

        factory = offline_factory or (lambda errors: service_offline_payload(route, errors))
        return factory(list(dict.fromkeys(failures)))
