"""aviationstack — L1 schedules (weak fallback only). §1, §5

STUB (Phase 2). aviationstack is a flight status/tracker API: Layer 1 only, no
fares and no award space, so it must never back the core. Wired behind the
interface as a schedules fallback. Reports DOWN until implemented.
"""

from __future__ import annotations

from typing import Optional

from ..domain.models import Layer
from .base import ProviderHealth, Query, Quote


class AviationstackProvider:
    name = "aviationstack"
    trust = 0.3

    def capabilities(self) -> set[Layer]:
        return {Layer.SCHEDULES}

    def health(self) -> ProviderHealth:
        return ProviderHealth.DOWN

    def remaining_quota(self) -> Optional[int]:
        return 0

    def fetch(self, q: Query) -> list[Quote]:
        return []
