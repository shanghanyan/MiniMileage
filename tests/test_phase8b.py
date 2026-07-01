"""Phase 8b — region canonicalization, blog/transcript intakes, devaluation
fast-path, validator hardening, and URL rediscovery, as executable tests.

Runs standalone (`python tests/test_phase8b.py`) and is pytest-discoverable.
Everything is OFFLINE and deterministic — no network, no optional deps
(feedparser/trafilatura/youtube-transcript-api are all injected or stubbed):

  1. Region canonicalization (§A): zone labels, zone pairs, distance bands.
  2. The §A bug-proving fixture: real raw labels resolve to a route quote after
     the fix (and would NOT under the old exact-token compare); the unmapped
     zone is dropped + counted.
  3. Distance-band resolver (§A.4): an Aeroplan distance chart resolves a route.
  4. Blog intake (§B): a feed + post fixture -> grounded blog: rows; Cache dedupe.
  5. Transcript intake (§C): a channel feed + mocked captions -> yt: rows; dedupe.
  6. Devaluation fast-path (§D): a "Turkish devaluation" headline marks Turkish
     stale in the store; the emitted Turkish row is demoted (stale, lower conf).
  7. Validator hardening (§G): ok / unreachable / rotted / selector_miss states.
  8. URL rediscovery (§F): rot trigger + validate-before-adopt gate, search
     backend mocked, never hits the network.
  9. The extraction-accuracy eval (§I) passes (CI gate).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MILEAGE_OFFLINE", "1")
os.environ.pop("MILEAGE_REDIS_URL", None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.config import Config
from mileage.domain.charts import lookup_award_miles, great_circle_miles, _bands_match
from mileage.domain.models import Cabin, Layer, Route
from mileage.providers.aggregator import parse as agg_parse
from mileage.providers.aggregator.fetch import FetchResult, Fetcher, _headers
from mileage.providers.aggregator.parse import parse_chart_pdf
from mileage.providers.aggregator.ingest import (
    Creator,
    detect_devaluation,
    mark_devaluations_stale,
    run_blog_intake,
    run_transcript_intake,
)
from mileage.providers.aggregator.parse import parse_chart_html_wide
from mileage.providers.aggregator.provider import AggregatorProvider
from mileage.providers.aggregator.regions import (
    canonicalize_region,
    canonicalize_zone_pair,
    parse_distance_band,
)
from mileage.providers.aggregator.rediscover import run_rediscovery
from mileage.providers.aggregator.sources import Target, validate_targets
from mileage.providers.aggregator.ingest import discovered_path, write_discovered
from mileage.providers.base import Query
from mileage.store.inproc import InProcCache, ThreadLock
from mileage.store.sqlite_repo import SQLiteRepository
from mileage.verify.crosscheck import verify_award_quotes
from mileage.verify.freshness import is_stale

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "mileage" / "knowledge"
_FIXTURES = _KNOWLEDGE / "fixtures"


# --------------------------------------------------------------------------- #
# A FakeFetcher: maps URL -> {head: status, body: text}. No network, ever.
# --------------------------------------------------------------------------- #
class FakeFetcher:
    def __init__(self, pages: dict) -> None:
        self.pages = pages

    def head_ok(self, url: str):
        p = self.pages.get(url)
        if p is None:
            return False, 0
        h = p["head"]
        return (h != 0 and h < 400, h)

    def get(self, url: str):
        p = self.pages.get(url)
        if p is None or p["head"] == 0 or p["head"] >= 400:
            return None
        return FetchResult(
            url=url, text=p.get("body", ""), status=200, final_url=url, via="file"
        )


# --------------------------------------------------------------------------- #
# §A — region canonicalization
# --------------------------------------------------------------------------- #
def test_canonicalize_region() -> None:
    assert canonicalize_region("North America") == "north_america"
    assert canonicalize_region("within north america") == "north_america"
    assert canonicalize_region("Atlantic") == "europe"
    assert canonicalize_region("Europe") == "europe"
    assert canonicalize_region("Japan") == "north_asia"
    assert canonicalize_region("north_america") == "north_america"  # idempotent
    assert canonicalize_region("Antarctica") is None  # unmapped -> drop
    # A short token must NOT match as a substring inside another word.
    assert canonicalize_region("australia") == "oceania"


def test_canonicalize_zone_pair_and_distance() -> None:
    assert canonicalize_zone_pair("Between North America and Atlantic") == (
        "north_america", "europe",
    )
    assert canonicalize_zone_pair("Within North America") == (
        "north_america", "north_america",
    )
    assert canonicalize_zone_pair("Between North America and Narnia") is None
    assert parse_distance_band("0–4,000 mi") == (0, 4000)
    assert parse_distance_band("1,501-2,750 miles") == (1501, 2750)
    assert parse_distance_band("6,001+ mi") == (6001, 999999)
    assert parse_distance_band("no numbers here") is None


def test_zone_pair_bug_then_fix() -> None:
    """The real-label ATF table resolves to a route quote AFTER the fix; the
    unmapped zone is dropped + counted. Under the OLD exact-token compare the
    raw 'North America'/'Europe' labels would never match (the bug)."""
    html = (_FIXTURES / "atf_lifemiles_chart.html").read_text(encoding="utf-8")

    # Prove the bug: raw labels do NOT equal the canonical route tokens.
    assert not _bands_match(["North America", "Europe"], "north_america", "europe")

    stats: dict = {}
    rows = parse_chart_html_wide(html, program="lifemiles", stats=stats)
    assert rows, "no rows parsed"
    # Canonicalized geography.
    assert all(r.region_a == "north_america" for r in rows)
    # The unmapped 'Antarctica' row was dropped AND counted, not mismatched.
    assert stats["dropped"] >= 1
    assert all("antarctica" not in (r.region_a + r.region_b) for r in rows)

    # After the fix, the mappable rows resolve to a route quote.
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    chart = agg._build_charts(rows).get("lifemiles")
    hit = lookup_award_miles(
        "lifemiles", chart, Route("LAX", "IST", Cabin.BUSINESS),
        agg._region_map, airport_coords=agg._airport_coords,
    )
    assert hit is not None and hit.miles == 63000


def test_distance_band_resolver() -> None:
    html = (_FIXTURES / "atf_aeroplan_chart.html").read_text(encoding="utf-8")
    rows = parse_chart_html_wide(html, program="aeroplan")
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    chart = agg._build_charts(rows).get("aeroplan")
    # LAX->JFK (~2,470 mi) falls in the Within-North-America 1,501-2,750 band.
    hit = lookup_award_miles(
        "aeroplan", chart, Route("LAX", "JFK", Cabin.ECONOMY),
        agg._region_map, airport_coords=agg._airport_coords,
    )
    assert hit is not None and hit.miles == 12500
    assert 2000 < great_circle_miles(
        agg._airport_coords["LAX"], agg._airport_coords["JFK"]
    ) < 3000


# --------------------------------------------------------------------------- #
# §B — blog intake
# --------------------------------------------------------------------------- #
def test_blog_intake_and_dedupe() -> None:
    feed_url = "https://frequentmiler.example/feed"
    post_url = "https://frequentmiler.example/posts/sweet-spots"
    fake = FakeFetcher({
        feed_url: {"head": 200, "body": (_FIXTURES / "creator_feed.xml").read_text()},
        post_url: {"head": 200, "body": (_FIXTURES / "creator_post.html").read_text()},
    })
    creators = [Creator(name="frequent_miler", blog_rss=feed_url, trust=0.55)]
    config = Config(offline=True, knowledge_dir=_KNOWLEDGE)
    cache = InProcCache()

    r1 = run_blog_intake(config, fetcher=fake, cache=cache, lock=ThreadLock(), creators=creators)
    triples = {(x["program"], x["region_a"], x["region_b"], x["cabin"], x["miles"]) for x in r1.rows}
    assert ("turkish", "north_america", "europe", "business", 45000) in triples
    assert ("lifemiles", "north_america", "europe", "economy", 30000) in triples
    assert all(x["source_name"] == "blog:frequent_miler" for x in r1.rows)
    assert r1.new == 1

    # Second run: the post URL is cached -> no re-extraction.
    r2 = run_blog_intake(config, fetcher=fake, cache=cache, creators=creators)
    assert r2.new == 0 and r2.rows == []


# --------------------------------------------------------------------------- #
# §C — transcript intake
# --------------------------------------------------------------------------- #
def test_transcript_intake_and_dedupe() -> None:
    cid = "UCU8_ZlRsvvGoinB8RnKC7qA"
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
    fake = FakeFetcher({
        feed_url: {"head": 200, "body": (_FIXTURES / "yt_feed.xml").read_text()},
    })
    captions = {
        "VID12345678": "Avianca LifeMiles economy from North America to Europe is 30,000 miles."
    }
    creators = [Creator(name="lukes", channel_id=cid, trust=0.45)]
    config = Config(offline=True, knowledge_dir=_KNOWLEDGE)
    cache = InProcCache()

    r1 = run_transcript_intake(
        config, fetcher=fake, cache=cache,
        caption_fetcher=lambda vid: captions.get(vid), creators=creators,
    )
    triples = {(x["program"], x["region_a"], x["region_b"], x["cabin"], x["miles"]) for x in r1.rows}
    assert ("lifemiles", "north_america", "europe", "economy", 30000) in triples
    assert all(x["source_name"] == "yt:lukes" for x in r1.rows)
    assert r1.new == 1

    r2 = run_transcript_intake(
        config, fetcher=fake, cache=cache,
        caption_fetcher=lambda vid: captions.get(vid), creators=creators,
    )
    assert r2.new == 0 and r2.rows == []


# --------------------------------------------------------------------------- #
# §D — devaluation fast-path
# --------------------------------------------------------------------------- #
def test_devaluation_faspath_demotes() -> None:
    assert detect_devaluation("Turkish Miles&Smiles devaluation incoming") == "turkish"

    with tempfile.TemporaryDirectory() as tmp:
        repo = SQLiteRepository(str(Path(tmp) / "deval.db"))
        marked = mark_devaluations_stale(repo, {"turkish"}, reason="test")
        assert marked == {"turkish"}
        assert "turkish" in repo.stale_programs()

        # A discovered Turkish chart row, resolved with the store consulted.
        disc = Path(tmp) / "discovered_charts.json"
        write_discovered(disc, [{
            "program": "turkish", "region_a": "north_america", "region_b": "europe",
            "cabin": "business", "miles": 45000, "roundtrip": False,
            "source_name": "email:hello@dailydrop.com", "source_url": None,
            "source_updated_at": "2026-06-29T09:00:00+00:00", "trust": 0.3,
        }], set())

        provider = AggregatorProvider(
            knowledge_dir=_KNOWLEDGE, offline=True,
            discovered_path=disc, health_repo=repo,
        )
        quotes = provider.fetch(
            Query(route=Route("LAX", "IST", Cabin.BUSINESS), layer=Layer.CHARTS,
                  programs=["turkish"])
        )
        deval = [q for q in quotes if "llm_extracted" in q.flags]
        assert deval, "discovered turkish did not resolve"
        q = deval[0]
        assert "stale" in q.flags, "devaluation did not flag the row stale"
        # source_updated_at was capped before the freshness cutoff so verify
        # independently demotes it.
        assert is_stale(q.provenance)

        verified = verify_award_quotes([q])
        assert verified and "stale" in verified[0].flags
        repo.close()


# --------------------------------------------------------------------------- #
# §G — validator hardening
# --------------------------------------------------------------------------- #
def test_validate_urls_states() -> None:
    ok_body = (_FIXTURES / "atf_lifemiles_chart.html").read_text()
    empty_body = "<html><body><p>Our chart moved.</p></body></html>"
    pages = {
        "http://ex/ok": {"head": 200, "body": ok_body},
        "http://ex/selmiss": {"head": 200, "body": empty_body},
        "http://ex/unreach": {"head": 0},
        "http://ex/rotted": {"head": 404},
    }
    fake = FakeFetcher(pages)

    def mk(name, url):
        return Target(name=name, url=url, format="html_table_wide",
                      provides="chart", program="lifemiles", trust=0.5)

    targets = [
        mk("ok", "http://ex/ok"),
        mk("selmiss", "http://ex/selmiss"),
        mk("unreach", "http://ex/unreach"),
        mk("rotted", "http://ex/rotted"),
    ]

    def content_check(t, text):
        return len(parse_chart_html_wide(text, program=t.program or t.name))

    validate_targets(targets, fake, force=True, deep=True, content_check=content_check)
    labels = {t.name: t.status_label() for t in targets}
    assert labels == {
        "ok": "ok",
        "selmiss": "selector_miss",
        "unreach": "unreachable",
        "rotted": "rotted",
    }, labels


# --------------------------------------------------------------------------- #
# §F — URL rediscovery: rot trigger + validate-before-adopt
# --------------------------------------------------------------------------- #
class _MockSearch:
    def __init__(self, urls):
        self.urls = urls

    def propose_urls(self, query, *, limit=5):
        return self.urls


def test_rot_trigger_and_validate_before_adopt() -> None:
    good_url = "http://ex/good-lifemiles"
    bad_url = "http://ex/bad-empty"
    fake = FakeFetcher({
        good_url: {"head": 200, "body": (_FIXTURES / "atf_lifemiles_chart.html").read_text()},
        bad_url: {"head": 200, "body": "<html><body>no table</body></html>"},
    })

    provider = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    provider.fetcher = fake
    rotted = Target(
        name="atf-lifemiles-chart", url="http://dead/lifemiles",
        format="html_table_wide", provides="chart", program="lifemiles",
        trust=0.8, consecutive_failures=3,
    )
    provider.targets = [rotted]
    assert rotted.is_rotted(), "consecutive_failures>=3 should be rotted"

    # Disabled -> no-op (no key / flag off).
    off = Config(offline=True, knowledge_dir=_KNOWLEDGE, url_rediscovery_enabled=False)
    rep_off = run_rediscovery(provider, off, search=_MockSearch([good_url]))
    assert rep_off.ran is False

    # Enabled: the bad candidate (parses 0 rows) is rejected, the good one
    # (parses >=1 row) is adopted. write=False so sources.yaml is untouched.
    on = Config(offline=True, knowledge_dir=_KNOWLEDGE, url_rediscovery_enabled=True)
    rep = run_rediscovery(
        provider, on, search=_MockSearch([bad_url, good_url]),
        cache=InProcCache(), write=False,
    )
    assert rep.ran is True
    assert bad_url in rep.rejected
    assert [a.new_url for a in rep.adoptions] == [good_url]


# --------------------------------------------------------------------------- #
# §I — extraction-accuracy eval passes (CI gate)
# --------------------------------------------------------------------------- #
def test_extraction_eval_gate() -> None:
    from mileage import evals

    report = evals.run_extraction_eval()
    assert report.ok, evals.render_extraction_report(report)
    assert report.dropped_regions >= 1


# --------------------------------------------------------------------------- #
# Live-scraping hardening: proxy hygiene, UA rotation, PDF extraction
# --------------------------------------------------------------------------- #
def test_fetcher_ignores_all_proxy_by_default() -> None:
    """A stray ALL_PROXY must NOT be trusted by default (else every live fetch
    routes through it and crashes before reaching the target). Opt back in
    explicitly via the constructor or MILEAGE_TRUST_ENV."""
    assert Fetcher().trust_env is False
    assert Fetcher(trust_env=True).trust_env is True
    os.environ["MILEAGE_TRUST_ENV"] = "1"
    try:
        assert Fetcher().trust_env is True
    finally:
        os.environ.pop("MILEAGE_TRUST_ENV", None)


def test_user_agent_rotates_and_is_not_self_identifying() -> None:
    seen = {_headers()["User-Agent"] for _ in range(50)}
    assert len(seen) > 1, "User-Agent should rotate"
    assert all("MileageAggregator" not in ua for ua in seen)
    assert all("Mozilla/5.0" in ua for ua in seen)


class _FakePdfPage:
    def __init__(self, tables):
        self._tables = tables

    def extract_tables(self):
        return self._tables


class _FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _install_fake_pdfplumber(tables):
    """Patch the optional pdfplumber dep with a deterministic stand-in so the
    PDF path is exercised offline, regardless of whether the binary is present.
    Returns a restore() callable."""
    class _FakePlumber:
        @staticmethod
        def open(_buf):
            return _FakePdf([_FakePdfPage(tables)])

    orig_mod, orig_flag = agg_parse.pdfplumber, agg_parse._HAS_PDFPLUMBER
    agg_parse.pdfplumber = _FakePlumber
    agg_parse._HAS_PDFPLUMBER = True

    def restore():
        agg_parse.pdfplumber = orig_mod
        agg_parse._HAS_PDFPLUMBER = orig_flag

    return restore


def test_pdf_chart_parses_distance_bands() -> None:
    tables = [[
        ["From", "Distance", "Economy", "Business", "First"],
        ["Within North America", "0-2,750 mi", "12,500", "25,000", "—"],
        ["Between North America and Atlantic", "0-4,000 mi", "30,000", "60,000", "85,000"],
        ["Between Narnia and Mordor", "0-4,000 mi", "1", "2", "3"],  # unmappable
    ]]
    restore = _install_fake_pdfplumber(tables)
    try:
        stats: dict = {}
        rows = parse_chart_pdf(b"%PDF-1.4 fake", program="aeroplan", stats=stats)
    finally:
        restore()
    # The unmappable Narnia/Mordor row is dropped AND counted, never guessed.
    assert stats["dropped"] == 1
    miles = {(r.region_a, r.region_b, r.cabin): r.miles for r in rows}
    assert miles[("north_america", "north_america", "economy")] == 12500
    assert miles[("north_america", "europe", "business")] == 60000
    # The '—' first-class cell on the NA-NA row is a selector miss -> no row.
    assert ("north_america", "north_america", "first") not in miles
    assert all(r.distance_min is not None for r in rows)


def test_pdf_chart_degrades_without_pdfplumber() -> None:
    orig = agg_parse._HAS_PDFPLUMBER
    agg_parse._HAS_PDFPLUMBER = False
    try:
        assert parse_chart_pdf(b"anything", program="aeroplan") == []
    finally:
        agg_parse._HAS_PDFPLUMBER = orig


def test_pdf_target_flows_through_provider() -> None:
    """A `format: pdf` chart target produces route quotes via the provider,
    using the raw bytes the Fetcher now carries on FetchResult."""
    tables = [[
        ["From", "To", "Economy", "Business"],
        ["North America", "Europe", "30,000", "63,000"],
    ]]
    restore = _install_fake_pdfplumber(tables)
    try:
        agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
        target = Target(
            name="aeroplan-official-pdf", url="https://example/chart.pdf",
            format="pdf", provides="chart", program="lifemiles", trust=0.95,
        )
        result = FetchResult(
            url=target.url, text="(binary)", status=200,
            final_url=target.url, via="httpx", raw=b"%PDF-1.4 fake",
        )
        rows = agg._parse_chart_rows(target, result.text, raw=result.raw)
        assert rows, "pdf target parsed no rows"
        chart = agg._build_charts(rows).get("lifemiles")
        hit = lookup_award_miles(
            "lifemiles", chart, Route("LAX", "IST", Cabin.BUSINESS),
            agg._region_map, airport_coords=agg._airport_coords,
        )
        assert hit is not None and hit.miles == 63000
    finally:
        restore()


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
