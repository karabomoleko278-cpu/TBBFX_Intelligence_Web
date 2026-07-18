"""Strict query parameter contracts for governed FeatureFactory routes."""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.config import settings


Timeframe = Literal["M5", "M15", "H1", "H4", "D1", "W1"]
MacroImportance = Literal["low", "medium", "high", "critical"]


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


class ValidationSuiteQuery(QueryParams):
    """Read-only lookup for an immutable historical validation snapshot."""

    symbol: str = Field(..., min_length=3, max_length=16)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        symbol = _clean_symbol(value)
        if symbol not in settings.WATCHLIST:
            raise ValueError(f"{symbol} is not in the immutable TBBFX watchlist")
        return symbol


class MacroEconomicQuery(QueryParams):
    """Validated read-only macro calendar filters for the Macro Map."""

    symbol: Optional[str] = Field(default=None, min_length=3, max_length=16)
    importance: Optional[MacroImportance] = None
    country: Optional[str] = Field(default=None, min_length=2, max_length=64)
    limit: int = Field(default=150, ge=1, le=150)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        symbol = _clean_symbol(value)
        if symbol not in settings.WATCHLIST:
            raise ValueError(f"{symbol} is not in the immutable TBBFX watchlist")
        return symbol

    @field_validator("importance", mode="before")
    @classmethod
    def normalize_importance(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return str(value).lower().strip()


class GeopoliticalNewsQuery(QueryParams):
    """Validated read-only geopolitical intelligence filters for the Macro Map."""

    symbol: Optional[str] = Field(default=None, min_length=3, max_length=16)
    keywords: Optional[str] = Field(default=None, max_length=160)
    category: Optional[str] = Field(default=None, max_length=80)
    country: Optional[str] = Field(default=None, min_length=2, max_length=64)
    source: Optional[str] = Field(default=None, max_length=80)
    min_latitude: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    max_latitude: Optional[float] = Field(default=None, ge=-90.0, le=90.0)
    min_longitude: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    max_longitude: Optional[float] = Field(default=None, ge=-180.0, le=180.0)
    limit: int = Field(default=150, ge=1, le=150)

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        symbol = _clean_symbol(value)
        if symbol not in settings.WATCHLIST:
            raise ValueError(f"{symbol} is not in the immutable TBBFX watchlist")
        return symbol

    @model_validator(mode="after")
    def validate_coordinate_bounds(self) -> "GeopoliticalNewsQuery":
        if (
            self.min_latitude is not None
            and self.max_latitude is not None
            and self.min_latitude > self.max_latitude
        ):
            raise ValueError("min_latitude cannot be greater than max_latitude")
        if (
            self.min_longitude is not None
            and self.max_longitude is not None
            and self.min_longitude > self.max_longitude
        ):
            raise ValueError("min_longitude cannot be greater than max_longitude")
        return self


class EmptyQuery(QueryParams):
    """Route has no mutable caller parameters but still receives audit wrapping."""

    pass
