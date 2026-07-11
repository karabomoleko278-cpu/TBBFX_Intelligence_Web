"""Global Transform-Extract-Transform fetcher primitives.

The Fetcher[Q, R] abstraction keeps provider-specific quirks out of the rest of
the trading desk. Each provider gets a chance to transform a unified query,
extract raw data, then normalize it into a stable model wrapped by TbbFxObject.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Generic, Iterable, List, Optional, TypeVar

from pydantic import BaseModel

from core.tbbfx_object import TbbFxObject, make_tbbfx_object


Q = TypeVar("Q")
R = TypeVar("R")


class Fetcher(BaseModel, Generic[Q, R], ABC):
    """Provider-agnostic Transform-Extract-Transform fetcher base."""

    name: str
    providers: List[str]

    class Config:
        arbitrary_types_allowed = True

    @abstractmethod
    def transform_query(self, params: Dict[str, Any], provider: str) -> Q:
        """Translate unified TBBFX params into a provider-specific query."""

    @abstractmethod
    async def aextract_data(self, query: Q, credentials: Dict[str, Any], provider: str) -> Any:
        """Extract raw provider data asynchronously."""

    @abstractmethod
    def transform_data(self, raw_data: Any, provider: str) -> R:
        """Normalize provider output into a stable response model."""

    async def fetch(
        self,
        params: Dict[str, Any],
        *,
        credentials: Optional[Dict[str, Any]] = None,
        provider_order: Optional[Iterable[str]] = None,
    ) -> TbbFxObject:
        """Run the TET pipeline with automatic provider failover."""
        start_ns = time.perf_counter_ns()
        warnings: List[str] = []
        credentials = credentials or {}
        candidates = list(provider_order or self.providers)
        if not candidates:
            candidates = ["unconfigured"]

        for provider in candidates:
            try:
                query = self.transform_query(params, provider)
                raw = await self.aextract_data(query, credentials, provider)
                normalized = self.transform_data(raw, provider)
                return make_tbbfx_object(
                    normalized,
                    provider=provider,
                    route=self.name,
                    warnings=warnings,
                    start_ns=start_ns,
                )
            except Exception as exc:  # noqa: BLE001 - fail over to the next provider
                warnings.append(f"{provider}: {type(exc).__name__}: {exc}")
                await asyncio.sleep(0)

        return make_tbbfx_object(
            [],
            provider="unavailable",
            route=self.name,
            warnings=warnings or ["No providers configured."],
            start_ns=start_ns,
        )


class SymbolQuery(BaseModel):
    symbol: str
    timeframe: str = "M5"
    count: int = 240


class GexQuery(BaseModel):
    symbol: str
    lookback_hours: int = 24


class LiveTelemetryQuery(BaseModel):
    symbol: str
