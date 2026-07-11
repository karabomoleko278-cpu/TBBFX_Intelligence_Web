"""Strict query parameter contracts for governed FeatureFactory routes."""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.config import settings


Timeframe = Literal["M5", "M15", "H1", "H4", "D1", "W1"]


def _clean_symbol(value: str) -> str:
    symbol = str(value or "").upper().strip()
    if symbol.endswith("M") and symbol[:-1] in settings.WATCHLIST:
        symbol = symbol[:-1]
    return symbol


class QueryParams(BaseModel):
    """Immutable base model for OpenBB-style command input validation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketDataQuery(QueryParams):
    """Validated market-data extraction parameters."""

    symbol: str = Field(..., min_length=3, max_length=16)
    timeframe: Optional[Timeframe] = Field(default=None)
    count: int = Field(default=240, ge=20, le=1200)
    limit: int = Field(default=200, ge=1, le=5000)
    buckets: int = Field(default=26, ge=5, le=100)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        symbol = _clean_symbol(value)
        if symbol not in settings.WATCHLIST:
            raise ValueError(f"{symbol} is not in the immutable TBBFX watchlist")
        return symbol

    @field_validator("timeframe", mode="before")
    @classmethod
    def normalize_timeframe(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        tf = str(value).upper().strip()
        return {"1H": "H1", "4H": "H4", "1D": "D1", "1W": "W1"}.get(tf, tf)


class GovernanceQuery(QueryParams):
    """Validated risk-governance command parameters."""

    symbol: str = Field(..., min_length=3, max_length=16)
    proposed_risk_pct: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    execution_stance: Optional[
        Literal["CONSERVATIVE_SCALP", "BREAKOUT_LONG", "DEFENSIVE_CASH", "LIQUIDITY_PROVISION"]
    ] = None
    tool_name: Optional[str] = Field(default=None, max_length=80)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        symbol = _clean_symbol(value)
        if symbol not in settings.WATCHLIST:
            raise ValueError(f"{symbol} is not in the immutable TBBFX watchlist")
        return symbol

    @field_validator("proposed_risk_pct", mode="before")
    @classmethod
    def normalize_risk(cls, value: Optional[float]) -> Optional[float]:
        if value is None:
            return None
        risk = float(value)
        return risk / 100.0 if risk > 1.0 else risk


class OptimizationQuery(QueryParams):
    """Validated optimizer command parameters."""

    symbol: Optional[str] = Field(default=None, min_length=3, max_length=16)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        symbol = _clean_symbol(value)
        if symbol not in settings.WATCHLIST:
            raise ValueError(f"{symbol} is not in the immutable TBBFX watchlist")
        return symbol


class EmptyQuery(QueryParams):
    """Route has no mutable caller parameters but still receives audit wrapping."""

    pass
