"""Sanity bounds — reject implausible values before they enter the graph (§2.1).

A scrape or parse that yields, say, 5 miles or 5,000,000 miles for a business
seat is almost certainly a selector error, not a real datum. Bounds turn the
"no hallucinations" principle into an enforceable check.
"""

from __future__ import annotations

from ..domain.models import AwardQuote, Cabin

# Plausible one-way award cost ranges per cabin (program miles).
AWARD_BOUNDS: dict[Cabin, tuple[int, int]] = {
    Cabin.ECONOMY: (3_000, 200_000),
    Cabin.PREMIUM_ECONOMY: (5_000, 250_000),
    Cabin.BUSINESS: (10_000, 400_000),
    Cabin.FIRST: (15_000, 700_000),
}

# Plausible one-way cash fare range, US cents.
FARE_BOUNDS_CENTS: tuple[int, int] = (1_000, 5_000_000)


def within_bounds(quote: AwardQuote) -> bool:
    lo, hi = AWARD_BOUNDS.get(quote.route.cabin, (1, 10_000_000))
    return lo <= quote.miles <= hi


def fare_within_bounds(cash_cents: int) -> bool:
    lo, hi = FARE_BOUNDS_CENTS
    return lo <= cash_cents <= hi
