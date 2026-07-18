"""Thread-safe in-memory TTL caching for read-only TBBFX analytics.

The cache deliberately stores presentation data only. It has no dependency on
the trading engine and cannot mutate execution or risk configuration.
"""

from __future__ import annotations

import copy
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional, Tuple


HIGH_FREQUENCY_TTL_SECONDS = 5 * 60
LOW_FREQUENCY_TTL_SECONDS = 12 * 60 * 60
OFFLINE_RETRY_TTL_SECONDS = 60


@dataclass(frozen=True)
class _CacheEntry:
    value: Any
    created_at: float
    expires_at: float


class TbbFxLocalCache:
    """Small process-local TTL cache with defensive copy semantics."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._entries: Dict[str, _CacheEntry] = {}
        self._lock = threading.RLock()
        self._refresh_locks: Dict[str, threading.RLock] = {}
        self._hits = 0
        self._misses = 0

    @contextmanager
    def refresh_lock(self, key: str) -> Iterator[None]:
        """Serialize provider refreshes for one key without blocking other keys.

        This prevents a polling burst from triggering duplicate upstream calls
        after a shared entry expires. Locks remain registered for the process
        lifetime so callers can never race against lock removal.
        """
        cache_key = str(key)
        with self._lock:
            lock = self._refresh_locks.setdefault(cache_key, threading.RLock())
        with lock:
            yield

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        ttl = max(0.001, float(ttl_seconds))
        now = self._clock()
        entry = _CacheEntry(copy.deepcopy(value), now, now + ttl)
        with self._lock:
            self._entries[str(key)] = entry

    def get(self, key: str, default: Any = None) -> Any:
        value, _ = self.get_with_metadata(key)
        return default if value is None else value

    def get_with_metadata(self, key: str) -> Tuple[Any, Optional[Dict[str, float]]]:
        cache_key = str(key)
        now = self._clock()
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is None:
                self._misses += 1
                return None, None
            if entry.expires_at <= now:
                self._entries.pop(cache_key, None)
                self._misses += 1
                return None, None
            self._hits += 1
            metadata = {
                "age_seconds": max(0.0, now - entry.created_at),
                "ttl_remaining_seconds": max(0.0, entry.expires_at - now),
            }
            return copy.deepcopy(entry.value), metadata

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._entries.pop(str(key), None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def prune_expired(self) -> int:
        now = self._clock()
        with self._lock:
            expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
            for key in expired:
                self._entries.pop(key, None)
            return len(expired)

    def stats(self) -> Dict[str, int]:
        self.prune_expired()
        with self._lock:
            return {
                "entries": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
            }


_LOCAL_CACHE = TbbFxLocalCache()


def get_local_cache() -> TbbFxLocalCache:
    return _LOCAL_CACHE
