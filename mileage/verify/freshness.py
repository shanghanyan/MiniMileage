"""Freshness — provenance age decays confidence (§2.7).

A datum's confidence is reduced as it ages past a half-life; past a hard
staleness cutoff it is flagged `stale` and cannot be `recommended`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..domain.models import Provenance, utcnow

# Charts change ~quarterly; treat data older than this as stale.
DEFAULT_STALE_AFTER_DAYS = 120.0
# Confidence halves every half-life.
DEFAULT_HALF_LIFE_DAYS = 60.0


def age_days(prov: Provenance, *, now: Optional[datetime] = None) -> float:
    return prov.age_seconds(now=now or utcnow()) / 86400.0


def is_stale(
    prov: Provenance,
    *,
    stale_after_days: float = DEFAULT_STALE_AFTER_DAYS,
    now: Optional[datetime] = None,
) -> bool:
    return age_days(prov, now=now) > stale_after_days


def age_decayed_confidence(
    base_confidence: float,
    prov: Provenance,
    *,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    now: Optional[datetime] = None,
) -> float:
    """Exponentially decay confidence by age. Clamped to [0, 1]."""
    days = age_days(prov, now=now)
    decay = 0.5 ** (days / half_life_days) if half_life_days > 0 else 1.0
    return max(0.0, min(1.0, base_confidence * decay))
