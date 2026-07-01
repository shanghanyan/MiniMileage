"""FastAPI app — multi-user orchestrator (Phase 4).

Endpoints:
  POST /redemptions       start a quote run (uses the authed user's balances)
  GET  /status/{run_id}   poll the 4-step pipeline + verdict
  GET  /freshness         provider health, cache TTL, source checks
  GET  /me                the acting user's profile (balances/card)
  PUT  /users/{user_id}   upsert a user's balances/card (seed accounts)

Auth is off by default (Phase 3 contract preserved); set MILEAGE_AUTH=1 to make
the bearer token the user id and load balances from the Repository (§9).
"""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .. import obs
from ..config import Config, build_registry, build_repository, load_federation
from ..domain.models import User
from ..providers.aggregator.live_scrape import run_live_scrape
from .auth import make_current_user_dependency
from .orchestrator import RunOrchestrator, request_to_route, request_to_user
from .schemas import (
    FreshnessProvider,
    FreshnessResponse,
    FreshnessSource,
    LiveScrapeResponse,
    RedemptionRequest,
    RedemptionResponse,
    RunStatusResponse,
    UpsertUserRequest,
    UserProfile,
)

app = FastAPI(
    title="Mileage",
    description="Points-to-flights optimizer — Phase 4 multi-user orchestrator",
    version="0.4.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_config: Optional[Config] = None
_orchestrator: Optional[RunOrchestrator] = None


def get_config() -> Config:
    global _config
    if _config is None:
        _config = Config.from_env()
    return _config


def get_orchestrator(config: Config = Depends(get_config)) -> RunOrchestrator:
    global _orchestrator
    if _orchestrator is None or _orchestrator.config.db_path != config.db_path:
        if _orchestrator is not None:
            _orchestrator.close()
        _orchestrator = RunOrchestrator(config)
    return _orchestrator


current_user = make_current_user_dependency(get_config, get_orchestrator)


@app.on_event("startup")
def _start_tracing() -> None:
    # Initialize Arize AX tracing before any request runs the pipeline.
    obs.setup_tracing()


@app.on_event("shutdown")
def _stop_tracing() -> None:
    obs.shutdown_tracing()


def reset_app_state() -> None:
    """Clear cached config/orchestrator + shared stores (tests)."""
    global _config, _orchestrator
    if _orchestrator is not None:
        _orchestrator.close()
    _config = None
    _orchestrator = None


@app.post("/redemptions", response_model=RedemptionResponse)
def create_redemption(
    req: RedemptionRequest,
    config: Config = Depends(get_config),
    orchestrator: RunOrchestrator = Depends(get_orchestrator),
    user: User = Depends(current_user),
) -> RedemptionResponse:
    route = request_to_route(req)
    if config.auth_enabled:
        # Balances are server-side truth; the request body can't inflate them.
        acting = user
        if req.card:
            acting.card = req.card
    else:
        if req.miles is None:
            raise HTTPException(
                status_code=422,
                detail="miles is required when auth is disabled",
            )
        acting = request_to_user(req)
        acting.user_id = user.user_id
    record = orchestrator.start(route, acting, req.currency)
    return RedemptionResponse(
        run_id=record.run_id,
        status=record.status,
        step=record.step,
        user_id=acting.user_id,
    )


@app.get("/status/{run_id}", response_model=RunStatusResponse)
def get_status(
    run_id: str,
    orchestrator: RunOrchestrator = Depends(get_orchestrator),
) -> RunStatusResponse:
    record = orchestrator.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail="run not found")
    return RunStatusResponse(
        run_id=record.run_id,
        status=record.status,
        step=record.step,
        steps_done=record.steps_done,
        result=record.result,
        error=record.error,
        message=record.message,
    )


@app.get("/freshness", response_model=FreshnessResponse)
def get_freshness(config: Config = Depends(get_config)) -> FreshnessResponse:
    federation = load_federation(config)
    repo = build_repository(config)
    registry = build_registry(config, repo)
    try:
        ttl = (registry.ttl_seconds or 0) / 86400
        providers = [
            FreshnessProvider(**row) for row in registry.provider_status()
        ]
        sources = [
            FreshnessSource(**row) for row in repo.all_source_health()
        ]
    finally:
        repo.close()
    return FreshnessResponse(
        cache_ttl_days=ttl or federation.cache_ttl_seconds / 86400,
        providers=providers,
        sources=sources,
    )


@app.get("/me", response_model=UserProfile)
def get_me(user: User = Depends(current_user)) -> UserProfile:
    return UserProfile(
        user_id=user.user_id,
        card=user.card,
        balances=user.balances,
        preferences=user.preferences,
    )


@app.put("/users/{user_id}", response_model=UserProfile)
def upsert_user(
    user_id: str,
    req: UpsertUserRequest,
    orchestrator: RunOrchestrator = Depends(get_orchestrator),
) -> UserProfile:
    """Seed/update a user's balances + card (the only user-scoped data, §9)."""
    user = User(
        user_id=user_id,
        card=req.card,
        balances=dict(req.balances),
        preferences=dict(req.preferences),
    )
    orchestrator.repo.put_user(user)
    return UserProfile(
        user_id=user.user_id,
        card=user.card,
        balances=user.balances,
        preferences=user.preferences,
    )


@app.get("/scrape/live", response_model=LiveScrapeResponse)
def scrape_live(offline: bool = False) -> LiveScrapeResponse:
    """Run the live scrape and return a role-aware per-target report (§6).

    This is the manual "Live scrape" button behind the UI diagnostics page: it
    walks every source in `sources.yaml` through the production fetch/parse/
    resolve stack and reports, per target, what was scraped (row count + a small
    sample) and — when it produced nothing — the specific reason (undecoded body
    vs JS shell vs schema mismatch vs a dead URL), never a generic "parser miss".

    Role-aware: a PRIMARY failing is a hard `fail` (its program lost coverage); a
    FALLBACK failing is only a `warn` (a working primary already covers it), so
    `summary.all_primaries_ok` is the single truthful green/red signal.

    `offline=true` reads only `file://` fixtures (fast, deterministic — good for
    a smoke check); the default `offline=false` performs a REAL network scrape
    and can take tens of seconds.
    """
    report = run_live_scrape(offline=offline)
    return LiveScrapeResponse(**report.to_dict())


@app.get("/health")
def health(config: Config = Depends(get_config)) -> dict:
    return {
        "status": "ok",
        "backend": config.backend,
        "auth": config.auth_enabled,
    }
