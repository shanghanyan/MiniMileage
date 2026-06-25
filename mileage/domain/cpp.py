"""Cents-per-point math, with per-hop compounding.

CPP (cents per point) = cash value unlocked (in cents) / source points spent.
Higher is better. The portal floor is a fixed CPP by card product.

Phase 0 uses a single transfer hop (Capital One -> partner program), but the
compounding helpers are written for the multi-hop north star so the graph
optimizer (graph/optimize.py) never has to special-case hop count.
"""

from __future__ import annotations

import math
from typing import Iterable


def cpp(cash_cents: int, source_points: int) -> float:
    """Cents of value per source point. Returns 0.0 if no points are spent."""
    if source_points <= 0:
        return 0.0
    return cash_cents / source_points


def portal_points_needed(cash_cents: int, portal_cpp: float) -> int:
    """Points to cover a cash fare at the fixed portal rate."""
    if portal_cpp <= 0:
        return math.inf  # type: ignore[return-value]
    return math.ceil(cash_cents / portal_cpp)


def source_points_for_award(program_miles: int, ratio: float) -> int:
    """Source points needed to acquire `program_miles` at `ratio`.

    ratio = program points per 1 source point. C1 -> Turkish is 1:1, so 45,000
    Turkish miles costs 45,000 C1 miles. A 2:1.5 transfer bonus would lower it.
    """
    if ratio <= 0:
        return math.inf  # type: ignore[return-value]
    return math.ceil(program_miles / ratio)


def compound_ratio(ratios: Iterable[float]) -> float:
    """Multiply transfer ratios across hops (e.g. C1 -> A -> B)."""
    total = 1.0
    for r in ratios:
        total *= r
    return total


def transfer_cpp(cash_cents: int, program_miles: int, ratio: float) -> float:
    """End-to-end CPP for a single-currency transfer redemption."""
    pts = source_points_for_award(program_miles, ratio)
    return cpp(cash_cents, pts)
