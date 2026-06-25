"""Curated provider — Layer 4 ratios/charts from versioned YAML (§4, §6).

Serves:
  - CHARTS: Capital One transfer ratios + partner award-chart costs (as
    `AwardQuote`s flagged `no_live_space`, since a chart proves a price, not a
    bookable seat).
  - FARES:  a low-trust fallback "price-to-beat" flagged `hardcoded_fallback`,
    used only when the Amadeus provider is unavailable (graceful degradation).

This is the Phase 0 default source. The aggregator (Engine A, Phase 1) will
emit the *same* `AwardQuote` contract from real scrapes, so the verification
core can cross-check curated vs. scraped without code changes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from ..domain.charts import lookup_award_miles
from ..domain.models import (
    AwardQuote,
    FareQuote,
    Layer,
    Provenance,
    Route,
    TransferRatio,
)
from .base import ProviderHealth, Query, Quote

_KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "knowledge"


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class CuratedProvider:
    name = "curated"
    trust = 0.7  # below live APIs, above raw aggregator scrapes

    def __init__(self, knowledge_dir: Optional[Path] = None) -> None:
        self._dir = Path(knowledge_dir) if knowledge_dir else _KNOWLEDGE_DIR
        self._ratios = self._load("ratios.yaml")
        self._charts = self._load("charts.yaml")
        self._fares = self._load("fares.yaml")

    def _load(self, filename: str) -> dict:
        path = self._dir / filename
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    # --- Provider interface ------------------------------------------------ #
    def capabilities(self) -> set[Layer]:
        return {Layer.CHARTS, Layer.FARES}

    def health(self) -> ProviderHealth:
        return ProviderHealth.HEALTHY

    def remaining_quota(self) -> Optional[int]:
        return None  # local YAML: effectively unlimited

    def fetch(self, q: Query) -> list[Quote]:
        if q.layer == Layer.CHARTS:
            return [*self._transfer_ratios(q), *self._award_quotes(q)]
        if q.layer == Layer.FARES:
            return self._fallback_fare(q)
        return []

    # --- ratios ------------------------------------------------------------ #
    def _transfer_ratios(self, q: Query) -> list[TransferRatio]:
        if not self._ratios:
            return []
        from_currency = self._ratios.get("from_currency", "capital_one")
        if q.currency and q.currency != from_currency:
            return []
        prov = Provenance(
            source_name=self._ratios.get("source", "curated ratios"),
            source_url=self._ratios.get("url"),
            trust=float(self._ratios.get("trust", 1.0)),
            source_updated_at=_parse_date(self._ratios.get("updated_at")),
        )
        out: list[TransferRatio] = []
        for program, ratio in (self._ratios.get("partners") or {}).items():
            if q.programs and program not in q.programs:
                continue
            out.append(
                TransferRatio(
                    from_currency=from_currency,
                    to_program=program,
                    ratio=float(ratio),
                    provenance=prov,
                    confidence=float(self._ratios.get("trust", 1.0)),
                )
            )
        return out

    # --- award chart costs ------------------------------------------------- #
    def _award_quotes(self, q: Query) -> list[AwardQuote]:
        region_map = self._charts.get("region_map", {})
        programs = self._charts.get("programs", {})
        out: list[AwardQuote] = []
        for program, spec in programs.items():
            if q.programs and program not in q.programs:
                continue
            hit = lookup_award_miles(program, spec, q.route, region_map)
            if hit is None:
                continue
            trust = float(spec.get("trust", 0.6))
            prov = Provenance(
                source_name=spec.get("source", f"{program} chart"),
                source_url=spec.get("url"),
                trust=trust,
                source_updated_at=_parse_date(spec.get("updated_at")),
            )
            out.append(
                AwardQuote(
                    program=program,
                    route=q.route,
                    miles=hit.miles,
                    seats_available=None,  # chart-only: availability unknown
                    provenance=prov,
                    confidence=trust,
                    flags=["no_live_space", *hit.flags],
                )
            )
        return out

    # --- fallback fares ---------------------------------------------------- #
    def _fallback_fare(self, q: Query) -> list[FareQuote]:
        table = self._fares.get("fares", {})
        cents = table.get(q.route.key())
        if cents is None:
            return []
        prov = Provenance(source_name="curated fallback fares", trust=0.3)
        return [
            FareQuote(
                route=q.route,
                cash_cents=int(cents),
                provenance=prov,
                confidence=0.3,
                flags=["hardcoded_fallback"],
            )
        ]
