"""StoreBundle — the swappable memory layer, assembled in one place (§9).

The Phase 0 seams (`Cache`, `RateLimiter`, `Lock`, `QuotaGuard`, `JobQueue`)
are gathered into one bundle so the single -> multi-user move is a single
adapter swap. Two backends share the contract:

  - **inproc** (Phase 0-3 default): process-local dict + token bucket +
    threading locks + SQLite quota counter. Zero infrastructure.
  - **redis**  (Phase 4 multi-user): shared hot cache + atomic global quota
    counter + SETNX locks across workers/users.

`build_stores` (in config.py) memoizes one bundle per process so the cache,
rate limiter, and quota counter are genuinely *shared* across runs and across
API workers in the same process — the property that makes "one scrape, both
users served from cache" true.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .cache import Cache, Lock, RateLimiter
from .jobs import JobQueue
from .quota import QuotaGuard


@dataclass
class StoreBundle:
    cache: Cache
    rate_limiter: RateLimiter
    lock: Lock
    quota: Optional[QuotaGuard]
    backend: str  # "inproc" | "redis"
    jobs: Optional[JobQueue] = None
    client: Any = None  # the underlying redis client, if any

    def close(self) -> None:
        if self.jobs is not None:
            self.jobs.close()
        if self.client is not None:
            try:
                self.client.close()
            except Exception:  # pragma: no cover - best effort
                pass
