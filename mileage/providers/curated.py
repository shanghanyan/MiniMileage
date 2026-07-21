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

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional, Union

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


def _parse_iso_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _bonus_active(
    *,
    valid_from: Optional[str],
    valid_until: Optional[str],
    today: date,
) -> bool:
    """Inclusive date window; missing bound = open on that side."""
    start = _parse_iso_date(valid_from)
    end = _parse_iso_date(valid_until)
    if start is not None and today < start:
        return False
    if end is not None and today > end:
        return False
    return True


def _partner_entries(
    _program: str,
    spec: Union[float, int, dict],
    *,
    today: date,
) -> list[tuple[float, float, list[str], Optional[str], Optional[str], Optional[str]]]:
    """Expand a partners.yaml value into (ratio, bonus_mult, flags, from, until, label).

    A bare float is the base rate only. A map may include ``bonus`` /
    ``bonus_multiplier`` plus an optional window; we emit the base row always
    and an extra bonus row when the window is active.
    """
    if isinstance(spec, (int, float)):
        return [(float(spec), 1.0, [], None, None, None)]

    if not isinstance(spec, dict):
        return []

    ratio = float(spec.get("ratio", 1.0))
    rows: list[tuple[float, float, list[str], Optional[str], Optional[str], Optional[str]]] = [
        (ratio, 1.0, [], None, None, None)
    ]

    bonus = spec.get("bonus", spec.get("bonus_multiplier"))
    if bonus is None:
        return rows

    bonus_mult = float(bonus)
    if bonus_mult == 1.0:
        return rows

    valid_from = spec.get("valid_from")
    valid_until = spec.get("valid_until")
    if not _bonus_active(valid_from=valid_from, valid_until=valid_until, today=today):
        return rows

    pct = int(round((bonus_mult - 1.0) * 100))
    label = spec.get("label") or f"+{pct}% transfer bonus"
    rows.append(
        (
            ratio,
            bonus_mult,
            ["transfer_bonus"],
            str(valid_from) if valid_from else None,
            str(valid_until) if valid_until else None,
            str(label),
        )
    )
    return rows


class CuratedProvider:
    name = "curated"
    trust = 0.7  # below live APIs, above raw aggregator scrapes

    def __init__(
        self,
        knowledge_dir: Optional[Path] = None,
        *,
        as_of: Optional[date] = None,
    ) -> None:
        self._dir = Path(knowledge_dir) if knowledge_dir else _KNOWLEDGE_DIR
        self._ratios = self._load("ratios.yaml")
        self._charts = self._load("charts.yaml")
        self._fares = self._load("fares.yaml")
        # Injectable "today" so bonus windows are hermetic under tests.
        self._as_of = as_of or date.today()

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
        """Ratios for every configured currency, filtered by `q.currency`.

        `ratios.yaml` holds one top-level currency block (capital_one, kept at
        the top level for backward compatibility) plus an optional `currencies:`
        list of additional blocks (§13 Phase 7 data expansion) — each block is
        the same shape. Partners may be a bare float or a map with optional
        time-boxed ``bonus`` / ``bonus_multiplier``.
        """
        if not self._ratios:
            return []
        blocks = [self._ratios, *(self._ratios.get("currencies") or [])]
        out: list[TransferRatio] = []
        for block in blocks:
            from_currency = block.get("from_currency", "capital_one")
            if q.currency and q.currency != from_currency:
                continue
            prov = Provenance(
                source_name=block.get("source", "curated ratios"),
                source_url=block.get("url"),
                trust=float(block.get("trust", 1.0)),
                source_updated_at=_parse_date(block.get("updated_at")),
            )
            for program, spec in (block.get("partners") or {}).items():
                if q.programs and program not in q.programs:
                    continue
                for ratio, bonus_mult, flags, v_from, v_until, label in _partner_entries(
                    program, spec, today=self._as_of
                ):
                    out.append(
                        TransferRatio(
                            from_currency=from_currency,
                            to_program=program,
                            ratio=ratio,
                            provenance=prov,
                            confidence=float(block.get("trust", 1.0)),
                            flags=list(flags),
                            bonus_multiplier=bonus_mult,
                            valid_from=v_from,
                            valid_until=v_until,
                            bonus_label=label,
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
            hit = lookup_award_miles(
                program, spec, q.route, region_map,
                program_zones=self._charts.get("program_zones"),
            )
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
