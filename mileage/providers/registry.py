"""Provider registry — federate a query by capability (§5).

Resolution per layer: capability match -> healthy -> cache-first ->
remaining quota -> trust order. Same-capability providers are tried in order.

  - FARES / SCHEDULES  -> FALLBACK mode: try in trust order, stop at first hit
                          (stretch free quota; don't double-spend).
  - AWARD / CHARTS     -> POOL mode: gather from all healthy providers so the
                          verification core can cross-check independent sources.

A response cache (keyed by provider+route+cabin+layer) with TTL = refresh
cadence means interactive re-runs cost zero quota.
"""

from __future__ import annotations

import logging
from typing import Optional

from ..domain.models import Layer
from ..store.cache import Cache, RateLimiter
from ..store.inproc import InProcCache, InProcRateLimiter
from .base import Provider, ProviderHealth, Query, Quote

log = logging.getLogger("mileage.registry")

# Layers whose providers should be pooled (for cross-check) vs. used as fallbacks.
_POOL_LAYERS = {Layer.AWARD, Layer.CHARTS}

# Default refresh cadence (~2 days) -> ~15 calls/route/month per provider (§5).
DEFAULT_TTL_SECONDS = 2 * 24 * 3600


class ProviderRegistry:
    def __init__(
        self,
        providers: Optional[list[Provider]] = None,
        *,
        cache: Optional[Cache] = None,
        rate_limiter: Optional[RateLimiter] = None,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._providers: list[Provider] = []
        self.cache: Cache = cache or InProcCache()
        self.rate_limiter: RateLimiter = rate_limiter or InProcRateLimiter()
        self.ttl_seconds = ttl_seconds
        for p in providers or []:
            self.register(p)

    def register(self, provider: Provider) -> None:
        self._providers.append(provider)

    def providers_for(self, layer: Layer) -> list[Provider]:
        eligible = [
            p
            for p in self._providers
            if layer in p.capabilities() and p.health() != ProviderHealth.DOWN
        ]
        # Highest trust first.
        return sorted(eligible, key=lambda p: p.trust, reverse=True)

    def _cache_key(self, provider: Provider, q: Query) -> str:
        return f"{provider.name}:{q.layer.value}:{q.route.key()}:{q.currency}"

    def _fetch_one(self, provider: Provider, q: Query) -> list[Quote]:
        key = self._cache_key(provider, q)
        cached = self.cache.get(key)
        if cached is not None:
            log.debug("cache hit %s", key)
            return cached

        quota = provider.remaining_quota()
        if quota is not None and quota <= 0:
            log.info("skip %s: quota exhausted", provider.name)
            return []

        if not self.rate_limiter.allow(provider.name):
            log.info("skip %s: rate limited", provider.name)
            return []

        try:
            quotes = provider.fetch(q)
        except Exception as exc:  # graceful degradation (§2.4): never crash
            log.warning("provider %s failed: %s", provider.name, exc)
            return []

        self.cache.set(key, quotes, ttl_seconds=self.ttl_seconds)
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
                    break  # fallback mode: first hit wins
        return collected
