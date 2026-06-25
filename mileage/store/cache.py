"""Storage interfaces: Cache, RateLimiter, Lock.

These are the seams that let the single-user, in-process Phase 0 become the
multi-user, Redis-backed Phase 4 without touching callers (§9).
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class Cache(Protocol):
    """Hot cache keyed by (provider, route, date, cabin) with TTL = cadence."""

    def get(self, key: str) -> Optional[Any]: ...

    def set(self, key: str, value: Any, ttl_seconds: Optional[float] = None) -> None: ...

    def delete(self, key: str) -> None: ...


@runtime_checkable
class RateLimiter(Protocol):
    """Per-provider token bucket. In-proc now; Redis atomic counter later."""

    def allow(self, key: str) -> bool:
        """Try to consume one token. True if permitted, False if throttled."""
        ...


@runtime_checkable
class Lock(Protocol):
    """Coordination to de-dupe concurrent scrapes. no-op/threading now; SETNX later."""

    def acquire(self, key: str) -> AbstractContextManager[bool]:
        """Context manager yielding True if the lock was acquired."""
        ...
