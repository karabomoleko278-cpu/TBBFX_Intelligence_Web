from __future__ import annotations

import os

import msgpack
from fastapi.testclient import TestClient

os.environ.setdefault("ENABLE_STARTUP_SCANNER", "0")
os.environ.setdefault("TBBFX_RUN_NEWS_AGGREGATOR", "0")
os.environ.setdefault("TBBFX_RUN_STREAM_PROCESSORS", "0")

from core.config import settings  # noqa: E402
from core.validation_suite import (  # noqa: E402
    get_validation_suite_snapshot,
    validate_validation_snapshot,
)
from main import app  # noqa: E402


EXPECTED_HASH = "F3D07D7027358A1CFDFB42A77FA1CA5D653E7A76FBF26C1ABF5773A86A22FD0A"
EXPECTED_MODE = "PUBLIC READ-ONLY MONITOR"
EXPECTED_REGIME = "TRAINED SYSTEM SYMBOL PARAMETERS // HARD LOCKED"
EXPECTED_CONTEXT = "TBBFX AI REFINERY // MECHANICAL REGIME ALPHA"
EXPECTED_STATE = "TRAINED_IMMUTABLE_PARAMETER_LOCKED"
FORBIDDEN_KEYS = {
    "risk",
    "risk_pct",
    "risk_percent",
    "risk_percentage",
    "position_size",
    "lot_size",
}


def _assert_scorecard(payload: dict, symbol: str) -> None:
    assert payload["symbol"] == symbol
    assert payload["system_mode"] == EXPECTED_MODE
    assert payload["regime_logic"] == EXPECTED_REGIME
    assert payload["model_context"] == EXPECTED_CONTEXT
    assert payload["parameter_state"] == EXPECTED_STATE
    assert payload["source"]["sha256"] == EXPECTED_HASH
    assert payload["read_only"] is True
    assert payload["execution_capability"] == "NONE"

    scorecard = payload["scorecard"]
    assert scorecard["metrics"]["trades"] > 0
    assert scorecard["metrics"]["profit_factor"] > 0
    assert scorecard["trained_parameters"]
    assert not (FORBIDDEN_KEYS & set(scorecard["trained_parameters"]))


def run() -> None:
    state = validate_validation_snapshot()
    assert state["symbols"] == sorted(settings.WATCHLIST)
    assert state["parameter_state"] == EXPECTED_STATE
    assert state["source_sha256"] == EXPECTED_HASH

    for symbol in settings.WATCHLIST:
        _assert_scorecard(get_validation_suite_snapshot(symbol), symbol)

    client = TestClient(app)
    public_headers = {"X-TBBFX-Public-Gateway": "1"}

    response = client.get(
        "/api/macro/validation-suite?symbol=XAUUSD",
        headers=public_headers,
    )
    assert response.status_code == 200
    envelope = response.json()
    assert envelope["provider"] == "immutable_validation_snapshot"
    assert len(envelope["results"]) == 1
    _assert_scorecard(envelope["results"][0], "XAUUSD")
    assert response.headers["x-tbbfx-system-mode"] == "public-read-only"

    binary_response = client.get(
        "/api/macro/validation-suite?symbol=USDJPY",
        headers={
            **public_headers,
            "Accept": "application/x-msgpack",
        },
    )
    assert binary_response.status_code == 200
    assert binary_response.headers["content-type"].startswith("application/x-msgpack")
    binary_envelope = msgpack.unpackb(binary_response.content, raw=False)
    _assert_scorecard(binary_envelope["results"][0], "USDJPY")

    for method in ("post", "put", "delete", "options"):
        denied = getattr(client, method)(
            "/api/macro/validation-suite?symbol=XAUUSD",
            headers=public_headers,
        )
        assert denied.status_code == 403, (method, denied.status_code, denied.text)
        denial = denied.json()
        assert denial["extra"]["system_mode"] == EXPECTED_MODE

    print("IMMUTABLE PUBLIC VALIDATION SUITE CHECKS PASSED")


if __name__ == "__main__":
    run()
