"""Phase 8 — discovery intake (email) + local extractor, as executable tests.

Runs standalone (`python tests/test_phase8.py`) and is pytest-discoverable.
Asserts the properties the plan promises for the aggregator's discovery mode
(§6.1/§6.2), all OFFLINE and deterministic:

  1. Verbatim-number grounding rejects any miles not literally in the source.
  2. The deterministic extractor turns newsletter prose into the right rows —
     and does NOT mistake a "100,000-point bonus" for an award price.
  3. Devaluation subjects flip the named program to `stale`.
  4. The email fixture ingests end-to-end into grounded rows.
  5. Discovered rows resolve through the SAME aggregator path as scraped URLs,
     flagged `llm_extracted` (so they can only ever be `tentative_best`).
  6. The wide HTML parser actually parses a REAL Award Travel Finder chart page
     (selecting the chart table among decoy nav/FAQ tables).
  7. Offline mode is truly network-free: live URLs resolve to nothing.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MILEAGE_OFFLINE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.config import Config
from mileage.domain.models import Cabin, Layer, Route
from mileage.providers.aggregator.extract import (
    DeterministicExtractor,
    number_is_grounded,
)
from mileage.providers.aggregator.fetch import Fetcher
from mileage.providers.aggregator.ingest import (
    detect_devaluation,
    load_discovered_rows,
    run_discovery,
    write_discovered,
)
from mileage.providers.aggregator.parse import parse_chart_html_wide
from mileage.providers.aggregator.provider import AggregatorProvider
from mileage.providers.base import Query

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "mileage" / "knowledge"
_FIXTURES = _KNOWLEDGE / "fixtures"


def test_grounding_rejects_invented_numbers() -> None:
    src = "Turkish business class to Europe is 45,000 miles one-way."
    assert number_is_grounded(45000, src)        # comma-insensitive
    assert number_is_grounded(45000, "45000 miles")
    assert number_is_grounded(45000, "45k miles")  # shorthand
    assert not number_is_grounded(44000, src)     # not in text -> rejected
    assert not number_is_grounded(0, src)


def test_extractor_reads_newsletter_prose() -> None:
    body = (_FIXTURES / "sample_newsletter.eml").read_text(encoding="utf-8")
    rows = DeterministicExtractor().extract(body)
    triples = {(r.program, r.region_a, r.region_b, r.cabin, r.miles) for r in rows}

    assert ("turkish", "north_america", "europe", "business", 45000) in triples
    assert ("lifemiles", "north_america", "europe", "economy", 30000) in triples
    assert any(
        r.program == "aeroplan" and r.cabin == "economy" and r.miles == 12500
        for r in rows
    )
    # The 100,000-point welcome bonus has no program/cabin/route -> never a row.
    assert all(r.miles != 100000 for r in rows)
    # Every emitted number is grounded in the source.
    for r in rows:
        assert number_is_grounded(r.miles, body)


def test_devaluation_subjects() -> None:
    assert detect_devaluation("Turkish Miles&Smiles devaluation incoming") == "turkish"
    assert detect_devaluation("Aeroplan award chart change for 2026") == "aeroplan"
    assert detect_devaluation("This week's award sweet spots") is None


def test_email_fixture_ingests_end_to_end() -> None:
    # Offline + no creds -> reads the .eml fixtures; never touches the network.
    config = Config(offline=True, knowledge_dir=_KNOWLEDGE)
    result = run_discovery(config, fixture_dir=_FIXTURES)

    assert result.used_fixtures is True
    assert result.documents, "no documents ingested from fixtures"
    progs = {r["program"] for r in result.rows}
    assert {"turkish", "lifemiles", "aeroplan"} <= progs
    for r in result.rows:
        assert r["source_name"].startswith("email:")


def test_discovered_rows_resolve_through_aggregator() -> None:
    config = Config(offline=True, knowledge_dir=_KNOWLEDGE)
    result = run_discovery(config, fixture_dir=_FIXTURES)

    with tempfile.TemporaryDirectory() as tmp:
        disc = Path(tmp) / "discovered_charts.json"
        write_discovered(disc, result.rows, result.stale_programs)

        provider = AggregatorProvider(
            knowledge_dir=_KNOWLEDGE, offline=True, discovered_path=disc
        )
        route = Route("LAX", "IST", Cabin.BUSINESS)
        quotes = provider.fetch(Query(route=route, layer=Layer.CHARTS, programs=["turkish"]))

        # The discovered (email) row must resolve and carry the llm_extracted
        # flag. Other curated/scraped Turkish rows may also resolve for the same
        # route (e.g. the awardatlas fixture) — we specifically assert the
        # discovery-sourced quote is present.
        discovered = [
            q
            for q in quotes
            if q.program == "turkish"
            and "llm_extracted" in q.flags
            and q.provenance.source_name.startswith("email:")
        ]
        assert discovered, "discovered Turkish chart row did not resolve LAX->IST business"
        assert discovered[0].miles == 45000


def test_real_atf_chart_actually_parses() -> None:
    html = (_FIXTURES / "atf_aeroplan_chart.html").read_text(encoding="utf-8")
    rows = parse_chart_html_wide(html, program="aeroplan", updated_at="2026-06-29")

    assert rows, "parser found no rows in the real ATF chart page"
    # Picked the chart table, not the decoy nav/FAQ tables.
    assert all(r.program == "aeroplan" for r in rows)
    # Regions are now CANONICALIZED (§A): "Between North America and Atlantic"
    # -> ("north_america", "europe"), with the distance band attached. A known
    # real value: NA <-> Atlantic, 0-4,000 mi, business = 60,000.
    assert any(
        r.region_a == "north_america"
        and r.region_b == "europe"
        and r.cabin == "business"
        and r.miles == 60000
        and (r.distance_min, r.distance_max) == (0, 4000)
        for r in rows
    )
    # Economy in the same band is 35,000 (not the premium/first columns).
    assert any(r.cabin == "economy" and r.miles == 35000 for r in rows)
    # Em-dash "first" cells (Within North America) are skipped, never zero/guessed.
    assert all(r.miles > 0 for r in rows)


def test_offline_fetcher_is_network_free() -> None:
    f = Fetcher(base_dir=_KNOWLEDGE, offline=True)
    assert f.get("https://awardtravelfinder.com/award-charts/aeroplan") is None
    ok, status = f.head_ok("https://example.com/whatever")
    assert ok is False and status == 0  # unknown, NOT a permanent 404
    # file:// fixtures still resolve through the same path.
    res = f.get((_FIXTURES / "atf_aeroplan_chart.html").as_uri())
    assert res is not None and res.ok


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
