"""Travelpayouts/Aviasales — L2 cached cash fares (free fallback). §5, Phase 2

Serves pre-fetched cached fares from `knowledge/travelpayouts_cache.yaml` as an
ordered fallback between Amadeus (live, trust 0.9) and curated hardcoded fares
(trust 0.3). Always HEALTHY — no API key required — so federation degradation
demos work offline. When a live Travelpayouts token is added later, this can
fetch fresh data and fall back to the cache on error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml

from ..domain.models import FareQuote, Layer, Provenance
from .base import ProviderHealth, Query, Quote

_KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"


class TravelpayoutsProvider:
    name = "travelpayouts"
    trust = 0.6

    def __init__(self, knowledge_dir: Optional[Path] = None) -> None:
        self._dir = Path(knowledge_dir) if knowledge_dir else _KNOWLEDGE_DIR
        self._cache = self._load_cache()

    def _load_cache(self) -> dict[str, int]:
        path = self._dir / "travelpayouts_cache.yaml"
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return dict(data.get("fares") or {})

    def capabilities(self) -> set[Layer]:
        return {Layer.FARES}

    def health(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY if self._cache else ProviderHealth.DOWN

    def remaining_quota(self) -> Optional[int]:
        return None  # registry tracks monthly quota from providers.yaml

    def fetch(self, q: Query) -> list[Quote]:
        if q.layer != Layer.FARES:
            return []
        cents = self._cache.get(q.route.key())
        if cents is None:
            return []
        prov = Provenance(
            source_name="Travelpayouts cached fares",
            source_url="https://www.travelpayouts.com/",
            trust=self.trust,
        )
        return [
            FareQuote(
                route=q.route,
                cash_cents=int(cents),
                provenance=prov,
                confidence=self.trust,
                flags=["cached_fare"],
            )
        ]
