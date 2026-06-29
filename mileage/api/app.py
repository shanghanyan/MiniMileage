"""FastAPI app — POST /redemptions, GET /status/{run_id}, GET /freshness."""

from __future__ import annotations

from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from ..config import Config, build_registry, build_repository, load_federation
from .orchestrator import RunOrchestrator, request_to_route, request_to_user
from .schemas import (
    FreshnessProvider,
    FreshnessResponse,
    FreshnessSource,
    RedemptionRequest,
    RedemptionResponse,
    RunStatusResponse,
)

app = FastAPI(
    title="Mileage",
    description="Points-to-flights optimizer — Phase 3 orchestrator",
    version="0.3.0",
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
        _orchestrator = RunOrchestrator(config)
    return _orchestrator


def reset_app_state() -> None:
    """Clear cached config/orchestrator (tests)."""
    global _config, _orchestrator
    _config = None
    _orchestrator = None


@app.post("/redemptions", response_model=RedemptionResponse)
def create_redemption(
    req: RedemptionRequest,
    orchestrator: RunOrchestrator = Depends(get_orchestrator),
) -> RedemptionResponse:
    route = request_to_route(req)
    user = request_to_user(req)
    record = orchestrator.start(route, user, req.currency)
    return RedemptionResponse(
        run_id=record.run_id,
        status=record.status,
        step=record.step,
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
