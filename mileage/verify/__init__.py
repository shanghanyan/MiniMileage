"""Verification core — cross-check, trust, freshness, bounds (§2, §7).

Like `domain/`, this layer NEVER imports from `providers/`, `aggregator/`, or
`brain/`. It consumes normalized quotes and decides what is trustworthy.

Principle (§2.1): no hallucinations — a datum is usable only if it carries a
verifiable value with provenance. Anything that fails bounds or arrives without
provenance is dropped, never silently used.
"""

from .freshness import age_decayed_confidence, is_stale
from .trust import trust_weighted_median
from .bounds import within_bounds, AWARD_BOUNDS
from .crosscheck import verify_award_quotes, verify_fare, VerifiedAward, VerifiedFare

__all__ = [
    "age_decayed_confidence",
    "is_stale",
    "trust_weighted_median",
    "within_bounds",
    "AWARD_BOUNDS",
    "verify_award_quotes",
    "verify_fare",
    "VerifiedAward",
    "VerifiedFare",
]
