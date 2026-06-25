"""Aggregator politeness — PHASE 1 placeholder. §6

Planned: adaptive per-domain throttle + backoff + jitter for 429s, and a small
source-rotation policy that learns the fastest non-429 delay per domain. This is
scheduling efficiency, NOT evasion; it starts hardcoded and stays there until
volume justifies more. Not implemented in Phase 0.
"""

from __future__ import annotations

DEFAULT_DELAY_SECONDS = 2.0
DEFAULT_JITTER_SECONDS = 0.5


def delay_for(domain: str) -> float:
    """Hardcoded polite delay; replaced by a learned policy in Phase 1+."""
    return DEFAULT_DELAY_SECONDS
