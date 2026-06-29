"""Pydantic models for the Phase 3 HTTP API."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


PipelineStep = Literal["route", "gathering", "crosscheck", "redemptions"]
RunStatus = Literal["pending", "running", "complete", "error"]


class RedemptionRequest(BaseModel):
    origin: str = Field(..., min_length=3, max_length=3, examples=["LAX"])
    dest: str = Field(..., min_length=3, max_length=3, examples=["IST"])
    cabin: Literal["economy", "premium_economy", "business", "first"] = "economy"
    currency: str = "capital_one"
    miles: int = Field(..., ge=0, examples=[90000])
    card: Literal["venture", "venture_x"] = "venture_x"


class RedemptionResponse(BaseModel):
    run_id: str
    status: RunStatus
    step: PipelineStep


class RunStatusResponse(BaseModel):
    run_id: str
    status: RunStatus
    step: PipelineStep
    steps_done: list[PipelineStep] = Field(default_factory=list)
    result: Optional[dict] = None
    error: Optional[str] = None
    message: Optional[str] = None


class FreshnessProvider(BaseModel):
    name: str
    health: str
    trust: float
    layers: list[str]
    disabled: bool
    monthly_limit: Optional[int] = None
    used: int = 0
    remaining: Optional[int] = None


class FreshnessSource(BaseModel):
    source_name: str
    url: str
    last_status: Optional[int] = None
    last_404: bool = False
    checked_at: str


class FreshnessResponse(BaseModel):
    cache_ttl_days: float
    providers: list[FreshnessProvider]
    sources: list[FreshnessSource]
