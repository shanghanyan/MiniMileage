"""Memory / storage layer (Cursor-Mileage-Plan.md §9).

Three separable concerns, all behind interfaces FROM PHASE 0 so the single ->
multi-user move is an adapter swap, not a rewrite:

  Concern        Interface       Phase 0-3 impl        Phase 4 impl
  -----------    -----------     ------------------    ----------------
  Durable truth  Repository      SQLite (sqlite_repo)  Turso / Supabase
  Hot cache      Cache           in-proc dict + TTL    Redis / Upstash
  Rate limiting  RateLimiter     in-proc token bucket  Redis atomic counter
  Coordination   Lock            threading.Lock        Redis SETNX
"""

from .cache import Cache, Lock, RateLimiter
from .repo import Repository
from .quota import QuotaGuard
from .inproc import InProcCache, InProcRateLimiter, ThreadLock
from .jobs import InProcJobQueue, Job, JobQueue
from .sqlite_repo import SQLiteRepository, SqliteQuotaGuard
from .stores import StoreBundle
from .redis_impl import (
    RedisCache,
    RedisLock,
    RedisQuotaGuard,
    RedisRateLimiter,
    RedisUnavailable,
    redis_from_url,
)

__all__ = [
    "Cache",
    "Lock",
    "RateLimiter",
    "QuotaGuard",
    "Repository",
    "InProcCache",
    "InProcRateLimiter",
    "ThreadLock",
    "Job",
    "JobQueue",
    "InProcJobQueue",
    "SQLiteRepository",
    "SqliteQuotaGuard",
    "StoreBundle",
    "RedisCache",
    "RedisRateLimiter",
    "RedisLock",
    "RedisQuotaGuard",
    "RedisUnavailable",
    "redis_from_url",
]
