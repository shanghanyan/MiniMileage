"""Phase 5 guarantees — observability + evals (§10, §12 Phase 5).

Runs standalone (`python tests/test_phase5.py`) and is pytest-discoverable.

  1. Tracing is optional + a pure no-op: with no deps/creds the span helpers
     are safe and the pipeline is unchanged.
  2. The golden route set passes as a CI honesty gate (Demo A + Demo B + extras).
  3. Anti-hallucination: an unsourced datum never survives verification (§2.1).
  4. Anti-hallucination: an out-of-bounds value never survives (§2.1).
  5. A winner built on a stale datum is `tentative_best`, never `best` (§7).
  6. The poisoned-chart demo: verification rejects stale/garbage; build stays green.
  7. The eval gate actually catches a planted hallucination (it would fail CI).
  8. Capital One -> United is structurally impossible: scraped United award space
     never becomes a transfer option (§0 load-bearing domain fact).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage import evals, obs
from mileage.domain.models import (
    AwardQuote,
    Cabin,
    PathOption,
    Provenance,
    Route,
    Verdict,
    VerdictLabel,
)
from mileage.domain.models import TransferRatio
from mileage.domain.verdict import conclude_winner
from mileage.graph.build import build_graph
from mileage.graph.optimize import rank_paths
from mileage.verify.crosscheck import VerifiedAward, verify_award_quotes

ROUTE_A = Route("LAX", "JFK", Cabin.ECONOMY)
ROUTE_B = Route("LAX", "IST", Cabin.BUSINESS)


# --------------------------------------------------------------------------- #
# 1 — tracing is optional and a no-op without deps/creds
# --------------------------------------------------------------------------- #
def test_tracing_optional_noop() -> None:
    # No ARIZE creds + no console flag -> tracing stays inactive, never raises.
    assert obs.setup_tracing(project_name="mileage-test") is False
    with obs.span("noop", obs.KIND_CHAIN, input_value="x") as s:
        # Helpers must be safe even when the span is a no-op (None).
        obs.set_output(s, "y")
        obs.set_attr(s, "k", "v")
        assert s is None
    obs.shutdown_tracing()  # idempotent, safe


# --------------------------------------------------------------------------- #
# 2 — the golden route set passes as a CI honesty gate
# --------------------------------------------------------------------------- #
def test_golden_set_passes() -> None:
    report = evals.run_golden()
    failures = [
        f"{c.name}: " + "; ".join(ck.detail for ck in c.checks if not ck.ok)
        for c in report.cases
        if not c.passed
    ]
    assert report.ok, "golden eval gate failed:\n" + "\n".join(failures)
    assert len(report.cases) >= 5


# --------------------------------------------------------------------------- #
# 3 + 4 — anti-hallucination at the verification boundary (§2.1)
# --------------------------------------------------------------------------- #
def test_unsourced_datum_dropped() -> None:
    good = AwardQuote(
        program="turkish", route=ROUTE_B, miles=45000, seats_available=2,
        provenance=Provenance(source_name="StarNet", trust=0.6), confidence=0.6,
        flags=["live_award_space"],
    )
    unsourced = AwardQuote(
        program="lifemiles", route=ROUTE_B, miles=63000, seats_available=1,
        provenance=Provenance(source_name="unknown"), confidence=0.99,
        flags=["live_award_space"],
    )
    verified = verify_award_quotes([good, unsourced])
    programs = {v.program for v in verified}
    assert "turkish" in programs, "a sourced, in-bounds datum must survive"
    assert "lifemiles" not in programs, "an unsourced datum must never survive"


def test_out_of_bounds_dropped() -> None:
    garbage = AwardQuote(
        program="turkish", route=ROUTE_B, miles=5, seats_available=1,
        provenance=Provenance(source_name="blog", trust=0.99), confidence=0.99,
        flags=["live_award_space"],
    )
    verified = verify_award_quotes([garbage])
    assert verified == [], "5 miles for a business seat is implausible -> dropped"


# --------------------------------------------------------------------------- #
# 5 — a stale winner is tentative_best, never best (§7)
# --------------------------------------------------------------------------- #
def test_stale_winner_is_tentative_not_best() -> None:
    portal = PathOption(
        label="Capital One portal", kind="portal", cpp=1.25,
        source_points=33600, cash_cents=42000, affordable=True,
    )
    stale_winner = PathOption(
        label="Capital One -> aeroplan", kind="transfer", cpp=7.0,
        source_points=60000, cash_cents=420000, program="aeroplan",
        affordable=True, flags=["stale"],
    )
    verdict = conclude_winner(ROUTE_B, portal, [stale_winner])
    assert verdict.label == VerdictLabel.TENTATIVE_BEST, (
        "a winner carrying `stale` must be demoted from best to tentative_best"
    )


# --------------------------------------------------------------------------- #
# 6 — the poisoned-chart demo rejects stale/garbage, keeps the clean control
# --------------------------------------------------------------------------- #
def test_poison_chart_rejected() -> None:
    result = evals.run_poison_check()
    detail = "\n".join(f"  ({'ok' if c.ok else 'XX'}) {c.name}: {c.detail}"
                       for c in result.checks)
    assert result.ok, "verification let poison through:\n" + detail


# --------------------------------------------------------------------------- #
# 7 — the eval gate actually catches a planted hallucination (would fail CI)
# --------------------------------------------------------------------------- #
def test_eval_catches_planted_hallucination() -> None:
    # A fabricated run result whose verified award is both unsourced AND
    # out-of-bounds. The honesty gate must flag it (i.e. CI would go red).
    bad_award = VerifiedAward(
        program="turkish", route=ROUTE_B, miles=5, confidence=0.99,
        seats_available=1, flags=[],
        provenance=[Provenance(source_name="unknown")],
    )
    portal = PathOption(
        label="Capital One portal", kind="portal", cpp=1.25,
        source_points=33600, cash_cents=42000, affordable=True,
    )
    verdict = Verdict(
        label=VerdictLabel.COMPARABLE, route=ROUTE_B, portal=portal,
        best_transfer=None, options=[portal], rationale="(fabricated)",
    )
    result = {"verdict": verdict, "awards": [bad_award]}
    fails = evals.anti_hallucination_failures(result)
    assert fails, "the gate must catch unsourced + out-of-bounds data"
    assert any("unsourced" in f for f in fails)
    assert any("out-of-bounds" in f for f in fails)


# --------------------------------------------------------------------------- #
# 8 — verified United award space still never enters the graph (§0)
# --------------------------------------------------------------------------- #
def test_united_never_enters_graph() -> None:
    # United award space is real, sourced, and in-bounds -> it verifies fine.
    united = AwardQuote(
        program="united", route=ROUTE_A, miles=11000, seats_available=7,
        provenance=Provenance(source_name="StarNet", trust=0.6), confidence=0.6,
        flags=["live_award_space"],
    )
    verified = verify_award_quotes([united])
    assert any(v.program == "united" for v in verified), (
        "United award data is verifiable (the exclusion is structural, not data)"
    )

    # But Capital One transfers only to partners — there is NO C1 -> United edge.
    ratios = [
        TransferRatio(
            from_currency="capital_one", to_program="turkish", ratio=1.0,
            provenance=Provenance(source_name="Capital One", trust=1.0),
        )
    ]
    graph = build_graph("capital_one", ratios, verified)
    options = rank_paths(
        graph, "capital_one", 15800, portal_cpp=1.25, balance=50000
    )
    transfer_programs = {o.program for o in options if o.kind == "transfer"}
    assert "united" not in transfer_programs, (
        "United must never enter the graph (no Capital One -> United transfer)"
    )


if __name__ == "__main__":
    test_tracing_optional_noop()
    test_golden_set_passes()
    test_unsourced_datum_dropped()
    test_out_of_bounds_dropped()
    test_stale_winner_is_tentative_not_best()
    test_poison_chart_rejected()
    test_eval_catches_planted_hallucination()
    test_united_never_enters_graph()
    print(
        "OK: tracing no-op, golden eval gate, anti-hallucination (unsourced + "
        "out-of-bounds), stale->tentative, poisoned-chart rejected, gate catches "
        "a planted hallucination, and United structurally excluded."
    )
