"""Travelpayouts/Aviasales — L2 cached cash fares (free fallback). §5

STUB (Phase 2 hardening). Wired behind the Provider interface so it can be
registered as an ordered fallback for the cash "price-to-beat" without touching
the core. Reports DOWN until implemented.
"""

from __future__ import annotations

from typing import Optional

from ..domain.models import Layer
from .base import ProviderHealth, Query, Quote


class TravelpayoutsProvider:
    name = "travelpayouts"
    trust = 0.6

    def capabilities(self) -> set[Layer]:
        return {Layer.FARES}

    def health(self) -> ProviderHealth:
        return ProviderHealth.DOWN  # not implemented in Phase 0

    def remaining_quota(self) -> Optional[int]:
        return 0

    def fetch(self, q: Query) -> list[Quote]:
        return []
