"""Redis/Upstash implementations of Cache / RateLimiter / Lock — PHASE 4.

Intentionally unimplemented in Phase 0. These land when multi-user makes Redis
load-bearing (§9): a global atomic quota counter shared across all users, a
shared hot cache (most market data is user-independent), and SETNX locks so two
users on the same route don't both scrape.

The single -> multi-user move is swapping these in for the in-proc impls; no
caller changes (the interfaces in cache.py are the contract).
"""

from __future__ import annotations

_PHASE = 4


class _NotYet(NotImplementedError):
    def __init__(self) -> None:
        super().__init__(
            "Redis/Upstash backends land in Phase 4 (multi-user). "
            "Phase 0 uses the in-process impls in store/inproc.py."
        )


class RedisCache:
    def __init__(self, *_, **__):  # pragma: no cover - placeholder
        raise _NotYet()


class RedisRateLimiter:
    def __init__(self, *_, **__):  # pragma: no cover - placeholder
        raise _NotYet()


class RedisLock:
    def __init__(self, *_, **__):  # pragma: no cover - placeholder
        raise _NotYet()
