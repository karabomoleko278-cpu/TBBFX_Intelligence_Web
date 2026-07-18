"""Offline verification for TTL caching and macro failover guardrails."""

from __future__ import annotations

import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.cache_manager import TbbFxLocalCache
from core.cot_positioning import CONTRACTS
from core.fallback_router import ResilientFallbackRouter
from core.public_cot_fallback import PublicCftcFallbackFetcher
from core.state_db import StateDatabase


class _FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload) -> None:
        self.payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse(self.payload)


def _verify_cache_expiry_and_copy_isolation() -> None:
    clock = _FakeClock()
    cache = TbbFxLocalCache(clock=clock)
    source = {"results": [{"value": 7}]}
    cache.set("macro:test", source, ttl_seconds=5)

    source["results"][0]["value"] = 99
    first = cache.get("macro:test")
    assert first["results"][0]["value"] == 7
    first["results"][0]["value"] = -1
    assert cache.get("macro:test")["results"][0]["value"] == 7

    clock.advance(5)
    assert cache.get("macro:test") is None
    assert cache.stats()["entries"] == 0


def _verify_cache_stampede_protection() -> None:
    cache = TbbFxLocalCache()
    provider_calls = 0
    call_lock = threading.Lock()

    def resolve():
        nonlocal provider_calls
        cached = cache.get("macro:shared")
        if cached is not None:
            return cached
        with cache.refresh_lock("macro:shared"):
            cached = cache.get("macro:shared")
            if cached is not None:
                return cached
            with call_lock:
                provider_calls += 1
            time.sleep(0.025)
            payload = {"results": [{"value": 42}]}
            cache.set("macro:shared", payload, ttl_seconds=60)
            return payload

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _index: resolve(), range(24)))

    assert provider_calls == 1
    assert all(item["results"][0]["value"] == 42 for item in results)


def _verify_provider_and_durable_fallbacks() -> None:
    router = ResilientFallbackRouter()
    validator = lambda payload: bool(payload.get("results"))

    secondary = router.execute(
        route="api.macro.test",
        primary=lambda: (_ for _ in ()).throw(TimeoutError("primary timeout")),
        secondary=lambda: {"results": [{"value": 10}], "provider": "secondary_fixture"},
        validator=validator,
    )
    assert secondary["data_status"] == "FALLBACK_REDUNDANCY_ACTIVE"
    assert secondary["fallback_source"] == "secondary_provider"
    assert secondary["execution_mutation_allowed"] is False

    with tempfile.TemporaryDirectory() as directory:
        database = StateDatabase(str(Path(directory) / "state.sqlite3"))
        try:
            database.save_macro_fallback_state("macro:test", secondary)
            durable = router.execute(
                route="api.macro.test",
                primary=lambda: (_ for _ in ()).throw(ConnectionError("primary offline")),
                secondary=lambda: (_ for _ in ()).throw(ConnectionError("secondary offline")),
                durable_load=lambda: database.get_macro_fallback_state("macro:test"),
                validator=validator,
            )
            assert durable["data_status"] == "FALLBACK_REDUNDANCY_ACTIVE"
            assert durable["fallback_source"] == "durable_local_state"
            assert durable["results"][0]["value"] == 10
        finally:
            database.close()

    offline = router.execute(
        route="api.macro.test",
        primary=lambda: (_ for _ in ()).throw(ConnectionError("primary offline")),
        secondary=lambda: (_ for _ in ()).throw(ConnectionError("secondary offline")),
        durable_load=lambda: None,
        validator=validator,
    )
    assert offline["status_warning"] == "SERVICE_TEMPORARILY_OFFLINE"
    assert offline["results"] == []
    assert offline["last_updated"]


def _verify_public_cftc_fallback_normalization() -> None:
    rows = []
    for index, contract in enumerate(CONTRACTS.values(), start=1):
        rows.append(
            {
                "report_date_as_yyyy_mm_dd": "2026-07-10T00:00:00.000",
                "cftc_contract_market_code": contract.cftc_code,
                "market_and_exchange_names": contract.market_name,
                "noncomm_positions_long_all": str(100000 + index),
                "noncomm_positions_short_all": str(50000 + index),
                "comm_positions_long_all": str(40000 + index),
                "comm_positions_short_all": str(80000 + index),
            }
        )

    session = _FakeSession(rows)
    normalized = list(PublicCftcFallbackFetcher(session=session)())
    assert len(normalized) == len(CONTRACTS)
    assert {item["symbol"] for item in normalized} == set(CONTRACTS)
    assert all(item["provider"] == "cftc_public_reporting_socrata" for item in normalized)
    assert len(session.calls) == 1


def main() -> None:
    _verify_cache_expiry_and_copy_isolation()
    _verify_cache_stampede_protection()
    _verify_provider_and_durable_fallbacks()
    _verify_public_cftc_fallback_normalization()
    print("ALL CACHE/FALLBACK CHECKS PASSED")


if __name__ == "__main__":
    main()
