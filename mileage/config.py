"""Configuration & wiring (§4 config.py).

Provider keys, refresh cadence, cache TTLs, and the registry assembly live here.
Phase 0 runs with zero keys: Amadeus and seats.aero self-disable when their env
vars are absent, and the registry falls back to the curated provider.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .providers.aggregator import AggregatorProvider
from .providers.amadeus import AmadeusProvider
from .providers.aviationstack import AviationstackProvider
from .providers.curated import CuratedProvider
from .providers.registry import DEFAULT_TTL_SECONDS, ProviderRegistry
from .providers.seats_aero import SeatsAeroProvider
from .providers.travelpayouts import TravelpayoutsProvider
from .store.inproc import InProcCache, InProcRateLimiter
from .store.sqlite_repo import SQLiteRepository

_KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
DEFAULT_CURRENCY = "capital_one"


@dataclass
class Config:
    db_path: str = "mileage.db"
    cache_ttl_seconds: float = DEFAULT_TTL_SECONDS
    rate_per_sec: float = 5.0
    rate_capacity: float = 10.0
    knowledge_dir: Path = field(default=_KNOWLEDGE_DIR)
    # Engine A is the DEFAULT award-space/chart source from Phase 1. Toggle off
    # with MILEAGE_NO_AGGREGATOR=1 to fall back to curated-only (graceful, §2.4).
    aggregator_enabled: bool = True

    @property
    def sources_path(self) -> Path:
        return self.knowledge_dir / "sources.yaml"

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            db_path=os.getenv("MILEAGE_DB", "mileage.db"),
            aggregator_enabled=os.getenv("MILEAGE_NO_AGGREGATOR", "") == "",
        )


def build_registry(config: Config | None = None) -> ProviderRegistry:
    """Assemble the federated provider registry for Phase 0."""
    config = config or Config.from_env()
    providers = [
        # Primary live APIs (self-disable without keys).
        AmadeusProvider(),
        SeatsAeroProvider(),
        # Engine A: default L3 award space + L4 charts from real scraped data.
        AggregatorProvider(
            sources_path=config.sources_path,
            knowledge_dir=config.knowledge_dir,
            enabled=config.aggregator_enabled,
        ),
        # Curated YAML: trusted L4 charts/ratios baseline + fallback L2 fares.
        CuratedProvider(knowledge_dir=config.knowledge_dir),
        # Wired fallbacks (DOWN until later phases).
        TravelpayoutsProvider(),
        AviationstackProvider(),
    ]
    return ProviderRegistry(
        providers,
        cache=InProcCache(),
        rate_limiter=InProcRateLimiter(
            rate=config.rate_per_sec, capacity=config.rate_capacity
        ),
        ttl_seconds=config.cache_ttl_seconds,
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
