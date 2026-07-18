"""Public ingress guardrails for the FeatureFactory API.

The limiter is intentionally process-local. Cloudflare and Nginx provide the
outer limits in production; this layer remains the final application-level
defence if either proxy is bypassed.
"""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict, Mapping, Tuple

from fastapi import Request


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class TbbFxIpRateLimiter:
    """Thread-safe sliding-window limiter partitioned by client IP."""

    def __init__(self, permit_limit: int = 60, window_seconds: int = 60) -> None:
        self.permit_limit = max(1, int(permit_limit))
        self.window_seconds = max(1, int(window_seconds))
        self._events: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, partition_key: str, now: float | None = None) -> RateLimitDecision:
        current = time.monotonic() if now is None else float(now)
        cutoff = current - self.window_seconds

        with self._lock:
            events = self._events[partition_key]
            while events and events[0] <= cutoff:
                events.popleft()

            if len(events) >= self.permit_limit:
                retry_after = max(1, int(self.window_seconds - (current - events[0])) + 1)
                return RateLimitDecision(False, self.permit_limit, 0, retry_after)

            events.append(current)
            remaining = max(0, self.permit_limit - len(events))
            return RateLimitDecision(True, self.permit_limit, remaining, 0)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


def _is_trusted_proxy(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return parsed.is_loopback or parsed.is_private


def resolve_forwarded_ip(direct: str, headers: Mapping[str, str]) -> str:
    """Resolve a forwarded client address only behind a trusted local proxy."""
    if _is_trusted_proxy(direct):
        cloudflare_ip = headers.get("cf-connecting-ip", "").strip()
        if cloudflare_ip:
            return cloudflare_ip

        forwarded = headers.get("x-forwarded-for", "").split(",", 1)[0].strip()
        if forwarded:
            return forwarded

    return direct


def resolve_client_ip(request: Request) -> str:
    """Resolve Cloudflare/proxy IPs only when the direct peer is trusted."""
    direct = request.client.host if request.client else "unknown"
    return resolve_forwarded_ip(direct, request.headers)


_RATE_LIMITED_PREFIXES: Tuple[str, ...] = (
    "/api/macro/",
    "/api/features/",
    "/api/candles/",
    "/api/momentum",
    "/api/greeks/",
    "/api/volprofile/",
    "/api/exposure/",
    "/api/orderflow/",
)


def is_public_market_path(path: str) -> bool:
    normalized = path.lower()
    return any(normalized.startswith(prefix) for prefix in _RATE_LIMITED_PREFIXES)


_LOW_FREQUENCY_CACHE_PATHS = {
    "/api/macro/liquidity-index",
    "/api/macro/cot-positioning",
    "/api/macro/yield-curve",
    "/api/macro/validation-suite",
}

_HIGH_FREQUENCY_CACHE_PATHS = {
    "/api/macro/yield-spreads",
    "/api/macro/sentiment",
    "/api/macro/calendar",
    "/api/macro/geopolitical-feed",
    "/api/macro/geospatial-nodes",
}


def cache_policy_for(request: Request) -> Tuple[str, str | None]:
    """Return browser and CDN cache directives for an API request."""
    path = request.url.path.lower().rstrip("/")
    has_credentials = (
        "key" in request.query_params
        or bool(request.headers.get("authorization"))
        or bool(request.headers.get("x-tbbfx-feature-key"))
        or bool(request.headers.get("x-tbbfx-key"))
    )
    if has_credentials:
        return "private, no-store", None

    if path in _LOW_FREQUENCY_CACHE_PATHS:
        return (
            "public, max-age=300, stale-while-revalidate=60",
            "public, max-age=43200, stale-while-revalidate=600, stale-if-error=86400",
        )

    if path in _HIGH_FREQUENCY_CACHE_PATHS:
        return (
            "public, max-age=30, stale-while-revalidate=30",
            "public, max-age=300, stale-while-revalidate=60, stale-if-error=900",
        )

    if is_public_market_path(path):
        return "private, no-store", None

    return "no-store", None
