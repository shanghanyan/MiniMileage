"""Phase 1 guarantees as executable tests — the Aggregator (Engine A).

Runs standalone (`python tests/test_phase1.py`) and is pytest-discoverable.
Asserts the properties Phase 1 adds without breaking Phase 0's honesty:

  1. The aggregator turns real scraped fixtures into normalized AwardQuotes
     (selector hits -> data; misses -> nothing).
  2. Live award space CLEARS the chart-only `no_live_space` caveat (§2.5).
  3. Two INDEPENDENT live sources cross-check (single_source clears, miles is a
     trust-weighted median), per §7.
  4. The structural United guarantee survives REAL scraping: a planted United
     award is parsed, yet still cannot enter the graph (no Cap One -> United).
  5. `--validate-urls` flags a dead target (last_404), the URL-rot health check.
  6. The fetch politeness policy backs off on 429 and recovers on 200.
"""

from __future__ import annotations

import os as _os; _os.environ.setdefault("MILEAGE_OFFLINE", "1")  # hermetic standalone runs

import contextlib
import http.server
import json
import socketserver
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.config import Config, build_registry, partner_programs
from mileage.domain.models import AwardQuote, Cabin, Layer, Provenance, Route, TransferRatio, User
from mileage.graph.build import build_graph
from mileage.graph.optimize import rank_paths
from mileage.providers.aggregator import AggregatorProvider, Fetcher, PolitenessPolicy
from mileage.providers.aggregator.parse import normalize_one_way, parse_award_json
from mileage.providers.aggregator.sources import Target, validate_targets
from mileage.providers.base import Query
from mileage.verify.crosscheck import verify_award_quotes
from mileage.verify.trust import spread, trust_weighted_median
from mileage.cli import run_quote

_CONFIG = Config()
_KNOWLEDGE = _CONFIG.knowledge_dir


def _aggregator() -> AggregatorProvider:
    return AggregatorProvider(
        sources_path=_CONFIG.sources_path, knowledge_dir=_KNOWLEDGE
    )


def test_aggregator_parses_live_award_space() -> None:
    """Scraped fixtures -> live AwardQuotes with seats and no no_live_space."""
    agg = _aggregator()
    route = Route("LAX", "IST", Cabin.BUSINESS)
    quotes = [q for q in agg.fetch(Query(route, Layer.AWARD)) if isinstance(q, AwardQuote)]
    turkish = [q for q in quotes if q.program == "turkish"]
    assert turkish, "expected scraped Turkish LAX-IST business award space"
    for q in turkish:
        assert q.seats_available and q.seats_available > 0
        assert "live_award_space" in q.flags
        assert "no_live_space" not in q.flags


def test_aggregator_charts_are_chart_only() -> None:
    """A CHARTS query yields chart-derived quotes flagged no_live_space."""
    agg = _aggregator()
    route = Route("LAX", "IST", Cabin.BUSINESS)
    charts = [q for q in agg.fetch(Query(route, Layer.CHARTS)) if isinstance(q, AwardQuote)]
    assert charts, "expected scraped chart quotes"
    assert all("no_live_space" in q.flags for q in charts)
    assert all(q.seats_available is None for q in charts)


def test_live_precedence_clears_no_live_space() -> None:
    """A live quote overrides a chart-only quote for the same program (§2.5)."""
    route = Route("LAX", "IST", Cabin.BUSINESS)
    chart = AwardQuote(
        program="turkish", route=route, miles=45000, seats_available=None,
        provenance=Provenance(source_name="curated", trust=0.7),
        confidence=0.7, flags=["no_live_space"],
    )
    live = AwardQuote(
        program="turkish", route=route, miles=45000, seats_available=2,
        provenance=Provenance(source_name="starnet", trust=0.7),
        confidence=0.7, flags=["live_award_space"],
    )
    verified = verify_award_quotes([chart, live])
    assert len(verified) == 1
    v = verified[0]
    assert v.seats_available == 2
    assert "no_live_space" not in v.flags


def test_independent_sources_cross_check() -> None:
    """Two independent live sources -> no single_source; trust-weighted median."""
    agg = _aggregator()
    route = Route("LAX", "IST", Cabin.BUSINESS)
    quotes = [q for q in agg.fetch(Query(route, Layer.AWARD)) if isinstance(q, AwardQuote)]
    verified = verify_award_quotes([q for q in quotes if q.program == "turkish"])
    assert len(verified) == 1
    v = verified[0]
    # json (45000, trust .70) + rss (46000, trust .45) -> two sources.
    assert "single_source" not in v.flags, "two independent sources should cross-check"
    assert 45000 <= v.miles <= 46000


def test_scraped_united_still_unreachable() -> None:
    """United is scraped (selector hits) yet cannot enter the graph (§0)."""
    agg = _aggregator()
    route = Route("LAX", "JFK", Cabin.ECONOMY)
    # programs empty => provider decides => returns everything it scraped.
    quotes = [q for q in agg.fetch(Query(route, Layer.AWARD)) if isinstance(q, AwardQuote)]
    assert any(q.program == "united" for q in quotes), (
        "fixture must actually contain a United award to make this test meaningful"
    )

    verified = verify_award_quotes(quotes)
    ratios = [
        TransferRatio(from_currency="capital_one", to_program=p, ratio=1.0)
        for p in partner_programs(_CONFIG)
    ]
    assert "united" not in {r.to_program for r in ratios}
    graph = build_graph("capital_one", ratios, verified)
    assert "united" not in graph.nodes
    options = rank_paths(graph, "capital_one", 15800, portal_cpp=1.25, balance=20000)
    assert "united" not in {o.program for o in options if o.kind == "transfer"}


def test_validate_urls_flags_dead_target() -> None:
    """A missing target is marked last_404 by the health check."""
    dead = Target(
        name="dead", url="file:///no/such/fixture.json",
        format="json", provides="award", trust=0.5,
    )
    validate_targets([dead], Fetcher(base_dir=_KNOWLEDGE))
    assert dead.last_404 is True
    assert not dead.healthy()


def test_politeness_backs_off_and_recovers() -> None:
    """429 widens the per-domain delay; 200 narrows it (adaptive throttle)."""
    pol = PolitenessPolicy(base_delay=1.0, sleep=lambda *_: None)
    start = pol.delay_for("example.com")
    pol.on_response("example.com", 429)
    after_429 = pol.delay_for("example.com")
    assert after_429 > start
    pol.on_response("example.com", 200)
    assert pol.delay_for("example.com") < after_429


# --------------------------------------------------------------------------- #
# Network-resilience proof: a REAL loopback HTTP server emits real 403/429 so
# the fetch chain (httpx -> ... -> Wayback) is exercised, not just described.
# --------------------------------------------------------------------------- #
class _ScriptedHandler(http.server.BaseHTTPRequestHandler):
    """Server behavior is driven by attributes set on the server instance."""

    def _send(self, code: int, body: bytes = b"ok") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        self._send(200, b"")

    def do_GET(self):  # noqa: N802
        srv = self.server
        srv.hits.append((self.path.split("?")[0]))  # type: ignore[attr-defined]
        path = self.path
        if path.startswith("/wb"):  # stand-in Wayback availability API
            snap = f"http://127.0.0.1:{srv.server_address[1]}/snapshot"
            payload = {"archived_snapshots": {"closest": {"available": True, "url": snap}}}
            self._send(200, json.dumps(payload).encode())
            return
        if path.startswith("/snapshot"):
            self._send(200, b"<archived>award chart</archived>")
            return
        if path.startswith("/blocked"):
            self._send(403, b"forbidden")
            return
        if path.startswith("/flaky"):
            n = srv.flaky_seen  # type: ignore[attr-defined]
            srv.flaky_seen += 1  # type: ignore[attr-defined]
            if n < srv.flaky_fail_times:  # type: ignore[attr-defined]
                self._send(429, b"slow down")
            else:
                self._send(200, b"finally ok")
            return
        self._send(404, b"nope")

    def log_message(self, *a):  # silence
        return


@contextlib.contextmanager
def _server(*, flaky_fail_times: int = 0):
    srv = socketserver.TCPServer(("127.0.0.1", 0), _ScriptedHandler)
    srv.hits = []  # type: ignore[attr-defined]
    srv.flaky_seen = 0  # type: ignore[attr-defined]
    srv.flaky_fail_times = flaky_fail_times  # type: ignore[attr-defined]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv, f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_fetch_429_backoff_then_success_real_http() -> None:
    """A REAL 429 (twice) widens the per-domain delay, then a 200 succeeds."""
    delays: list[float] = []
    pol = PolitenessPolicy(base_delay=0.05, jitter=0.0, sleep=lambda *_: None)
    with _server(flaky_fail_times=2) as (srv, base):
        fetcher = Fetcher(
            politeness=pol, max_429_retries=3, use_wayback=False, offline=False
        )
        domain = base.split("//", 1)[1]
        before = pol.delay_for(domain)
        result = fetcher.get(f"{base}/flaky")
        after = pol.delay_for(domain)
    n_429 = sum(1 for h in srv.hits if h == "/flaky") - 1  # last one was the 200
    print(
        f"    [429] server saw {srv.flaky_seen} hits "
        f"({n_429}x 429 then 200); delay {before:.3f}s -> {after:.3f}s; "
        f"final via={result.via if result else None} ok={bool(result and result.ok)}"
    )
    assert result is not None and result.ok
    assert result.via == "httpx"
    assert srv.flaky_seen == 3, "expected two 429s then one 200"
    assert after > before, "429s must widen the adaptive delay"


def test_fetch_403_falls_back_to_wayback_real_http() -> None:
    """A REAL 403 exhausts the live path and recovers via a Wayback snapshot."""
    with _server() as (srv, base):
        fetcher = Fetcher(
            politeness=PolitenessPolicy(base_delay=0.0, jitter=0.0, sleep=lambda *_: None),
            use_wayback=True,
            wayback_api=f"{base}/wb?url=",
            offline=False,
        )
        result = fetcher.get(f"{base}/blocked")
    paths = [h for h in srv.hits]
    print(
        f"    [403->wayback] server path sequence: {paths}; "
        f"final via={result.via if result else None} "
        f"flags={result.flags if result else None}"
    )
    assert result is not None and result.ok
    assert result.via == "wayback", "must recover through the Wayback snapshot"
    assert "from_wayback" in result.flags
    assert "/blocked" in paths and "/snapshot" in paths
    assert any(p.startswith("/wb") for p in paths)


def test_two_source_median_inputs_and_result() -> None:
    """Expose the actual cross-check inputs AND the trust-weighted median math."""
    agg = _aggregator()
    route = Route("LAX", "IST", Cabin.BUSINESS)
    turkish = [
        q
        for q in agg.fetch(Query(route, Layer.AWARD))
        if isinstance(q, AwardQuote) and q.program == "turkish"
    ]
    inputs = sorted(
        (q.provenance.source_name, q.miles, q.provenance.trust) for q in turkish
    )
    # The two genuinely independent live sources feeding the cross-check.
    assert {name for name, _, _ in inputs} == {"starnet-award-space", "milefeed-rss"}
    miles_by_source = {name: miles for name, miles, _ in inputs}
    assert miles_by_source["starnet-award-space"] == 45000
    assert miles_by_source["milefeed-rss"] == 46000

    vals = [float(q.miles) for q in turkish]
    weights = [q.provenance.trust for q in turkish]
    median = trust_weighted_median(vals, weights)
    sp = spread(vals)
    verified = verify_award_quotes(turkish)[0]
    print(
        f"    [median] inputs={inputs}; trust_weighted_median={median:.0f}; "
        f"spread={sp * 100:.1f}%; verified.miles={verified.miles}; "
        f"flags={verified.flags}"
    )
    assert median == 45000, "0.70-trust 45k outweighs 0.45-trust 46k"
    assert verified.miles == 45000
    assert sp <= 0.10, "within 10% -> no sources_disagree flag"
    assert "single_source" not in verified.flags
    assert not any(f.startswith("sources_disagree") for f in verified.flags)


def test_roundtrip_award_normalized_to_one_way() -> None:
    """A round-trip award (e.g. ANA) is halved to one-way and flagged (§6)."""
    raw = parse_award_json(json.dumps([
        {"program": "ana", "origin": "SFO", "dest": "NRT", "cabin": "business",
         "miles": 90000, "seats": 2, "roundtrip": True, "updated_at": "2026-06-24"}
    ]))
    assert raw and raw[0].roundtrip is True and raw[0].miles == 90000
    one_way, flags = normalize_one_way(raw[0].miles, raw[0].roundtrip)
    print(f"    [rt->ow] 90,000 RT -> {one_way:,} OW; flags={flags}")
    assert one_way == 45000
    assert "rt_to_ow_normalized" in flags


def test_demo_b_verdict_best_with_live_space() -> None:
    """End-to-end: Demo B is `best` and its winner carries verified live space."""
    registry = build_registry(_CONFIG)
    route = Route("LAX", "IST", Cabin.BUSINESS)
    user = User(balances={"capital_one": 90000}, card="venture_x")
    result = run_quote(route, user, "capital_one", registry=registry, config=_CONFIG)
    verdict = result["verdict"]
    assert verdict.label.value in ("best", "tentative_best")
    winner = verdict.best_transfer
    assert winner is not None
    assert "no_live_space" not in winner.flags, "live space must clear the caveat"
    live = [a for a in result["awards"] if a.seats_available]
    assert any(a.program == "turkish" for a in live)


if __name__ == "__main__":
    print("Behavioral guarantees:")
    test_aggregator_parses_live_award_space()
    test_aggregator_charts_are_chart_only()
    test_live_precedence_clears_no_live_space()
    test_independent_sources_cross_check()
    test_scraped_united_still_unreachable()
    test_validate_urls_flags_dead_target()
    test_politeness_backs_off_and_recovers()
    test_demo_b_verdict_best_with_live_space()

    print("\nInternals proven against real HTTP / explicit math:")
    test_fetch_429_backoff_then_success_real_http()
    test_fetch_403_falls_back_to_wayback_real_http()
    test_two_source_median_inputs_and_result()
    test_roundtrip_award_normalized_to_one_way()

    print(
        "\nOK: behavior + internals verified — real 429 backoff, real "
        "403->Wayback fallback, exposed two-source median, rt->ow normalization, "
        "plus live precedence / cross-check / United-unreachable / URL-rot."
    )
