from __future__ import annotations

from starlette.requests import Request

from core.rate_limiter import TbbFxIpRateLimiter, cache_policy_for, resolve_client_ip


def _request(path: str, headers: dict[str, str] | None = None, query: bytes = b"") -> Request:
    raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query,
        "headers": raw_headers,
        "client": ("127.0.0.1", 41000),
        "server": ("localhost", 8000),
    }
    return Request(scope)


def run() -> None:
    limiter = TbbFxIpRateLimiter(permit_limit=2, window_seconds=60)
    assert limiter.check("198.51.100.5", now=0).allowed
    assert limiter.check("198.51.100.5", now=1).allowed
    denied = limiter.check("198.51.100.5", now=2)
    assert not denied.allowed and denied.retry_after_seconds > 0
    assert limiter.check("198.51.100.6", now=2).allowed
    assert limiter.check("198.51.100.5", now=61).allowed

    proxied = _request("/api/macro/calendar", {"CF-Connecting-IP": "203.0.113.9"})
    assert resolve_client_ip(proxied) == "203.0.113.9"

    browser, cdn = cache_policy_for(_request("/api/macro/liquidity-index"))
    assert "max-age=300" in browser and cdn and "max-age=43200" in cdn

    browser, cdn = cache_policy_for(_request("/api/features/XAUUSD"))
    assert browser == "private, no-store" and cdn is None

    browser, cdn = cache_policy_for(
        _request("/api/macro/liquidity-index", query=b"key=operator-secret")
    )
    assert browser == "private, no-store" and cdn is None
    print("RATE LIMIT AND CACHE POLICY CHECKS PASSED")


if __name__ == "__main__":
    run()
