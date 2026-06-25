"""Aggregator fetch stack — PHASE 1 placeholder. §6

Planned: httpx for plain pages, curl_cffi (TLS/JA4 impersonation) for
header/TLS-only checks, plus Wayback / RSS (feedparser) / PDF (pdfplumber)
fallbacks. Targets come from knowledge/sources.yaml (ordered, trust-weighted).
Output: normalized `AwardQuote` with full provenance.

Not implemented in Phase 0.
"""

from __future__ import annotations

from typing import Optional

from ...domain.models import Layer
from ..base import ProviderHealth, Query, Quote


class AggregatorProvider:
    name = "aggregator"
    trust = 0.55  # raw scrape; verified/cross-checked before it counts

    def capabilities(self) -> set[Layer]:
        return {Layer.AWARD, Layer.CHARTS}

    def health(self) -> ProviderHealth:
        return ProviderHealth.DOWN  # lands in Phase 1

    def remaining_quota(self) -> Optional[int]:
        return 0

    def fetch(self, q: Query) -> list[Quote]:
        return []
