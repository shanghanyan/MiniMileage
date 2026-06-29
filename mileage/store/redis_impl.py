"""Redis/Upstash implementations of Cache / RateLimiter / Lock / QuotaGuard — PHASE 4.

These land when multi-user makes Redis load-bearing (§9):

  - a **shared hot cache** — most market data (charts, ratios, fares, award
    space) is user-independent, so one user's lookup serves the rest;
  - a **global atomic quota counter** shared across all users — free tiers cap
    *your key*, not per-user, so the counter must be cross-process;
  - **SETNX distributed locks** so two users on the same route don't both scrape.

The single -> multi-user move is swapping these in for the in-proc impls; no
caller changes — the interfaces in `cache.py` / `quota.py` are the contract.

`redis` is an optional dependency (`pip install -e .[multiuser]`). Constructing
any adapter without it raises a clear error; the in-proc impls remain the
zero-dependency default for single-user/local runs.
"""

from __future__ import annotations

import pickle
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .quota import current_month

try:  # optional dependency — only needed for the multi-user backend
    import redis as _redis
except ImportError:  # pragma: no cover - exercised only without redis installed
    _redis = None


_PREFIX = "mileage"


class RedisUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "The redis package is not installed. Install the multi-user extra "
            "(`pip install -e .[multiuser]`) or unset MILEAGE_REDIS_URL to use "
            "the in-process backend."
        )


def redis_from_url(url: str, **kwargs: Any):
    """Build a redis client from a URL (redis:// or rediss:// for Upstash TLS)."""
    if _redis is None:
        raise RedisUnavailable()
    return _redis.Redis.from_url(url, **kwargs)


def _require(client: Any) -> Any:
    if _redis is None:
        raise RedisUnavailable()
    if client is None:
        raise RedisUnavailable()
    return client


# --------------------------------------------------------------------------- #
# Cache — shared hot cache (§9). Values are pickled so the contract is identical
# to InProcCache: callers get back the same Python objects (lists of Quotes).
# --------------------------------------------------------------------------- #
class RedisCache:
    def __init__(self, client: Any, *, namespace: str = "cache") -> None:
        self._r = _require(client)
        self._ns = f"{_PREFIX}:{namespace}:"

    def _key(self, key: str) -> str:
        return self._ns + key

    def get(self, key: str) -> Optional[Any]:
        raw = self._r.get(self._key(key))
        if raw is None:
            return None
        try:
            return pickle.loads(raw)
        except Exception:
            # Corrupt / incompatible payload: treat as a miss, don't crash a run.
            self._r.delete(self._key(key))
            return None

    def set(self, key: str, value: Any, ttl_seconds: Optional[float] = None) -> None:
        blob = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
        if ttl_seconds:
            self._r.set(self._key(key), blob, ex=int(ttl_seconds))
        else:
            self._r.set(self._key(key), blob)

    def delete(self, key: str) -> None:
        self._r.delete(self._key(key))

    def clear(self) -> None:
        """Drop every key in this cache namespace (tests / admin)."""
        for k in self._r.scan_iter(match=self._ns + "*"):
            self._r.delete(k)


# --------------------------------------------------------------------------- #
# RateLimiter — global token bucket via an atomic Lua script (§9). Shared across
# workers so the free-tier budget is enforced once, not per-process.
# --------------------------------------------------------------------------- #
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)
local allowed = 0
if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, 120000)
return allowed
"""


class RedisRateLimiter:
    def __init__(
        self,
        client: Any,
        *,
        rate: float = 5.0,
        capacity: float = 10.0,
        namespace: str = "rl",
    ) -> None:
        self._r = _require(client)
        self.rate = rate
        self.capacity = capacity
        self._ns = f"{_PREFIX}:{namespace}:"
        self._script = self._r.register_script(_TOKEN_BUCKET_LUA)

    def allow(self, key: str) -> bool:
        allowed = self._script(
            keys=[self._ns + key],
            args=[self.rate, self.capacity, time.time()],
        )
        return bool(allowed)


# --------------------------------------------------------------------------- #
# Lock — SETNX with TTL (§9). De-dupes concurrent scrapes across workers: one
# fetches, the others fall through to re-read the shared cache.
# --------------------------------------------------------------------------- #
class RedisLock:
    def __init__(
        self,
        client: Any,
        *,
        ttl_seconds: float = 30.0,
        wait_seconds: float = 30.0,
        poll_seconds: float = 0.05,
        namespace: str = "lock",
    ) -> None:
        self._r = _require(client)
        self._ttl_ms = int(ttl_seconds * 1000)
        self._wait = wait_seconds
        self._poll = poll_seconds
        self._ns = f"{_PREFIX}:{namespace}:"

    @contextmanager
    def acquire(self, key: str) -> Iterator[bool]:
        token = uuid.uuid4().hex
        full = self._ns + key
        # Retry SETNX so a concurrent worker *waits* for the in-flight fetch
        # rather than double-scraping; the waiter then reads the warm cache.
        deadline = time.monotonic() + self._wait
        acquired = bool(self._r.set(full, token, nx=True, px=self._ttl_ms))
        while not acquired and time.monotonic() < deadline:
            time.sleep(self._poll)
            acquired = bool(self._r.set(full, token, nx=True, px=self._ttl_ms))
        try:
            yield acquired
        finally:
            if acquired:
                # Release only if we still own it (avoid clobbering after TTL).
                try:
                    if self._r.get(full) == token.encode():
                        self._r.delete(full)
                except Exception:  # pragma: no cover - best-effort release
                    pass


# --------------------------------------------------------------------------- #
# QuotaGuard — global monthly counter (§9). This is Redis's core multi-user job:
# the free-tier budget is shared across ALL users, so the counter must be a
# single atomic, cross-process value (SQLite counters degrade under concurrent
# multi-process writes).
# --------------------------------------------------------------------------- #
class RedisQuotaGuard:
    def __init__(self, client: Any, *, namespace: str = "quota") -> None:
        self._r = _require(client)
        self._ns = f"{_PREFIX}:{namespace}:"

    def _key(self, provider: str, month: Optional[str] = None) -> str:
        return f"{self._ns}{provider}:{month or current_month()}"

    def remaining(self, provider: str, monthly_limit: Optional[int]) -> Optional[int]:
        if monthly_limit is None:
            return None
        return max(0, monthly_limit - self.used(provider))

    def consume(self, provider: str, count: int = 1) -> None:
        key = self._key(provider)
        pipe = self._r.pipeline()
        pipe.incrby(key, count)
        # Expire ~63 days out so a stale month self-cleans without a cron.
        pipe.expire(key, 63 * 24 * 3600)
        pipe.execute()

    def used(self, provider: str) -> int:
        raw = self._r.get(self._key(provider))
        return int(raw) if raw else 0

    def reset(self, provider: str, month: Optional[str] = None) -> None:
        self._r.delete(self._key(provider, month))

    def exhaust(self, provider: str, monthly_limit: int) -> None:
        """Set usage to the limit (simulate exhaustion for demos/tests)."""
        self._r.set(self._key(provider), monthly_limit, ex=63 * 24 * 3600)
