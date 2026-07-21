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
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CURRENCY = "capital_one"


def _load_dotenv() -> None:
    """Load <repo>/.env into the environment if python-dotenv is installed.

    Keys are read from the process environment (§4); a .env file is the
    convenient local place to keep them. Absence of python-dotenv (or the file)
    is a no-op — shell exports still work exactly as before.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(_REPO_ROOT / ".env")


_load_dotenv()


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
    # When True the aggregator never touches the network: only `file://`
    # fixtures resolve (live HTTP + Wayback short-circuit to None). Set via
    # MILEAGE_OFFLINE=1. This is what makes the test suite and `mileage eval`
    # deterministic and hang-proof regardless of network reachability. The
    # default reads the environment so even a bare `Config()` (as built in
    # tests) honors MILEAGE_OFFLINE without threading it through every caller.
    offline: bool = field(
        default_factory=lambda: os.getenv("MILEAGE_OFFLINE", "")
        not in ("", "0", "false")
    )
    # Gmail mailbox for the discovery intake (§6.1). Read from the environment
    # (.env), never hardcoded. App-Password IMAP only — no OAuth, no Gmail API.
    gmail_address: Optional[str] = None
    gmail_app_password: Optional[str] = None
    # When True (default), messages successfully polled over live IMAP are
    # moved to [Gmail]/Trash after extraction so the inbox doesn't fill up with
    # already-scraped mail forever (previously: PEEK-only, never marked/removed
    # at the message level — see mileage-project-state memory). Gmail keeps
    # trashed mail ~30 days before permanent purge, so this is recoverable, not
    # a hard delete. Never touches fixtures/offline runs. Set
    # GMAIL_AUTO_DELETE=0 to keep the old poll-forever behavior.
    gmail_auto_delete: bool = True
    # --- Phase 8b: URL rediscovery (§F) ----------------------------------- #
    # Deterministic scraping of known-good URLs is the default + cheap path. An
    # LLM/web search runs ONLY when a source rots, behind this flag AND a search
    # key (no key -> no-op). It only proposes URLs; extraction stays
    # deterministic + grounded. Rot thresholds: a source is rotted when it 404s,
    # OR fails N times in a row, OR returns M consecutive selector-misses.
    url_rediscovery_enabled: bool = False
    bing_search_api_key: Optional[str] = None
    serpapi_api_key: Optional[str] = None
    rot_max_failures: int = 3
    rot_max_selector_misses: int = 2
    rediscovery_min_rotted: int = 1
    # --- Local LLM extractor backend (§6.2) --------------------------------- #
    # Default stays the keyless deterministic extractor everywhere (offline
    # tests, a fresh checkout, CI). Set MILEAGE_EXTRACTOR_BACKEND=ollama once a
    # local Ollama server + model are actually running (see
    # Cursor-LLM-Extractor-Task.md) to opt every ingest call site into
    # `OllamaExtractor` via `extract.build_extractor(config)`.
    extractor_backend: str = "deterministic"
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"

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
            offline=os.getenv("MILEAGE_OFFLINE", "") not in ("", "0", "false"),
            gmail_address=os.getenv("GMAIL_ADDRESS") or None,
            gmail_app_password=os.getenv("GMAIL_APP_PASSWORD") or None,
            gmail_auto_delete=os.getenv("GMAIL_AUTO_DELETE", "1")
            not in ("0", "false", "False"),
            url_rediscovery_enabled=os.getenv("MILEAGE_URL_REDISCOVERY", "")
            not in ("", "0", "false"),
            bing_search_api_key=os.getenv("BING_SEARCH_API_KEY") or None,
            serpapi_api_key=os.getenv("SERPAPI_API_KEY") or None,
            rot_max_failures=int(os.getenv("MILEAGE_ROT_MAX_FAILURES", "3")),
            rot_max_selector_misses=int(
                os.getenv("MILEAGE_ROT_MAX_SELECTOR_MISSES", "2")
            ),
            rediscovery_min_rotted=int(os.getenv("MILEAGE_REDISCOVERY_MIN_ROTTED", "1")),
            extractor_backend=os.getenv("MILEAGE_EXTRACTOR_BACKEND", "deterministic"),
            ollama_host=os.getenv("OLLAMA_HOST", "http://localhost:11434"),
            ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b-instruct"),
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
            offline=config.offline,
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


def partner_programs(
    config: Config | None = None, currency: Optional[str] = None
) -> list[str]:
    """Transfer partners declared in knowledge/ratios.yaml for `currency`.

    `currency=None` preserves prior behavior (the top-level, capital_one block)
    for existing callers. Pass the actual query currency to restrict the CHARTS
    query to that currency's partners — otherwise a `--currency amex_mr` run
    would still be narrowed to capital_one's partner list (§13 Phase 7).
    """
    config = config or Config.from_env()
    path = config.knowledge_dir / "ratios.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    top_currency = data.get("from_currency", DEFAULT_CURRENCY)
    if currency is None or currency == top_currency:
        return list((data.get("partners") or {}).keys())
    for block in data.get("currencies") or []:
        if block.get("from_currency") == currency:
            return list((block.get("partners") or {}).keys())
    return []
