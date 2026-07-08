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
    # Optional when authenticated: balances come from the user's account, not
    # the request body. Required for anonymous/single-user requests.
    miles: Optional[int] = Field(default=None, ge=0, examples=[90000])
    card: Literal["venture", "venture_x"] = "venture_x"


class RedemptionResponse(BaseModel):
    run_id: str
    status: RunStatus
    step: PipelineStep
    user_id: str = "local"


class UserProfile(BaseModel):
    user_id: str
    card: str = "venture_x"
    balances: dict[str, int] = Field(default_factory=dict)
    preferences: dict[str, str] = Field(default_factory=dict)


class UpsertUserRequest(BaseModel):
    card: Literal["venture", "venture_x"] = "venture_x"
    balances: dict[str, int] = Field(default_factory=dict)
    preferences: dict[str, str] = Field(default_factory=dict)


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


class LiveScrapeTarget(BaseModel):
    """One target's fetch -> parse -> resolve outcome (role-reclassified)."""

    name: str
    url: str
    role: str                       # primary | fallback
    format: str
    provides: str                   # chart | award
    trust: float
    status: str                     # ok | warn | fail
    detail: str
    rows: int = 0
    program: Optional[str] = None
    resolved: Optional[str] = None  # probe route this source resolved, if any
    reclassified: bool = False      # a fallback FAIL downgraded to WARN?
    sample: list[dict] = Field(default_factory=list)


class LiveScrapeProgram(BaseModel):
    """Per-program roll-up: does this program have a working PRIMARY source?"""

    program: str
    has_working_primary: bool
    primaries: list[dict] = Field(default_factory=list)
    fallbacks: list[dict] = Field(default_factory=list)


class LiveScrapeDiscoveryResult(BaseModel):
    row_count: int = 0
    email_docs: int = 0
    blog_new: int = 0
    transcript_new: int = 0
    email_links_followed: int = 0
    by_intake: dict[str, int] = Field(default_factory=dict)
    stale_programs: list[str] = Field(default_factory=list)
    used_fixtures: bool = False
    detail: str = ""


class LiveScrapeResponse(BaseModel):
    offline: bool
    targets: list[LiveScrapeTarget]
    programs: list[LiveScrapeProgram]
    summary: dict
    discovery: Optional[LiveScrapeDiscoveryResult] = None


class DailyScrapeResponse(BaseModel):
    """Last persisted daily scrape snapshot (Redis Cloud or local file fallback)."""

    found: bool
    storage: Optional[str] = None
    storage_backend: Optional[str] = None
    completed_at: Optional[str] = None
    stored_at: Optional[str] = None
    discovery: Optional[LiveScrapeDiscoveryResult] = None
    scrape: Optional[dict] = None


class ScrapeDiscoveryChannel(BaseModel):
    kind: str
    name: str
    url: Optional[str] = None
    trust: float
    ready: bool
    command: str
    detail: str


class ScrapeProviderPath(BaseModel):
    name: str
    health: str
    trust: float
    layers: list[str]
    disabled: bool
    monthly_limit: Optional[int] = None
    config_hint: Optional[str] = None
    note: Optional[str] = None


class ScrapeDiscoveredMeta(BaseModel):
    updated_at: Optional[str] = None
    row_count: int = 0
    by_intake: dict[str, int] = Field(default_factory=dict)
    stale_programs: list[str] = Field(default_factory=list)


class ScrapeInventoryResponse(BaseModel):
    discovery: list[ScrapeDiscoveryChannel]
    providers: list[ScrapeProviderPath]
    discovered: ScrapeDiscoveredMeta
    summary: dict
