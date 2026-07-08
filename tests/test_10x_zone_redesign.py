"""Regression tests for the 10xtravel Turkish/ANA parsers (§6, 2026-07-06).

Both 10xtravel blog fallback pages were confirmed redesigned on 2026-07-06
(live-scrape debugging session): the old from/to/cabin `html_table_wide`
schema `parse_chart_html_wide` looks for no longer exists on either page.

  - Turkish is now a region-NUMBER zone x zone matrix, one table per cabin,
    with the cabin named in a clean preceding <h3> heading ("Economy Class").
    The SAME page also has a same-shaped UPGRADE-cost matrix ("Economy to
    Business Class") that must NOT be mistaken for an award chart.
    -> `parse_chart_html_zone_matrix` / format `html_table_zone_matrix`.

  - ANA is now a zone legend + many small round-trip "season x cabin" tables,
    one per zone pair, the pair named in a preceding "Routes between X (Zone
    N) and Y (Zone M)" <p> caption. Scope: only the ANA-operated
    international zone-pair tables are parsed (the domestic distance-band
    tables and partner-operated departure tables use two further shapes,
    left for a follow-up).
    -> `parse_chart_html_seasonal_zones` / format `html_table_seasonal_zones`.

Fixtures (`10x_turkish_zone_matrix.html`, `10x_ana_seasonal_zones.html`) are
TRIMMED SLICES of the real live pages (fetched 2026-07-06 via
`scripts/dump_scrape_target_shapes.py`), not synthetic mockups — so these
tests exercise the parser against the actual production HTML shape, the same
discipline `test_pdf_chart.py`'s offline tests follow for the official PDFs.

Run offline:  python tests/test_10x_zone_redesign.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("MILEAGE_OFFLINE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.domain.charts import lookup_award_miles
from mileage.domain.models import Cabin, Route
from mileage.providers.aggregator.fetch import FetchResult
from mileage.providers.aggregator.live_scrape import check_target
from mileage.providers.aggregator.parse import (
    parse_chart_html_seasonal_zones,
    parse_chart_html_zone_matrix,
)
from mileage.providers.aggregator.provider import AggregatorProvider
from mileage.providers.aggregator.sources import Target

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "mileage" / "knowledge"
_FIXTURES = _KNOWLEDGE / "fixtures"


# --------------------------------------------------------------------------- #
# Turkish: region-number zone matrix, cabin-per-table via heading
# --------------------------------------------------------------------------- #
def test_turkish_zone_matrix_parses_economy_and_business_only() -> None:
    html = (_FIXTURES / "10x_turkish_zone_matrix.html").read_text(encoding="utf-8")
    stats: dict = {}
    rows = parse_chart_html_zone_matrix(html, program="turkish", stats=stats)
    assert stats.get("html_route") == "zone_matrix"
    assert stats.get("dropped", 0) == 0
    assert rows, "expected rows from the real zone-matrix fixture"

    # The fixture's THIRD table is "Economy to Business Class" (an UPGRADE
    # cost matrix, same grid shape as the award charts). It must be excluded
    # entirely — no 'first' cabin appears in this fixture at all, and if the
    # upgrade table leaked in it would silently double-count/mislabel rows.
    cabins = {r.cabin for r in rows}
    assert cabins == {"economy", "business"}

    by = {
        (r.region_a, r.region_b, r.cabin): r.miles
        for r in rows
    }
    # Region 1 (Domestic) -> Region 1: economy 4,500 (the "4.5" thousands cell).
    assert by[("turkish_zone_1", "turkish_zone_1", "economy")] == 4500
    # Region 1 -> Region 10 (North America): economy 40,000, business 65,000.
    assert by[("turkish_zone_1", "turkish_zone_10", "economy")] == 40000
    assert by[("turkish_zone_1", "turkish_zone_10", "business")] == 65000


def test_turkish_zone_matrix_rejects_bare_matrix_with_no_cabin_heading() -> None:
    """A zone x zone matrix with no clean '<Cabin> Class' heading in front is
    a selector miss (never guessed), not a crash."""
    html = (
        "<html><body><table><tr><th>Region of departure</th><th>1</th>"
        "<th>2</th></tr><tr><td>1</td><td>10</td><td>20</td></tr>"
        "<tr><td>2</td><td></td><td>15</td></tr></table></body></html>"
    )
    rows = parse_chart_html_zone_matrix(html, program="turkish")
    assert rows == []


# --------------------------------------------------------------------------- #
# ANA: zone legend + round-trip season x cabin tables per zone pair
# --------------------------------------------------------------------------- #
def test_ana_seasonal_zones_parses_low_season_and_drops_ambiguous_zone() -> None:
    html = (_FIXTURES / "10x_ana_seasonal_zones.html").read_text(encoding="utf-8")
    stats: dict = {}
    rows = parse_chart_html_seasonal_zones(html, program="ana", stats=stats)
    assert stats.get("html_route") == "seasonal_zones"

    # Japan (Zone 1) <-> South Korea/Russia 1 (Zone 2): both canonicalize
    # cleanly to north_asia via the EXISTING region_map vocabulary (§A) — no
    # new zone-token machinery needed since these are real geographic names.
    by = {(r.region_a, r.region_b, r.cabin): r.miles for r in rows}
    assert by[("north_asia", "north_asia", "economy")] == 12000  # low season
    assert by[("north_asia", "north_asia", "business")] == 25000
    assert all(r.roundtrip for r in rows), "ANA charts are round-trip"

    # Japan <-> "Asia 1" (Zone 3): "Asia 1" is the program's OWN zone label,
    # not a real region name our canonicalizer knows (its geography spans
    # both north_asia AND southeast_asia elsewhere in this codebase) — MUST
    # be dropped + counted, never guessed a single bucket (§A contract).
    assert stats.get("dropped", 0) >= 1
    assert len(rows) == 2  # only the Japan<->Korea pair's 2 cabins survive


def test_ana_seasonal_zones_resolves_a_real_route() -> None:
    """Parsed rows resolve through the SAME region_map + lookup_award_miles
    every other chart uses — no separate resolution path for this format."""
    html = (_FIXTURES / "10x_ana_seasonal_zones.html").read_text(encoding="utf-8")
    rows = parse_chart_html_seasonal_zones(html, program="ana")
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    chart = agg._build_charts(rows).get("ana")
    assert chart is not None

    # NRT (Tokyo) and ICN (Seoul) both canonicalize to north_asia already
    # (knowledge/charts.yaml::region_map) — a real Japan<->Korea route. The
    # fixture's low-season business cell is 25,000 ROUND-TRIP; the resolver
    # halves it to 12,500 one-way and flags the normalization (§6).
    hit = lookup_award_miles(
        "ana", chart, Route("NRT", "ICN", Cabin.BUSINESS),
        agg._region_map, airport_coords=agg._airport_coords,
    )
    assert hit is not None and hit.miles == 12500
    assert "rt_to_ow_normalized" in hit.flags


# --------------------------------------------------------------------------- #
# End-to-end: the live-scrape dispatch path (provider.format wiring), not just
# the parser function directly — guards against a format-string/wiring typo
# the same way test_live_scrape_check_target.py guards `_resolve_detail`.
# --------------------------------------------------------------------------- #
class _FakeFetcher:
    offline = False

    def __init__(self, url: str, body: str) -> None:
        self._url, self._body = url, body

    def get(self, url: str):
        if url != self._url:
            return None
        return FetchResult(
            url=url, text=self._body, status=200, final_url=url, via="file",
            raw=self._body.encode("utf-8"),
        )


def test_turkish_zone_matrix_resolves_probe_route() -> None:
    """With turkish program_zones, the zone-matrix fallback resolves LAX→IST."""
    html = (_FIXTURES / "10x_turkish_zone_matrix.html").read_text(encoding="utf-8")
    rows = parse_chart_html_zone_matrix(html, program="turkish")
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    chart = agg._build_charts(rows).get("turkish")
    assert chart is not None
    hit = lookup_award_miles(
        "turkish", chart, Route("LAX", "IST", Cabin.BUSINESS),
        agg._region_map, program_zones=agg._program_zones,
    )
    assert hit is not None and hit.miles == 65000


def test_check_target_dispatches_html_table_zone_matrix_format() -> None:
    url = "https://example.test/turkish-zone-matrix"
    body = (_FIXTURES / "10x_turkish_zone_matrix.html").read_text(encoding="utf-8")
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    agg.fetcher = _FakeFetcher(url, body)

    target = Target(
        name="10x-turkish-test", url=url, format="html_table_zone_matrix",
        provides="chart", program="turkish", role="fallback",
    )
    res = check_target(target, agg)
    assert res.rows > 0, res.detail
    assert res.status == "ok", res.detail
    assert res.resolved is not None


def test_check_target_dispatches_html_table_seasonal_zones_format() -> None:
    url = "https://example.test/ana-seasonal-zones"
    body = (_FIXTURES / "10x_ana_seasonal_zones.html").read_text(encoding="utf-8")
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    agg.fetcher = _FakeFetcher(url, body)

    target = Target(
        name="10x-ana-test", url=url, format="html_table_seasonal_zones",
        provides="chart", program="ana", role="fallback",
    )
    res = check_target(target, agg)
    assert res.status == "ok", res.detail
    assert res.rows > 0
    assert res.resolved is not None


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  XX  {fn.__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
