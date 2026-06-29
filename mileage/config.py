"""Configuration & wiring (§4 config.py).

Provider keys, refresh cadence, cache TTLs, federation config, and the registry
assembly live here. Phase 2 adds quota guards, provider disable list, and
2-day cache cadence from knowledge/providers.yaml.
"""

from __future__ import annotations

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
from .store.inproc import InProcCache, InProcRateLimiter
from .store.sqlite_repo import SQLiteRepository, SqliteQuotaGuard

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

    @property
    def sources_path(self) -> Path:
        return self.knowledge_dir / "sources.yaml"

    @property
    def providers_path(self) -> Path:
        return self.knowledge_dir / "providers.yaml"

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
        )


def load_federation(config: Config | None = None) -> FederationConfig:
    config = config or Config.from_env()
    return load_federation_config(config.providers_path)


def build_registry(
    config: Config | None = None,
    repo: Optional[SQLiteRepository] = None,
) -> ProviderRegistry:
    """Assemble the federated provider registry (Phase 2 hardened)."""
    config = config or Config.from_env()
    federation = load_federation(config)
    quota = SqliteQuotaGuard(repo) if repo is not None else None

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
        cache=InProcCache(),
        rate_limiter=InProcRateLimiter(
            rate=config.rate_per_sec, capacity=config.rate_capacity
        ),
        quota=quota,
        federation=federation,
        ttl_seconds=federation.cache_ttl_seconds or config.cache_ttl_seconds,
        disabled=set(config.disabled_providers),
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
