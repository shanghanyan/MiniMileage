"""Trust-weighted aggregation (§7 step 2).

When independent sources are pooled, combine them by a trust-weighted median and
measure their spread. Two mirrors of the same published chart are NOT
independent; that judgment is the caller's (crosscheck.py groups inputs).
"""

from __future__ import annotations

from typing import Sequence


def trust_weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    """Weighted median of `values` by `weights`. Falls back to plain median."""
    pairs = [(v, w) for v, w in zip(values, weights) if w > 0]
    if not pairs:
        return float("nan")
    pairs.sort(key=lambda p: p[0])
    total = sum(w for _, w in pairs)
    acc = 0.0
    half = total / 2.0
    for value, weight in pairs:
        acc += weight
        if acc >= half:
            return value
    return pairs[-1][0]


def spread(values: Sequence[float]) -> float:
    """Relative spread (max-min)/median, 0.0 for a single value."""
    vals = [v for v in values if v == v]  # drop NaN
    if len(vals) <= 1:
        return 0.0
    vals_sorted = sorted(vals)
    mid = vals_sorted[len(vals_sorted) // 2] or 1.0
    return (vals_sorted[-1] - vals_sorted[0]) / abs(mid)
