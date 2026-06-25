"""seats.aero Partner API — L3 live award availability (OPTIONAL, paid). §1, §5

Off by default. Wired behind the SAME interface as the aggregator so it can be
switched on with a key (SEATS_AERO_API_KEY) when/if you choose to pay — no
rearchitecting. Because it is genuinely INDEPENDENT of the aggregator scrape,
turning it on enables a real cross-check (§7 step 2). Reports DOWN without a key.
"""

from __future__ import annotations

import os
from typing import Optional

from ..domain.models import Layer
from .base import ProviderHealth, Query, Quote


class SeatsAeroProvider:
    name = "seats_aero"
    trust = 0.85

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key or os.getenv("SEATS_AERO_API_KEY")

    def capabilities(self) -> set[Layer]:
        return {Layer.AWARD}

    def health(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY if self.api_key else ProviderHealth.DOWN

    def remaining_quota(self) -> Optional[int]:
        return None

    def fetch(self, q: Query) -> list[Quote]:
        # Implemented when the optional paid upgrade is enabled (post-Phase 0).
        return []
