"""Cross-check — reconcile quotes per (program, route, cabin) group (§7).

Pipeline per group:
  1. Drop quotes that fail sanity bounds (no hallucinations, §2.1).
  2. Age-decay each quote's confidence; flag `stale` past the cutoff.
  3. Live precedence (§2.5): if any quote in the group carries live award space
     (no `no_live_space` flag), the chart-only quotes are dropped and only the
     live quotes reconcile — a confirmed seat overrides a static chart price.
  4. Reconcile across INDEPENDENT sources (distinct source_name):
       - multiple independent -> trust-weighted median; spread >10% adds a
         `sources_disagree_NN%` flag and demotes confidence.
       - single source -> `single_source` flag at medium confidence.
  5. Carry through provenance, seat availability, and chart flags.

Cross-check earns its name only across genuinely independent providers (§2.3).
Two mirrors of the same chart are not independent — here, independence is keyed
by source_name, so curated-only Phase 0 groups are honestly `single_source`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..domain.models import AwardQuote, FareQuote, Provenance, Route
from .bounds import fare_within_bounds, within_bounds
from .freshness import age_decayed_confidence, is_stale
from .trust import spread, trust_weighted_median

SPREAD_THRESHOLD = 0.10


@dataclass
class VerifiedAward:
    program: str
    route: Route
    miles: int
    confidence: float
    seats_available: Optional[int] = None  # set once live space verifies it
    flags: list[str] = field(default_factory=list)
    provenance: list[Provenance] = field(default_factory=list)


@dataclass
class VerifiedFare:
    route: Route
    cash_cents: int
    confidence: float
    flags: list[str] = field(default_factory=list)
    provenance: Optional[Provenance] = None


def _independent_sources(quotes: list[AwardQuote]) -> int:
    return len({q.provenance.source_name for q in quotes})


def verify_award_quotes(
    quotes: list[AwardQuote], *, now: Optional[datetime] = None
) -> list[VerifiedAward]:
    """Group by program and reconcile each group into one verified award."""
    by_program: dict[str, list[AwardQuote]] = {}
    for q in quotes:
        if not q.provenance or q.provenance.source_name == "unknown":
            continue  # no provenance -> never usable (§2.1)
        if not within_bounds(q):
            continue  # implausible value dropped
        by_program.setdefault(q.program, []).append(q)

    verified: list[VerifiedAward] = []
    for program, group in by_program.items():
        # Live precedence (§2.5): a confirmed seat overrides a static chart.
        live = [q for q in group if "no_live_space" not in q.flags]
        if live:
            group = live

        miles_vals = [float(q.miles) for q in group]
        weights = [max(q.provenance.trust, 1e-6) for q in group]
        decayed = [
            age_decayed_confidence(q.confidence, q.provenance, now=now)
            for q in group
        ]

        flags: set[str] = set()
        for q in group:
            flags.update(q.flags)
            if is_stale(q.provenance, now=now):
                flags.add("stale")
        if live:
            flags.discard("no_live_space")

        seats = [q.seats_available for q in group if q.seats_available is not None]
        seats_available = max(seats) if seats else None

        n_independent = _independent_sources(group)
        if n_independent >= 2:
            miles = int(round(trust_weighted_median(miles_vals, weights)))
            sp = spread(miles_vals)
            confidence = max(decayed)
            if sp > SPREAD_THRESHOLD:
                flags.add(f"sources_disagree_{int(round(sp * 100))}%")
                confidence *= 0.6
        else:
            # Single source: pick its (single) value, medium confidence.
            best_idx = max(range(len(group)), key=lambda i: decayed[i])
            miles = int(group[best_idx].miles)
            confidence = min(decayed[best_idx], 0.7)
            flags.add("single_source")

        verified.append(
            VerifiedAward(
                program=program,
                route=group[0].route,
                miles=miles,
                confidence=round(confidence, 3),
                seats_available=seats_available,
                flags=sorted(flags),
                provenance=[q.provenance for q in group],
            )
        )
    return verified


def verify_fare(
    quotes: list[FareQuote], *, now: Optional[datetime] = None
) -> Optional[VerifiedFare]:
    """Pick the most trustworthy in-bounds fare as the price-to-beat."""
    usable = [
        q
        for q in quotes
        if q.provenance
        and q.provenance.source_name != "unknown"
        and fare_within_bounds(q.cash_cents)
    ]
    if not usable:
        return None
    scored = [
        (age_decayed_confidence(q.confidence, q.provenance, now=now), q)
        for q in usable
    ]
    confidence, best = max(scored, key=lambda s: s[0])
    flags = set(best.flags)
    if is_stale(best.provenance, now=now):
        flags.add("stale")
    return VerifiedFare(
        route=best.route,
        cash_cents=best.cash_cents,
        confidence=round(confidence, 3),
        flags=sorted(flags),
        provenance=best.provenance,
    )
