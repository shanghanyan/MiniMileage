"""PDF award-chart parsing — wide, Aeroplan distance, zone matrix (§6).

Offline tests use a fake pdfplumber stand-in; ``test_live_aeroplan_official_pdf``
and ``test_live_krisflyer_official_pdf`` hit the real CDN URLs when
``MILEAGE_OFFLINE=0``.

Run offline:  python tests/test_pdf_chart.py
Run live:     MILEAGE_OFFLINE=0 python tests/test_pdf_chart.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("MILEAGE_OFFLINE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.domain.charts import lookup_award_miles
from mileage.domain.models import Cabin, Route
from mileage.providers.aggregator.fetch import Fetcher
from mileage.providers.aggregator.parse import parse_chart_pdf
from mileage.providers.aggregator.provider import AggregatorProvider

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "mileage" / "knowledge"
_OFFLINE = os.environ.get("MILEAGE_OFFLINE", "1") not in ("0", "false", "no")

# Reuse the pdfplumber fake from test_phase8b.
from tests.test_phase8b import _install_fake_pdfplumber  # noqa: E402


def test_offline_aeroplan_distance_pdf_rows() -> None:
    """Aeroplan official PDF shape: page zone title + distance/partner grid."""
    tables = [[
        ["distance (miles)", "operated by", "economy", None, "business", None],
        [
            "0 - 4,000",
            "Air Canada\nand/or Select Partners",
            "Starting at\n32,500 pts",
            "Median:\n40,000 pts",
            "Starting at\n60,000 pts",
            "Median:\n129,300 pts",
        ],
        [None, "All other partners", "35,000 pts", None, "60,000 pts", None],
    ]]
    restore = _install_fake_pdfplumber(
        tables,
        page_text="Between North America and Atlantic zones\n",
    )
    try:
        stats: dict = {}
        rows = parse_chart_pdf(
            b"%PDF-1.4 fake", program="aeroplan", stats=stats,
        )
    finally:
        restore()
    assert stats.get("pdf_route") == "aeroplan_distance"
    miles = {
        (r.region_a, r.region_b, r.cabin, r.distance_min, r.distance_max): r.miles
        for r in rows
    }
    assert miles[("north_america", "europe", "economy", 0, 4000)] == 35000
    assert miles[("north_america", "europe", "business", 0, 4000)] == 60000


def test_offline_krisflyer_zone_matrix_resolves() -> None:
    """Zone-matrix PDF rows resolve via program_zones in charts.yaml."""
    tables = [[
        ["", "1", "13"],
        ["zone 1: singapore", "-", "46"],
        [None, "-", "^"],
        [None, "-", "117"],
        [None, "-", "156"],
    ]]
    restore = _install_fake_pdfplumber(
        tables,
        page_text="One-way Saver Awards\n",
    )
    try:
        rows = parse_chart_pdf(b"%PDF-1.4 fake", program="krisflyer")
    finally:
        restore()
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    chart = agg._build_charts(rows).get("krisflyer")
    assert chart is not None
    hit = lookup_award_miles(
        "krisflyer", chart, Route("SIN", "JFK", Cabin.BUSINESS),
        agg._region_map, program_zones=agg._program_zones,
    )
    assert hit is not None and hit.miles == 117000


def test_offline_lifemiles_text_pdf_rows() -> None:
    """2022 Thrifty Traveler LifeMiles PDF — text layout, no extractable tables."""
    import httpx

    url = (
        "https://thriftytraveler.com/wp-content/uploads/2022/06/"
        "Avianca-Redemption-Table.pdf"
    )
    try:
        raw = httpx.get(url, follow_redirects=True, timeout=30).content
    except Exception as exc:
        import pytest
        pytest.skip(f"network unavailable: {exc}")

    stats: dict = {}
    rows = parse_chart_pdf(raw, program="lifemiles", stats=stats)
    assert stats.get("pdf_route") == "lifemiles_text"
    assert len(rows) >= 100
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    chart = agg._build_charts(rows).get("lifemiles")
    assert chart is not None
    hit = lookup_award_miles(
        "lifemiles", chart, Route("LAX", "IST", Cabin.BUSINESS),
        agg._region_map, program_zones=agg._program_zones,
    )
    assert hit is not None and hit.miles >= 50000


def _skip_if_offline(reason: str) -> bool:
    if not _OFFLINE:
        return False
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(reason)
    print(f"  skip ({reason})")
    return True


def test_live_aeroplan_official_pdf() -> None:
    if _skip_if_offline("set MILEAGE_OFFLINE=0 for live PDF fetch"):
        return
    url = (
        "https://www.aircanada.com/content/dam/aircanada/loyalty-content/"
        "documents/flight-rewards-chart-june2026-en.pdf"
    )
    result = Fetcher(offline=False).get(url)
    assert result is not None and result.ok and result.raw
    stats: dict = {}
    rows = parse_chart_pdf(result.raw, program="aeroplan", stats=stats)
    assert stats.get("pdf_route") == "aeroplan_distance"
    assert len(rows) >= 20, f"expected partner rows from official PDF, got {len(rows)}"
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    chart = agg._build_charts(rows).get("aeroplan")
    hit = lookup_award_miles(
        "aeroplan", chart, Route("LAX", "LHR", Cabin.BUSINESS),
        agg._region_map, airport_coords=agg._airport_coords,
        program_zones=agg._program_zones,
    )
    assert hit is not None and hit.miles >= 50000


def test_live_krisflyer_official_pdf() -> None:
    if _skip_if_offline("set MILEAGE_OFFLINE=0 for live PDF fetch"):
        return
    url = (
        "https://www.singaporeair.com/content/dam/sia/web-assets/pdfs/"
        "ppsclub-krisflyer/krisflyer/progupdates/awardcharts/"
        "SingaporeAirlinesOne-WayAdvantageSaverAwardChartupdated1Nov25.pdf"
    )
    result = Fetcher(offline=False).get(url)
    assert result is not None and result.ok and result.raw
    stats: dict = {}
    rows = parse_chart_pdf(result.raw, program="krisflyer", stats=stats)
    assert stats.get("pdf_route") == "zone_matrix"
    assert len(rows) >= 100
    cabins = {r.cabin for r in rows}
    assert "economy" in cabins and "business" in cabins
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    chart = agg._build_charts(rows).get("krisflyer")
    hit = lookup_award_miles(
        "krisflyer", chart, Route("SIN", "JFK", Cabin.BUSINESS),
        agg._region_map, program_zones=agg._program_zones,
    )
    assert hit is not None and hit.miles == 117000


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
