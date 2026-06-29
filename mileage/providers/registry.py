"""Provider registry — federated query resolution with quota + cache (§5, Phase 2).

Resolution per layer:
  capability match -> not disabled -> healthy -> cache-first (2-day TTL) ->
  remaining quota -> rate limit -> fetch -> consume quota on miss only.

  - FARES / SCHEDULES  -> FALLBACK: try in trust order, stop at first hit.
  - AWARD / CHARTS     -> POOL: gather from all healthy providers for cross-check.

Cache hits cost zero quota — interactive re-runs within the refresh cadence are
free (§5 cadence: ~2 days -> ~15 calls/route/month).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Optional

from .. import obs
from ..domain.models import Layer
from ..store.cache import Cache, Lock, RateLimiter
from ..store.inproc import InProcCache, InProcRateLimiter, ThreadLock
from ..store.quota import QuotaGuard
from .base import Provider, ProviderHealth, Query, Quote
from .federation import FederationConfig

log = logging.getLogger("mileage.registry")

_POOL_LAYERS = {Layer.AWARD, Layer.CHARTS}
DEFAULT_TTL_SECONDS = 2 * 24 * 3600


@dataclass
class FetchEvent:
    """One provider attempt during federation (for status / demo-degrade)."""

    provider: str
    layer: str
    cache_hit: bool = False
    skipped: bool = False
    skip_reason: Optional[str] = None  # disabled | down | quota | rate_limit | error
    quotes: int = 0


@dataclass
class RegistryStats:
    cache_hits: int = 0
    cache_misses: int = 0
    quota_skips: int = 0
    events: list[FetchEvent] = field(default_factory=list)


class ProviderRegistry:
    def __init__(
        self,
        providers: Optional[list[Provider]] = None,
        *,
        cache: Optional[Cache] = None,
        rate_limiter: Optional[RateLimiter] = None,
        quota: Optional[QuotaGuard] = None,
        lock: Optional[Lock] = None,
        federation: Optional[FederationConfig] = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        disabled: Optional[set[str]] = None,
        stores: object = None,
    ) -> None:
        self._providers: list[Provider] = []
        self.cache: Cache = cache or InProcCache()
        self.rate_limiter: RateLimiter = rate_limiter or InProcRateLimiter()
        self.quota: Optional[QuotaGuard] = quota
        self.federation: Optional[FederationConfig] = federation
        self.ttl_seconds = ttl_seconds
        self.disabled: set[str] = disabled or set()
        # The memory-layer bundle (cache/limiter/lock/quota/jobs), if assembled
        # via build_registry. Exposes the background job queue to callers (§9.4).
        self.stores = stores
        # Coordination across concurrent fetches; shared (Redis SETNX) in
        # multi-user mode so two users on the same route don't both scrape (§9).
        self._lock: Lock = lock or ThreadLock()
        self._stats = RegistryStats()
        # Stats are updated from multiple worker threads (concurrent users);
        # guard the counters so they don't lose increments under contention.
        self._stats_lock = threading.Lock()
        for p in providers or []:
            self.register(p)

    def register(self, provider: Provider) -> None:
        self._providers.append(provider)

    def reset_stats(self) -> None:
        self._stats = RegistryStats()

    @property
    def stats(self) -> RegistryStats:
        return self._stats

    def providers_for(self, layer: Layer) -> list[Provider]:
        eligible = [
            p
            for p in self._providers
            if layer in p.capabilities()
            and p.name not in self.disabled
            and p.health() != ProviderHealth.DOWN
        ]

        def _trust(p: Provider) -> float:
            if self.federation:
                spec = self.federation.spec(p.name)
                if spec:
                    return spec.trust_for(layer.value)
            return p.trust

        return sorted(eligible, key=_trust, reverse=True)

    def remaining_quota(self, provider_name: str) -> Optional[int]:
        if self.quota is None or self.federation is None:
            p = next((x for x in self._providers if x.name == provider_name), None)
            return p.remaining_quota() if p else None
        limit = self.federation.monthly_quota(provider_name)
        if limit is None:
            return None
        return self.quota.remaining(provider_name, limit)

    def _cache_key(self, provider: Provider, q: Query) -> str:
        programs = ",".join(sorted(q.programs)) if q.programs else "*"
        return f"{provider.name}:{q.layer.value}:{q.route.key()}:{q.currency}:{programs}"

    def _record(
        self,
        provider: str,
        layer: Layer,
        *,
        cache_hit: bool = False,
        skipped: bool = False,
        skip_reason: Optional[str] = None,
        quotes: int = 0,
    ) -> None:
        with self._stats_lock:
            if cache_hit:
                self._stats.cache_hits += 1
            elif not skipped:
                self._stats.cache_misses += 1
            if skip_reason == "quota":
                self._stats.quota_skips += 1
            self._stats.events.append(
                FetchEvent(
                    provider=provider,
                    layer=layer.value,
                    cache_hit=cache_hit,
                    skipped=skipped,
                    skip_reason=skip_reason,
                    quotes=quotes,
                )
            )

    def _fetch_one(self, provider: Provider, q: Query) -> list[Quote]:
        # Each provider attempt is a TOOL span (RETRIEVER for award/chart
        # lookups — they retrieve seat/ratio data) so every scrape / API call
        # shows up in the trace with its query and result count (§10).
        kind = (
            obs.KIND_RETRIEVER
            if q.layer in _POOL_LAYERS
            else obs.KIND_TOOL
        )
        with obs.span(
            provider.name,
            kind,
            input_value=f"{q.layer.value} {q.route.key()} ({q.currency})",
        ) as s:
            quotes = self._fetch_one_inner(provider, q)
            obs.set_output(s, f"{len(quotes)} quote(s)")
            return quotes

    def _fetch_one_inner(self, provider: Provider, q: Query) -> list[Quote]:
        key = self._cache_key(provider, q)

        # Cache-first: hits cost zero quota (§5).
        cached = self.cache.get(key)
        if cached is not None:
            log.debug("cache hit %s", key)
            self._record(provider.name, q.layer, cache_hit=True, quotes=len(cached))
            return cached

        # Monthly quota guard.
        if self.quota is not None and self.federation is not None:
            limit = self.federation.monthly_quota(provider.name)
            if limit is not None:
                remaining = self.quota.remaining(provider.name, limit)
                if remaining is not None and remaining <= 0:
                    log.info("skip %s: monthly quota exhausted", provider.name)
                    self._record(
                        provider.name, q.layer, skipped=True, skip_reason="quota"
                    )
                    return []

        if not self.rate_limiter.allow(provider.name):
            log.info("skip %s: rate limited", provider.name)
            self._record(
                provider.name, q.layer, skipped=True, skip_reason="rate_limit"
            )
            return []

        # De-dupe concurrent fetches for the same key (§9 Lock interface).
        with self._lock.acquire(key) as acquired:
            if not acquired:
                # Another thread holds the lock; re-check cache.
                cached = self.cache.get(key)
                if cached is not None:
                    self._record(
                        provider.name, q.layer, cache_hit=True, quotes=len(cached)
                    )
                    return cached
                self._record(
                    provider.name, q.layer, skipped=True, skip_reason="locked"
                )
                return []

            # Double-check cache after acquiring lock.
            cached = self.cache.get(key)
            if cached is not None:
                self._record(
                    provider.name, q.layer, cache_hit=True, quotes=len(cached)
                )
                return cached

            try:
                quotes = provider.fetch(q)
            except Exception as exc:
                log.warning("provider %s failed: %s", provider.name, exc)
                self._record(
                    provider.name, q.layer, skipped=True, skip_reason="error"
                )
                return []

            # Populate cache + consume quota *inside* the lock so a concurrent
            # waiter sees the result on its post-lock re-check and is served from
            # cache instead of double-scraping (§9: one scrape, both served).
            if self.quota is not None:
                self.quota.consume(provider.name, 1)
            self.cache.set(key, quotes, ttl_seconds=self.ttl_seconds)
            self._record(provider.name, q.layer, quotes=len(quotes))
            return quotes

    def fetch(self, q: Query) -> list[Quote]:
        """Federate one query across all providers serving its layer."""
        providers = self.providers_for(q.layer)
        if not providers:
            log.info("no provider serves layer %s", q.layer.value)
            return []

        pool = q.layer in _POOL_LAYERS
        collected: list[Quote] = []
        for provider in providers:
            quotes = self._fetch_one(provider, q)
            if quotes:
                collected.extend(quotes)
                if not pool:
                    break
        return collected

    def provider_status(self) -> list[dict]:
        """Snapshot for `mileage providers` — health, quota, federation order."""
        rows: list[dict] = []
        for p in self._providers:
            limit = (
                self.federation.monthly_quota(p.name) if self.federation else None
            )
            used = self.quota.used(p.name) if self.quota else 0
            remaining = self.remaining_quota(p.name)
            rows.append(
                {
                    "name": p.name,
                    "health": p.health().value,
                    "trust": p.trust,
                    "layers": sorted(x.value for x in p.capabilities()),
                    "disabled": p.name in self.disabled,
                    "monthly_limit": limit,
                    "used": used,
                    "remaining": remaining,
                }
            )
        return sorted(rows, key=lambda r: r["trust"], reverse=True)
