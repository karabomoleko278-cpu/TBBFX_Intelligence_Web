"""Read-only access to the signed-off historical validation snapshot."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

from core.config import settings


SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "data" / "validation_suite_snapshot.json"


@lru_cache(maxsize=1)
def _load_snapshot() -> Dict[str, Any]:
    with SNAPSHOT_PATH.open("r", encoding="utf-8") as stream:
        snapshot = json.load(stream)

    symbols = snapshot.get("symbols")
    if not isinstance(symbols, dict):
        raise RuntimeError("Validation snapshot is missing its symbol matrix")

    missing = sorted(set(settings.WATCHLIST) - set(symbols))
    if missing:
        raise RuntimeError(f"Validation snapshot is missing watchlist symbols: {', '.join(missing)}")

    return snapshot


def get_validation_suite_snapshot(symbol: str) -> Dict[str, Any]:
    """Return one immutable, historical OOS scorecard without exposing risk tiers."""
    normalized = str(symbol or "").upper().strip()
    if normalized.endswith("M") and normalized[:-1] in settings.WATCHLIST:
        normalized = normalized[:-1]

    snapshot = _load_snapshot()
    scorecard = snapshot["symbols"].get(normalized)
    if scorecard is None:
        raise ValueError(f"{normalized} is not in the immutable TBBFX validation snapshot")

    return {
        "schema_version": snapshot["schema_version"],
        "model_context": snapshot["model_context"],
        "system_mode": snapshot["system_mode"],
        "regime_logic": snapshot["regime_logic"],
        "parameter_state": snapshot["parameter_state"],
        "snapshot_type": snapshot["snapshot_type"],
        "currency": snapshot["currency"],
        "symbol": normalized,
        "scorecard": copy.deepcopy(scorecard),
        "source": copy.deepcopy(snapshot["source"]),
        "disclaimer": snapshot["disclaimer"],
        "read_only": True,
        "execution_capability": "NONE",
    }


def validate_validation_snapshot() -> Dict[str, Any]:
    """Verification helper used by tests and deployment preflight."""
    snapshot = _load_snapshot()
    return {
        "symbols": sorted(snapshot["symbols"]),
        "parameter_state": snapshot["parameter_state"],
        "source_sha256": snapshot["source"]["sha256"],
    }
