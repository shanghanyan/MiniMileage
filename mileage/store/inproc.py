"""In-process implementations of Cache / RateLimiter / Lock (Phase 0-3).

Single-user, single-process: a dict with TTLs, a token bucket, and a
threading.Lock are sufficient. Phase 4 swaps these for Redis/Upstash with no
caller changes.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional


class InProcCache:
    """Dict cache with per-key TTL."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[Any, Optional[float]]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if expires_at is not None and time.monotonic() > expires_at:
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl_seconds: Optional[float] = None) -> None:
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds else None
        with self._lock:
            self._data[key] = (value, expires_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class InProcRateLimiter:
    """Per-key token bucket. Refills `rate` tokens/sec up to `capacity`."""

    def __init__(self, rate: float = 5.0, capacity: float = 10.0) -> None:
        self.rate = rate
        self.capacity = capacity
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, ts)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return True
            self._buckets[key] = (tokens, now)
            return False


class ThreadLock:
    """Process-local locks keyed by string. Redis SETNX replaces this later.

    `acquire` blocks (up to `timeout`) so a concurrent request for the same key
    *waits* for the in-flight fetch instead of skipping: the winner scrapes and
    populates the cache, the waiter then reads it (one scrape, both served, §9).
    """

    def __init__(self, timeout: float = 30.0) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._timeout = timeout

    @contextmanager
    def acquire(self, key: str) -> Iterator[bool]:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        acquired = lock.acquire(blocking=True, timeout=self._timeout)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
