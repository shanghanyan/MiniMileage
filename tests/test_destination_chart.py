"""Destination-table charts — hub-based per-destination guide pages (§6).

Turkish Miles&Smiles and Singapore KrisFlyer publish "guide" pages that are NOT
from/to region charts but hub-based per-destination tables:

    Destination | Code | Economy<tier> | Business<tier> | First<tier> | ...

origin is IMPLICITLY the program hub (IST / SIN). The key correctness property
these tests lock in: prices vary *within* a region (IST->LAX 100k biz vs
IST->ORD 90k biz; SIN->LAX 112.5k vs SIN->JFK 117k), so the parser keys each row
on its exact IATA code and the resolver returns the queried city's own price
instead of collapsing a region to one (wrong) number.

Runs standalone (`python tests/test_destination_chart.py`) and under pytest.
Everything is OFFLINE and deterministic — the fixtures are trimmed real pages.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("MILEAGE_OFFLINE", "1")
os.environ.pop("MILEAGE_REDIS_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.domain.charts import lookup_award_miles
from mileage.domain.models import Cabin, Layer, Route
from mileage.providers.aggregator.fetch import FetchResult
from mileage.providers.aggregator.parse import parse_chart_destination_table
from mileage.providers.aggregator.provider import AggregatorProvider
from mileage.providers.aggregator.sources import Target, load_targets
from mileage.providers.base import Query

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "mileage" / "knowledge"
_FIXTURES = _KNOWLEDGE / "fixtures"


class _FakeFetcher:
    """URL -> fixture body. No network, ever (mirrors test_phase8b)."""

    def __init__(self, pages: dict) -> None:
        self.pages = pages

    def head_ok(self, url: str):
        p = self.pages.get(url)
        return (p is not None, 200 if p else 0)

    def get(self, url: str):
        p = self.pages.get(url)
        if p is None:
            return None
        return FetchResult(url=url, text=p, status=200, final_url=url, via="file")


# --------------------------------------------------------------------------- #
# Parser: schema, cabin-tier mapping, decoy skip, drop-counting
# --------------------------------------------------------------------------- #
def test_turkish_guide_parses_exact_airport_rows() -> None:
    html = (_FIXTURES / "atf_turkish_guide.html").read_text(encoding="utf-8")
    stats: dict = {}
    rows = parse_chart_destination_table(
        html, program="turkish", hub="IST", stats=stats
    )
    assert rows, "no rows parsed from Turkish guide"

    # Every row is keyed on the hub + an exact destination IATA code, no regions.
    assert all(r.origin_airport == "IST" for r in rows)
    assert all(r.region_a is None and r.region_b is None for r in rows)
    assert all(len(r.dest_airport) == 3 for r in rows)

    by = {(r.dest_airport, r.cabin): r.miles for r in rows}
    # Intra-region variance is preserved (this is the whole point).
    assert by[("LAX", "business")] == 100000
    assert by[("ORD", "business")] == 90000
    # '—' cells are selector misses -> no row (ORD/AMS have no first class).
    assert ("ORD", "first") not in by
    assert by[("LAX", "first")] == 135000
    # The decoy nav table (no cabin columns) is skipped; the codeless row drops.
    assert stats["dropped"] >= 1
    assert ("TBD", "business") not in by


def test_krisflyer_guide_cabin_and_mixed_region_table() -> None:
    html = (_FIXTURES / "atf_krisflyer_guide.html").read_text(encoding="utf-8")
    rows = parse_chart_destination_table(html, program="krisflyer", hub="SIN")
    by = {(r.dest_airport, r.cabin): r.miles for r in rows}
    # 'First/SuitesSaver' maps to first; 'BusinessSaver' to business.
    assert by[("LAX", "business")] == 112500
    assert by[("JFK", "business")] == 117000  # differs from LAX within NA
    assert by[("AMS", "first")] == 148000
    # Mixed-region "Asia" table still yields both rows, keyed by code.
    assert by[("AMD", "business")] == 45000
    assert by[("BKK", "economy")] == 8000


def test_missing_hub_yields_nothing() -> None:
    html = (_FIXTURES / "atf_turkish_guide.html").read_text(encoding="utf-8")
    assert parse_chart_destination_table(html, program="turkish", hub="") == []


# --------------------------------------------------------------------------- #
# Resolver: exact O->D precision (never region-collapse)
# --------------------------------------------------------------------------- #
def test_exact_airport_resolution_beats_region_collapse() -> None:
    html = (_FIXTURES / "atf_turkish_guide.html").read_text(encoding="utf-8")
    rows = parse_chart_destination_table(html, program="turkish", hub="IST")
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    chart = agg._build_charts(rows)["turkish"]

    def biz(o, d):
        hit = lookup_award_miles(
            "turkish", chart, Route(o, d, Cabin.BUSINESS),
            agg._region_map, airport_coords=agg._airport_coords,
        )
        return hit.miles if hit else None

    # Two North America cities, same region, DIFFERENT prices — region-collapse
    # would return one number for both; exact-airport matching returns each.
    assert biz("LAX", "IST") == 100000
    assert biz("ORD", "IST") == 90000
    # Order-independent (region pairs are unordered): IST->LAX == LAX->IST.
    assert biz("IST", "LAX") == 100000
    # A NA city NOT on the guide does not resolve from this source (honest miss,
    # not a guessed region rate).
    assert biz("SEA", "IST") is None


# --------------------------------------------------------------------------- #
# End-to-end through the provider (the same contract every provider uses)
# --------------------------------------------------------------------------- #
def test_destination_target_flows_through_provider() -> None:
    url = "https://awardtravelfinder.com/turkish-miles-guides"
    fixture = (_FIXTURES / "atf_turkish_guide.html").read_text(encoding="utf-8")
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    agg.fetcher = _FakeFetcher({url: fixture})
    agg.targets = [
        Target(
            name="atf-turkish-chart", url=url, format="html_table_destination",
            program="turkish", hub="IST", provides="chart", trust=0.80,
            updated_at="2026-06-29",
        )
    ]
    quotes = agg.fetch(
        Query(route=Route("LAX", "IST", Cabin.BUSINESS), layer=Layer.CHARTS,
              programs=["turkish"])
    )
    assert quotes, "provider produced no quote for LAX->IST business"
    q = quotes[0]
    assert q.program == "turkish"
    assert q.miles == 100000
    assert q.seats_available is None
    assert "no_live_space" in q.flags  # L4 chart -> seat unknown
    assert q.provenance.source_name == "atf-turkish-chart"


def test_sources_yaml_marks_guides_as_destination_tables() -> None:
    targets = {t.name: t for t in load_targets(_KNOWLEDGE / "sources.yaml")}
    for name, hub in (("atf-turkish-chart", "IST"), ("atf-krisflyer-chart", "SIN")):
        t = targets[name]
        assert t.format == "html_table_destination", name
        assert t.hub == hub, name


def _run_all() -> int:
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  XX  {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  XX  {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
