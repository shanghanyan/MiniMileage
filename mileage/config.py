"""Configuration & wiring (§4 config.py).

Provider keys, refresh cadence, cache TTLs, federation config, and the registry
assembly live here. Phase 2 adds quota guards, provider disable list, and
2-day cache cadence from knowledge/providers.yaml.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .providers.aggregator import AggregatorProvider
from .providers.amadeus import AmadeusProvider
from .providers.aviationstack import AviationstackProvider
from .providers.curated import CuratedProvider
from .providers.federation import FederationConfig, load_federation_config
from .providers.registry import DEFAULT_TTL_SECONDS, ProviderRegistry
from .providers.seats_aero import SeatsAeroProvider
from .providers.travelpayouts import TravelpayoutsProvider
from .store.inproc import InProcCache, InProcRateLimiter, ThreadLock
from .store.jobs import InProcJobQueue
from .store.sqlite_repo import SQLiteRepository, SqliteQuotaGuard
from .store.stores import StoreBundle

log = logging.getLogger("mileage.config")

_KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
DEFAULT_CURRENCY = "capital_one"


@dataclass
class Config:
    db_path: str = "mileage.db"
    cache_ttl_seconds: float = DEFAULT_TTL_SECONDS
    rate_per_sec: float = 5.0
    rate_capacity: float = 10.0
    knowledge_dir: Path = field(default=_KNOWLEDGE_DIR)
    aggregator_enabled: bool = True
    disabled_providers: set[str] = field(default_factory=set)
    # --- Phase 4: multi-user backend selection (§9) ----------------------- #
    # When set (redis:// or rediss:// for Upstash TLS), the hot cache, global
    # quota counter, and de-dupe locks move to Redis; otherwise the in-process
    # impls are used. Falls back to in-proc if the server is unreachable.
    redis_url: Optional[str] = None
    auth_enabled: bool = False

    @property
    def sources_path(self) -> Path:
        return self.knowledge_dir / "sources.yaml"

    @property
    def providers_path(self) -> Path:
        return self.knowledge_dir / "providers.yaml"

    @property
    def backend(self) -> str:
        return "redis" if self.redis_url else "inproc"

    @classmethod
    def from_env(cls) -> "Config":
        disabled = {
            p.strip()
            for p in os.getenv("MILEAGE_DISABLE_PROVIDERS", "").split(",")
            if p.strip()
        }
        return cls(
            db_path=os.getenv("MILEAGE_DB", "mileage.db"),
            aggregator_enabled=os.getenv("MILEAGE_NO_AGGREGATOR", "") == "",
            disabled_providers=disabled,
            redis_url=os.getenv("MILEAGE_REDIS_URL") or None,
            auth_enabled=os.getenv("MILEAGE_AUTH", "") not in ("", "0", "false"),
        )


def load_federation(config: Config | None = None) -> FederationConfig:
    config = config or Config.from_env()
    return load_federation_config(config.providers_path)


# --------------------------------------------------------------------------- #
# Memory layer (§9) — assemble the swappable backend in one place. A bundle is
# held for the lifetime of a long-running context (the API orchestrator, or a
# CLI invocation), so the cache, rate limiter, and global quota counter are
# shared across the runs that context serves. That sharing is what makes "one
# scrape, both users served from cache" true; the orchestrator holds exactly
# one registry (hence one bundle) for its lifetime.
# --------------------------------------------------------------------------- #
def _make_redis_stores(
    config: Config, repo: Optional[SQLiteRepository]
) -> Optional[StoreBundle]:
    """Build a Redis-backed bundle; return None if Redis is unreachable."""
    from .store.redis_impl import (
        RedisCache,
        RedisLock,
        RedisQuotaGuard,
        RedisRateLimiter,
        redis_from_url,
    )

    try:
        client = redis_from_url(config.redis_url)
        client.ping()
    except Exception as exc:  # redis missing or server down -> graceful fallback
        log.warning(
            "MILEAGE_REDIS_URL set but Redis is unavailable (%s); "
            "falling back to in-process stores.",
            exc,
        )
        return None

    return StoreBundle(
        cache=RedisCache(client),
        rate_limiter=RedisRateLimiter(
            client, rate=config.rate_per_sec, capacity=config.rate_capacity
        ),
        lock=RedisLock(client),
        quota=RedisQuotaGuard(client),
        backend="redis",
        jobs=InProcJobQueue(),
        client=client,
    )


def _make_inproc_stores(
    config: Config, repo: Optional[SQLiteRepository]
) -> StoreBundle:
    return StoreBundle(
        cache=InProcCache(),
        rate_limiter=InProcRateLimiter(
            rate=config.rate_per_sec, capacity=config.rate_capacity
        ),
        lock=ThreadLock(),
        quota=SqliteQuotaGuard(repo) if repo is not None else None,
        backend="inproc",
        jobs=InProcJobQueue(),
    )


def build_stores(
    config: Config, repo: Optional[SQLiteRepository] = None
) -> StoreBundle:
    """Assemble the memory-layer backend selected by `config` (§9)."""
    bundle: Optional[StoreBundle] = None
    if config.redis_url:
        bundle = _make_redis_stores(config, repo)
    if bundle is None:
        bundle = _make_inproc_stores(config, repo)
    return bundle


def build_registry(
    config: Config | None = None,
    repo: Optional[SQLiteRepository] = None,
    *,
    stores: Optional[StoreBundle] = None,
) -> ProviderRegistry:
    """Assemble the federated provider registry (Phase 2 hardened, Phase 4 shared)."""
    config = config or Config.from_env()
    federation = load_federation(config)

    if stores is None:
        stores = build_stores(config, repo)

    providers = [
        AmadeusProvider(),
        SeatsAeroProvider(),
        AggregatorProvider(
            sources_path=config.sources_path,
            knowledge_dir=config.knowledge_dir,
            enabled=config.aggregator_enabled,
            health_repo=repo,
        ),
        CuratedProvider(knowledge_dir=config.knowledge_dir),
        TravelpayoutsProvider(knowledge_dir=config.knowledge_dir),
        AviationstackProvider(),
    ]
    return ProviderRegistry(
        providers,
        cache=stores.cache,
        rate_limiter=stores.rate_limiter,
        quota=stores.quota,
        lock=stores.lock,
        federation=federation,
        ttl_seconds=federation.cache_ttl_seconds or config.cache_ttl_seconds,
        disabled=set(config.disabled_providers),
        stores=stores,
    )


def build_repository(config: Config | None = None) -> SQLiteRepository:
    config = config or Config.from_env()
    return SQLiteRepository(config.db_path)


def partner_programs(config: Config | None = None) -> list[str]:
    """Capital One transfer partners declared in knowledge/ratios.yaml."""
    config = config or Config.from_env()
    path = config.knowledge_dir / "ratios.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return list((data.get("partners") or {}).keys())
