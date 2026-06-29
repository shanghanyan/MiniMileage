"""AggregatorProvider — Engine A as a first-class Provider (§5, §6).

The DEFAULT Layer 3 (award space) + Layer 4 (charts) source from Phase 1. It
fetches every configured target, parses each into normalized rows, resolves
them against the requested route, and emits `AwardQuote`s with full provenance —
the *same* contract every other provider uses, so the verification core cannot
tell a scrape from an API call (§2.2).

  - CHARTS  -> chart-derived `AwardQuote` (seats unknown -> `no_live_space`),
               one per (program, source) so independent sources cross-check.
  - AWARD   -> live `AwardQuote` with `seats_available` set (no `no_live_space`);
               this is what clears Demo B's chart-only caveat.

Carried-over fixes live here: rt->ow normalization, freshness de-dupe, and
trust-weighted reconciliation (delegated to the verification core, which now
sees curated + scraped as genuinely independent sources).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from ...domain.charts import lookup_award_miles
from ...domain.models import AwardQuote, Layer, Provenance, Route
from ..base import ProviderHealth, Query, Quote
from .fetch import Fetcher
from .parse import (
    RawAwardRow,
    RawChartRow,
    normalize_one_way,
    parse_award_json,
    parse_chart_html,
    parse_chart_json,
    parse_rss,
)
from .politeness import PolitenessPolicy
from .sources import Target, load_targets, validate_targets

log = logging.getLogger("mileage.aggregator")

_KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent.parent / "knowledge"


def _parse_date(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            dt = datetime.strptime(text, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class AggregatorProvider:
    name = "aggregator"
    trust = 0.6  # raw scrape; verified/cross-checked before it counts

    def __init__(
        self,
        *,
        sources_path: Optional[Path] = None,
        knowledge_dir: Optional[Path] = None,
        fetcher: Optional[Fetcher] = None,
        enabled: bool = True,
    ) -> None:
        self._dir = Path(knowledge_dir) if knowledge_dir else _KNOWLEDGE_DIR
        self._sources_path = (
            Path(sources_path) if sources_path else self._dir / "sources.yaml"
        )
        self.enabled = enabled
        self._region_map = self._load_region_map()
        self.targets: list[Target] = load_targets(self._sources_path)
        self.fetcher = fetcher or Fetcher(
            politeness=PolitenessPolicy(), base_dir=self._dir
        )

    # --- Provider interface ------------------------------------------------ #
    def capabilities(self) -> set[Layer]:
        return {Layer.AWARD, Layer.CHARTS}

    def health(self) -> ProviderHealth:
        if not self.enabled or not self.targets:
            return ProviderHealth.DOWN
        if all(not t.healthy() for t in self.targets):
            return ProviderHealth.DOWN
        if any(not t.healthy() for t in self.targets):
            return ProviderHealth.DEGRADED
        return ProviderHealth.HEALTHY

    def remaining_quota(self) -> Optional[int]:
        return None  # scraping public targets; no fixed API quota

    def fetch(self, q: Query) -> list[Quote]:
        if self.health() == ProviderHealth.DOWN:
            return []
        wanted = set(p.lower() for p in q.programs) if q.programs else None
        if q.layer == Layer.CHARTS:
            return self._fetch_charts(q.route, wanted)
        if q.layer == Layer.AWARD:
            return self._fetch_award(q.route, wanted)
        return []

    def validate_urls(self) -> list[Target]:
        """Run the `--validate-urls` health check (records last_404)."""
        return validate_targets(self.targets, self.fetcher)

    # --- helpers ----------------------------------------------------------- #
    def _load_region_map(self) -> dict[str, str]:
        path = self._dir / "charts.yaml"
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {k.upper(): v for k, v in (data.get("region_map") or {}).items()}

    def _targets_for(self, provides: str) -> list[Target]:
        return [t for t in self.targets if t.provides == provides and t.healthy()]

    def _read(self, target: Target):
        result = self.fetcher.get(target.url)
        if result is None or not result.ok:
            log.info("aggregator: no data from %s", target.name)
            return None
        return result

    def _provenance(self, target: Target, result, updated_at) -> Provenance:
        flags_url = result.final_url if result.via in ("wayback", "file") else target.url
        return Provenance(
            source_name=target.name,
            source_url=flags_url,
            trust=target.trust,
            source_updated_at=_parse_date(updated_at or target.updated_at),
        )

    # --- L4 charts --------------------------------------------------------- #
    def _fetch_charts(
        self, route: Route, wanted: Optional[set]
    ) -> list[AwardQuote]:
        out: list[AwardQuote] = []
        for target in self._targets_for("chart"):
            result = self._read(target)
            if result is None:
                continue
            rows = self._parse_chart_rows(target, result.text)
            chart_by_program = self._build_charts(rows)
            for program, chart in chart_by_program.items():
                if wanted and program not in wanted:
                    continue
                hit = lookup_award_miles(program, chart, route, self._region_map)
                if hit is None:
                    continue
                prov = self._provenance(
                    target, result, chart.get("_updated_at")
                )
                flags = ["no_live_space", *hit.flags]
                if "from_wayback" in result.flags:
                    flags.append("from_wayback")
                out.append(
                    AwardQuote(
                        program=program,
                        route=route,
                        miles=hit.miles,
                        seats_available=None,
                        provenance=prov,
                        confidence=target.trust,
                        flags=flags,
                    )
                )
        return out

    def _parse_chart_rows(self, target: Target, text: str) -> list[RawChartRow]:
        if target.format == "html_table":
            return parse_chart_html(text, updated_at=target.updated_at)
        if target.format == "json":
            return parse_chart_json(text)
        if target.format == "rss":
            charts, _ = parse_rss(text)
            return charts
        return []  # pdf path is optional; degrades to empty without pdfplumber

    def _build_charts(self, rows: list[RawChartRow]) -> dict[str, dict]:
        """Group raw rows into per-program chart specs for lookup_award_miles."""
        charts: dict[str, dict] = {}
        for r in rows:
            spec = charts.setdefault(r.program, {"bands": [], "_updated_at": None})
            spec["bands"].append(
                {
                    "regions": [r.region_a, r.region_b],
                    "roundtrip": r.roundtrip,
                    "miles": {r.cabin: r.miles},
                }
            )
            if r.updated_at and not spec["_updated_at"]:
                spec["_updated_at"] = r.updated_at
        return charts

    # --- L3 live award space ---------------------------------------------- #
    def _fetch_award(
        self, route: Route, wanted: Optional[set]
    ) -> list[AwardQuote]:
        out: list[AwardQuote] = []
        for target in self._targets_for("award"):
            result = self._read(target)
            if result is None:
                continue
            rows = self._parse_award_rows(target, result.text)
            rows = self._dedupe_fresh(rows)
            for r in rows:
                if r.origin != route.origin or r.dest != route.dest:
                    continue
                if r.cabin != route.cabin.value:
                    continue
                if wanted and r.program not in wanted:
                    continue
                miles, norm_flags = normalize_one_way(r.miles, r.roundtrip)
                prov = self._provenance(target, result, r.updated_at)
                flags = ["live_award_space", *norm_flags]
                if "from_wayback" in result.flags:
                    flags.append("from_wayback")
                out.append(
                    AwardQuote(
                        program=r.program,
                        route=route,
                        miles=miles,
                        seats_available=r.seats,
                        provenance=prov,
                        confidence=target.trust,
                        flags=flags,
                    )
                )
        return out

    def _parse_award_rows(self, target: Target, text: str) -> list[RawAwardRow]:
        if target.format == "json":
            return parse_award_json(text)
        if target.format == "rss":
            _, awards = parse_rss(text)
            return awards
        return []

    @staticmethod
    def _dedupe_fresh(rows: list[RawAwardRow]) -> list[RawAwardRow]:
        """Keep the freshest row per (program, origin, dest, cabin) (§6)."""
        best: dict[tuple, RawAwardRow] = {}
        for r in rows:
            key = (r.program, r.origin, r.dest, r.cabin)
            cur = best.get(key)
            if cur is None:
                best[key] = r
                continue
            new_d = _parse_date(r.updated_at)
            cur_d = _parse_date(cur.updated_at)
            if new_d and (cur_d is None or new_d > cur_d):
                best[key] = r
        return list(best.values())
