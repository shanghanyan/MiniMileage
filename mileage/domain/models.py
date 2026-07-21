"""Source-agnostic domain models.

These types are the contract between the provider layer and the verification /
graph core. A scrape, an API call, or a curated YAML row all normalize to the
same `AwardQuote` / `FareQuote`, so the core cannot tell them apart
(Cursor-Mileage-Plan.md §2.2).

Every datum carries provenance + confidence as first-class fields (§2.7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Layer(str, Enum):
    """The four data layers a provider can serve (§1)."""

    SCHEDULES = "schedules"  # L1: which flights fly O->D, and when
    FARES = "fares"          # L2: the cash price-to-beat
    AWARD = "award"          # L3: is there a saver seat in miles
    CHARTS = "charts"        # L4: ratios + award charts (how points convert)


class Cabin(str, Enum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


class VerdictLabel(str, Enum):
    """Honest conclusions (§7). Never name a winner without verified data."""

    PORTAL_ONLY = "portal_only"      # no verified transfer path beats the floor
    COMPARABLE = "comparable"        # best transfer within 20% of portal
    BEST = "best"                    # transfer beats portal by >=20%
    TENTATIVE_BEST = "tentative_best"  # best, but the winner carries a warning flag


# Portal floor, cents-per-point, by Capital One product (§7).
PORTAL_CPP: dict[str, float] = {
    "venture": 1.0,
    "venture_x": 1.25,
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Provenance — attached to every datum
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Provenance:
    """Where a datum came from, how much we trust it, and how old it is."""

    source_name: str
    source_url: Optional[str] = None
    fetched_at: datetime = field(default_factory=utcnow)
    # Trust weight in [0, 1]; authoritative sources (Capital One) ~1.0.
    trust: float = 0.5
    # When the *source* last updated the underlying value (vs. when we fetched).
    source_updated_at: Optional[datetime] = None

    def age_seconds(self, *, now: Optional[datetime] = None) -> float:
        now = now or utcnow()
        basis = self.source_updated_at or self.fetched_at
        return max(0.0, (now - basis).total_seconds())


# --------------------------------------------------------------------------- #
# Route + user
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Route:
    origin: str          # IATA, e.g. "LAX"
    dest: str            # IATA, e.g. "JFK"
    cabin: Cabin = Cabin.ECONOMY

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", self.origin.upper())
        object.__setattr__(self, "dest", self.dest.upper())

    def key(self) -> str:
        return f"{self.origin}-{self.dest}-{self.cabin.value}"


@dataclass
class User:
    """The only user-scoped data in the system (§9). Market data is shared.

    Multi-user-ready: a `user_id` exists from Phase 0 even though the CLI runs
    single-user, so the Repository can carry a user dimension later without a
    schema rewrite.
    """

    user_id: str = "local"
    # currency -> point balance, e.g. {"capital_one": 20000}
    balances: dict[str, int] = field(default_factory=dict)
    # Capital One product determining the portal floor.
    card: str = "venture_x"
    preferences: dict[str, str] = field(default_factory=dict)

    def portal_cpp(self) -> float:
        return PORTAL_CPP.get(self.card, PORTAL_CPP["venture"])


# --------------------------------------------------------------------------- #
# Normalized quotes (provider output)
# --------------------------------------------------------------------------- #
@dataclass
class FareQuote:
    """L2 — the cash price-to-beat for a route/cabin, in US cents."""

    route: Route
    cash_cents: int
    currency: str = "USD"
    provenance: Provenance = field(
        default_factory=lambda: Provenance(source_name="unknown")
    )
    confidence: float = 0.5
    flags: list[str] = field(default_factory=list)

    @property
    def cash_dollars(self) -> float:
        return self.cash_cents / 100.0


@dataclass
class AwardQuote:
    """L3/L4 — miles required to fly a route/cabin in a given program.

    A chart-derived quote (no confirmed seat) carries the `no_live_space` flag;
    a live-availability quote (Phase 1+) does not.
    """

    program: str               # e.g. "turkish", "aeroplan", "lifemiles"
    route: Route
    miles: int                 # one-way miles in the program's own currency
    seats_available: Optional[int] = None  # None => unknown (chart-only)
    provenance: Provenance = field(
        default_factory=lambda: Provenance(source_name="unknown")
    )
    confidence: float = 0.5
    flags: list[str] = field(default_factory=list)


@dataclass
class TransferRatio:
    """L4 — how a transferable currency converts into a program.

    ratio = program_points received per 1 source point (base rate). Capital One
    -> most Star Alliance partners is 1:1. Capital One -> United does NOT exist;
    that absence is load-bearing and is represented by the lack of a row.

    Optional transfer bonuses: ``bonus_multiplier`` (e.g. 1.3 = +30%) applied
    on top of ``ratio`` while ``valid_from``/``valid_until`` (ISO dates) contain
    "today". Effective ratio = ratio * bonus_multiplier. Inactive bonus rows
    are filtered out by the curated loader before they reach the graph.
    """

    from_currency: str         # e.g. "capital_one"
    to_program: str            # e.g. "turkish"
    ratio: float = 1.0
    provenance: Provenance = field(
        default_factory=lambda: Provenance(source_name="unknown")
    )
    confidence: float = 1.0
    flags: list[str] = field(default_factory=list)
    bonus_multiplier: float = 1.0
    valid_from: Optional[str] = None   # ISO date inclusive, or None
    valid_until: Optional[str] = None  # ISO date inclusive, or None
    bonus_label: Optional[str] = None  # e.g. "+30% transfer bonus"

    @property
    def effective_ratio(self) -> float:
        return self.ratio * self.bonus_multiplier

    @property
    def is_bonus(self) -> bool:
        return self.bonus_multiplier != 1.0 or "transfer_bonus" in self.flags


# --------------------------------------------------------------------------- #
# Optimizer output
# --------------------------------------------------------------------------- #
@dataclass
class PathOption:
    """One concrete way to pay for the seat, ranked by cents-per-point."""

    label: str                 # human label, e.g. "Capital One -> Turkish"
    kind: str                  # "portal" | "transfer"
    cpp: float                 # cents per source point
    source_points: int         # source-currency points required
    cash_cents: int            # cash value being unlocked
    program: Optional[str] = None
    affordable: bool = True    # does the user hold enough points?
    confidence: float = 0.5
    flags: list[str] = field(default_factory=list)
    provenance: list[Provenance] = field(default_factory=list)


@dataclass
class Verdict:
    label: VerdictLabel
    route: Route
    portal: PathOption
    best_transfer: Optional[PathOption]
    options: list[PathOption]
    rationale: str
    flags: list[str] = field(default_factory=list)
