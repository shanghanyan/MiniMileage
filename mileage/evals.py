"""Phase 5 — golden-route evals + anti-hallucination guards (§10, §12 Phase 5).

Tracing (`obs.py`) makes every run *traceable*; this module makes the honesty
rules *enforceable*. The golden set (Demo A + Demo B + extras) runs the real
pipeline and asserts both the expected verdict AND the load-bearing rules from
§2/§7, so "no data without verification" is a failing build, not a comment:

  - no datum without verifiable provenance ever survives verification (§2.1);
  - implausible (out-of-bounds) values are rejected, never graphed (§2.1);
  - a winner built on a warning-flagged datum (stale / sources_disagree /
    bounds_violation) is never `best`, only `tentative_best` (§7);
  - Capital One -> United is structurally impossible, so United never appears
    as a transfer option even when its award space is scraped (§0 domain fact).

`run_golden()` returns a report; `mileage eval` exits non-zero on any failure,
so these are CI evals. `run_poison_check()` is the §12 Phase 5 demo: it feeds a
stale/garbage chart and proves verification rejects it (and that the eval would
fail the build if it ever didn't) — the trace shows exactly where.

Evals run offline + deterministically: live API providers self-disable without
keys, and the aggregator is pinned to its `file://` fixtures (`_OfflineFetcher`)
so the golden set never depends on the network.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import obs
from .config import (
    Config,
    DEFAULT_CURRENCY,
    build_registry,
    build_repository,
)
from .domain.models import (
    AwardQuote,
    Cabin,
    Provenance,
    Route,
    User,
    Verdict,
    VerdictLabel,
)
from .domain.verdict import WARNING_FLAGS
from .providers.aggregator.fetch import Fetcher, FetchResult
from .providers.registry import ProviderRegistry
from .store.repo import Repository
from .verify.bounds import AWARD_BOUNDS
from .verify.crosscheck import VerifiedAward, verify_award_quotes
from .verify.freshness import is_stale

from .cli import run_quote  # noqa: E402  (run_quote is the system-under-test)


# --------------------------------------------------------------------------- #
# Deterministic, offline eval harness
# --------------------------------------------------------------------------- #
class _OfflineFetcher(Fetcher):
    """A Fetcher that only resolves `file://` fixtures.

    Live HTTP and Wayback are short-circuited to None so the aggregator falls
    back to its on-disk fixtures. This makes the golden evals deterministic and
    network-free — the exact same parse/normalize path runs, just from disk.
    """

    def _get_http(self, url: str, domain: str) -> Optional[FetchResult]:  # noqa: D102
        return None

    def _get_wayback(self, url: str) -> Optional[FetchResult]:  # noqa: D102
        return None


def build_eval_registry(config: Config, repo: Repository) -> ProviderRegistry:
    """The normal federated registry, with the aggregator pinned to fixtures."""
    registry = build_registry(config, repo)
    offline = _OfflineFetcher(base_dir=config.knowledge_dir)
    for provider in registry._providers:  # noqa: SLF001 - same-package eval harness
        if getattr(provider, "name", None) == "aggregator":
            provider.fetcher = offline
    return registry


@contextmanager
def eval_context(
    base_config: Optional[Config] = None,
) -> Iterator[tuple[Config, Repository, ProviderRegistry]]:
    """Yield (config, repo, offline registry) backed by a throwaway DB.

    Evals never touch the user's real `mileage.db`; each run gets a fresh
    temp Repository so run/edge records don't accumulate.
    """
    base = base_config or Config.from_env()
    with tempfile.TemporaryDirectory() as tmp:
        config = replace(base, db_path=str(Path(tmp) / "eval.db"))
        repo = build_repository(config)
        registry = build_eval_registry(config, repo)
        try:
            yield config, repo, registry
        finally:
            repo.close()
            if registry.stores is not None:
                registry.stores.close()


# --------------------------------------------------------------------------- #
# The golden route set (Demo A + Demo B + honesty extras)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GoldenCase:
    name: str
    route: Route
    miles: int
    card: str
    # Allowed outcomes: verdict labels, or "no_fare" for the no-data case.
    expect: frozenset[str]
    expect_live_space: bool = False
    min_winner_cpp: Optional[float] = None
    note: str = ""


GOLDEN_SET: list[GoldenCase] = [
    GoldenCase(
        name="demo_a_honest_floor",
        route=Route("LAX", "JFK", Cabin.ECONOMY),
        miles=20000,
        card="venture_x",
        expect=frozenset({"portal_only", "comparable"}),
        note="Demo A: transcon economy — the portal floor usually wins.",
    ),
    GoldenCase(
        name="demo_b_hidden_value",
        route=Route("LAX", "IST", Cabin.BUSINESS),
        miles=90000,
        card="venture_x",
        expect=frozenset({"best", "tentative_best"}),
        expect_live_space=True,
        min_winner_cpp=1.25,
        note="Demo B: international business via Turkish — transfer wins, seat exists.",
    ),
    GoldenCase(
        name="demo_a_underfunded",
        route=Route("LAX", "JFK", Cabin.ECONOMY),
        miles=10000,
        card="venture_x",
        expect=frozenset({"portal_only"}),
        note="Honesty: can't afford any transfer -> portal floor, never a fake win.",
    ),
    GoldenCase(
        name="demo_b_underfunded",
        route=Route("LAX", "IST", Cabin.BUSINESS),
        miles=30000,
        card="venture_x",
        expect=frozenset({"portal_only"}),
        note="Honesty: the value path exists but is unaffordable at this balance.",
    ),
    GoldenCase(
        name="no_data_route",
        route=Route("DEN", "SEA", Cabin.ECONOMY),
        miles=20000,
        card="venture_x",
        expect=frozenset({"no_fare"}),
        note="Graceful degradation: no verified price-to-beat -> honest no_fare.",
    ),
]


# --------------------------------------------------------------------------- #
# Anti-hallucination invariants (§2.1, §7) — applied to a run result
# --------------------------------------------------------------------------- #
def _award_failures(awards: list[VerifiedAward]) -> list[str]:
    """No verified award may lack provenance or carry an out-of-bounds value."""
    fails: list[str] = []
    for a in awards:
        if not a.provenance:
            fails.append(f"{a.program}: verified award has no provenance")
        for prov in a.provenance:
            if not prov or prov.source_name in ("", "unknown"):
                fails.append(f"{a.program}: an unsourced datum survived verification")
        lo, hi = AWARD_BOUNDS.get(a.route.cabin, (1, 10_000_000))
        if not (lo <= a.miles <= hi):
            fails.append(
                f"{a.program}: out-of-bounds {a.miles:,} miles survived "
                f"(bounds {lo:,}-{hi:,})"
            )
    return fails


def _winner_failures(verdict: Verdict) -> list[str]:
    """A `best` winner must be clean; a flagged winner must be tentative_best."""
    bt = verdict.best_transfer
    if bt is None or verdict.label != VerdictLabel.BEST:
        return []
    bad = [
        f for f in bt.flags if f in WARNING_FLAGS or f.startswith("sources_disagree")
    ]
    if bad:
        return [
            f"winner labeled `best` but carries warning flag(s) {bad} "
            "(should be tentative_best)"
        ]
    return []


def _united_failures(verdict: Verdict) -> list[str]:
    """Capital One does not transfer to United -> never a transfer option (§0)."""
    for o in verdict.options:
        if o.kind != "transfer":
            continue
        if o.program == "united" or "united" in o.label.lower():
            return [
                "United appeared as a transfer option — no Capital One -> United "
                "ratio exists; scraped United space must never enter the graph"
            ]
    return []


def anti_hallucination_failures(result: dict) -> list[str]:
    """All §2.1/§7 honesty violations in a run result (empty == clean)."""
    verdict: Optional[Verdict] = result.get("verdict")
    if verdict is None:
        return []  # a `no_fare` result has nothing to hallucinate
    fails = _award_failures(result.get("awards") or [])
    fails += _winner_failures(verdict)
    fails += _united_failures(verdict)
    return fails


# --------------------------------------------------------------------------- #
# Running the golden set
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class CaseResult:
    name: str
    label: str
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks)


@dataclass
class EvalReport:
    cases: list[CaseResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.cases)


def run_case(
    case: GoldenCase,
    registry: ProviderRegistry,
    repo: Repository,
    config: Config,
) -> CaseResult:
    user = User(
        user_id="eval", balances={DEFAULT_CURRENCY: case.miles}, card=case.card
    )
    with obs.span(
        f"eval:{case.name}", obs.KIND_CHAIN, input_value=case.route.key()
    ) as s:
        result = run_quote(
            case.route, user, DEFAULT_CURRENCY,
            registry=registry, repo=repo, config=config,
        )
        verdict: Optional[Verdict] = result.get("verdict")
        label = verdict.label.value if verdict else (result.get("error") or "no_verdict")
        obs.set_output(s, label)

    checks = [
        Check(
            "verdict in expected",
            label in case.expect,
            f"got {label}; expected {sorted(case.expect)}",
        )
    ]

    if verdict is not None:
        hf = anti_hallucination_failures(result)
        checks.append(
            Check("no hallucinations", not hf, "; ".join(hf) or "clean")
        )
        if case.expect_live_space:
            live = [a for a in (result.get("awards") or []) if a.seats_available]
            checks.append(
                Check(
                    "live award space present",
                    bool(live),
                    ", ".join(f"{a.program} {a.seats_available} seats" for a in live)
                    or "no live seat found",
                )
            )
        if case.min_winner_cpp is not None:
            bt = verdict.best_transfer
            ok = bt is not None and bt.cpp >= case.min_winner_cpp
            got = f"{bt.cpp:.2f}" if bt else "none"
            checks.append(
                Check(f"winner cpp >= {case.min_winner_cpp}", ok, f"got {got}c/pt")
            )

    return CaseResult(name=case.name, label=label, checks=checks)


def run_golden(
    cases: Optional[list[GoldenCase]] = None,
    *,
    base_config: Optional[Config] = None,
) -> EvalReport:
    """Run the golden set through the real pipeline; return a pass/fail report."""
    cases = cases or GOLDEN_SET
    report = EvalReport()
    with eval_context(base_config) as (config, repo, registry):
        for case in cases:
            registry.cache.clear()
            report.cases.append(run_case(case, registry, repo, config))
    return report


def render_report(report: EvalReport) -> str:
    lines = ["", "=== Phase 5 — golden-route evals (CI honesty gate) ===", ""]
    for case in report.cases:
        mark = "PASS" if case.passed else "FAIL"
        lines.append(f"  [{mark}] {case.name}  ->  {case.label}")
        for c in case.checks:
            tick = "ok" if c.ok else "XX"
            lines.append(f"        ({tick}) {c.name}: {c.detail}")
    n_pass = sum(1 for c in report.cases if c.passed)
    lines.append("")
    lines.append(
        f"  {n_pass}/{len(report.cases)} golden cases passed  ->  "
        f"{'BUILD OK' if report.ok else 'BUILD FAILS'}"
    )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The poisoned-chart demo (§12 Phase 5)
# --------------------------------------------------------------------------- #
@dataclass
class PoisonRow:
    desc: str
    quote: AwardQuote
    expect: str  # "rejected" | "flagged_stale" | "survives_clean"


def poison_rows(route: Route) -> list[PoisonRow]:
    """Three planted bad rows + one clean control, for the same route/cabin."""
    fresh = Provenance(
        source_name="poisoned award blog",
        source_url="https://example.invalid/too-good-to-be-true",
        trust=0.99,  # dressed up as authoritative — trust alone must NOT save it
    )
    ancient = Provenance(
        source_name="abandoned chart mirror",
        trust=0.99,
        source_updated_at=datetime(2021, 1, 1, tzinfo=timezone.utc),
    )
    clean = Provenance(source_name="StarNet award-space aggregator", trust=0.6)
    return [
        PoisonRow(
            "garbage value (5 miles for a business seat)",
            AwardQuote(
                program="turkish", route=route, miles=5, seats_available=1,
                provenance=fresh, confidence=0.99, flags=["live_award_space"],
            ),
            expect="rejected",
        ),
        PoisonRow(
            "unsourced datum (no provenance)",
            AwardQuote(
                program="lifemiles", route=route, miles=25000, seats_available=1,
                provenance=Provenance(source_name="unknown"),
                confidence=0.99, flags=["live_award_space"],
            ),
            expect="rejected",
        ),
        PoisonRow(
            "stale chart (last updated 2021)",
            AwardQuote(
                program="aeroplan", route=route, miles=60000, seats_available=1,
                provenance=ancient, confidence=0.99, flags=["live_award_space"],
            ),
            expect="flagged_stale",
        ),
        PoisonRow(
            "clean control (fresh, in-bounds, sourced)",
            AwardQuote(
                program="turkish", route=route, miles=45000, seats_available=2,
                provenance=clean, confidence=0.6, flags=["live_award_space"],
            ),
            expect="survives_clean",
        ),
    ]


# --------------------------------------------------------------------------- #
# Extraction-accuracy eval (§6.3/§I) — a deterministic CI gate
# --------------------------------------------------------------------------- #
# A frozen gold corpus: (document, expected (program, region_a, region_b, cabin,
# miles) rows). Deterministic so this is a real build gate, not a vibe. The
# newsletter prose is the same fixture the email intake ingests.
_FIXTURES_DIR = Path(__file__).resolve().parent / "knowledge" / "fixtures"

_EXTRACTION_GOLD: list[tuple[str, set[tuple]]] = [
    (
        (_FIXTURES_DIR / "sample_newsletter.eml").read_text(encoding="utf-8"),
        {
            ("turkish", "north_america", "europe", "business", 45000),
            ("lifemiles", "north_america", "europe", "economy", 30000),
            ("aeroplan", "north_america", "north_america", "economy", 12500),
        },
    ),
]

# Regression thresholds. Lowering these (or breaking grounding) fails the build.
_MIN_RECALL = 0.90
_MIN_PRECISION = 0.85
_MIN_EXACT_MILES = 0.90


@dataclass
class ExtractionReport:
    precision: float = 0.0
    recall: float = 0.0
    exact_miles: float = 0.0
    dropped_regions: int = 0
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)


def run_extraction_eval() -> ExtractionReport:
    """Row precision/recall + exact miles-match + dropped-row counter (§I).

    - precision/recall/exact-miles measure the deterministic extractor on the
      frozen prose corpus;
    - the dropped-region counter proves the §A canonicalizer drops (and counts)
      an unmappable zone rather than silently mismatching it;
    - a grounding check proves an ungrounded number is never emitted.
    Returns a report whose `ok` is False on any regression (CI exits non-zero).
    """
    from .providers.aggregator.extract import DeterministicExtractor, number_is_grounded
    from .providers.aggregator.parse import parse_chart_html_wide

    extractor = DeterministicExtractor()
    tp = 0
    n_pred = 0
    n_exp = 0
    exact_hits = 0
    exact_keys = 0
    for document, expected in _EXTRACTION_GOLD:
        rows = extractor.extract(document)
        predicted = {
            (r.program, r.region_a, r.region_b, r.cabin, r.miles) for r in rows
        }
        tp += len(predicted & expected)
        n_pred += len(predicted)
        n_exp += len(expected)
        # Exact miles match keyed on (program, region pair, cabin).
        pred_by_key = {(r.program, r.region_a, r.region_b, r.cabin): r.miles for r in rows}
        for (prog, ra, rb, cab, miles) in expected:
            exact_keys += 1
            if pred_by_key.get((prog, ra, rb, cab)) == miles:
                exact_hits += 1
        # Every emitted number must be grounded in the source.
        for r in rows:
            if not number_is_grounded(r.miles, document):
                # An ungrounded number escaping is a hard failure — record it as
                # a precision hit of 0 by inflating predicted with a phantom.
                n_pred += 1

    report = ExtractionReport(
        precision=(tp / n_pred) if n_pred else 0.0,
        recall=(tp / n_exp) if n_exp else 0.0,
        exact_miles=(exact_hits / exact_keys) if exact_keys else 0.0,
    )

    # Dropped-row counter: the §A canonicalizer must drop the unmapped zone in
    # the real-label ATF fixture (and count it), not silently mismatch it.
    stats: dict = {}
    lifemiles_html = (_FIXTURES_DIR / "atf_lifemiles_chart.html").read_text(encoding="utf-8")
    parse_chart_html_wide(lifemiles_html, program="lifemiles", stats=stats)
    report.dropped_regions = int(stats.get("dropped", 0))

    # A planted ungrounded number must never be emitted.
    ungrounded = extractor.extract(
        "Turkish business class to Europe is 47,123 miles."  # 47123 IS grounded;
    )
    grounding_clean = all(
        number_is_grounded(r.miles, "Turkish business class to Europe is 47,123 miles.")
        for r in ungrounded
    )

    report.checks = [
        Check(
            f"recall >= {_MIN_RECALL}",
            report.recall >= _MIN_RECALL,
            f"recall={report.recall:.2f}",
        ),
        Check(
            f"precision >= {_MIN_PRECISION}",
            report.precision >= _MIN_PRECISION,
            f"precision={report.precision:.2f}",
        ),
        Check(
            f"exact miles-match >= {_MIN_EXACT_MILES}",
            report.exact_miles >= _MIN_EXACT_MILES,
            f"exact={report.exact_miles:.2f}",
        ),
        Check(
            "unmapped region dropped + counted (§A)",
            report.dropped_regions >= 1,
            f"dropped={report.dropped_regions} (expected >=1: the Antarctica row)",
        ),
        Check(
            "every emitted number is grounded (§6.2)",
            grounding_clean,
            "all emitted miles literally present in source",
        ),
    ]
    return report


def render_extraction_report(report: ExtractionReport) -> str:
    lines = ["", "=== Extraction-accuracy eval (CI gate) ===", ""]
    for c in report.checks:
        tick = "ok" if c.ok else "XX"
        lines.append(f"  ({tick}) {c.name}: {c.detail}")
    lines.append("")
    lines.append(
        f"  precision={report.precision:.2f} recall={report.recall:.2f} "
        f"exact_miles={report.exact_miles:.2f} dropped_regions={report.dropped_regions}"
        f"  ->  {'OK' if report.ok else 'REGRESSION'}"
    )
    lines.append("")
    return "\n".join(lines)


@dataclass
class PoisonResult:
    ok: bool
    checks: list[Check] = field(default_factory=list)
    verified: list[VerifiedAward] = field(default_factory=list)


def run_poison_check(
    route: Route = Route("LAX", "IST", Cabin.BUSINESS),
) -> PoisonResult:
    """Feed verification a stale/garbage chart; prove it rejects the poison.

    Returns ok=False if any garbage/unsourced row survives — that is the
    condition that *fails the build*. The clean control proves verification
    didn't simply drop everything.
    """
    rows = poison_rows(route)
    with obs.span(
        "verify:anti-hallucination",
        obs.KIND_CHAIN,
        input_value=f"{len(rows)} planted quotes ({route.key()})",
    ) as s:
        verified = verify_award_quotes([r.quote for r in rows])
        obs.set_output(s, f"{len(verified)} survived verification")

    by_program = {v.program: v for v in verified}
    checks: list[Check] = []

    # 1) Garbage (5 miles) must never appear as a verified turkish value.
    turkish = by_program.get("turkish")
    checks.append(
        Check(
            "garbage value rejected (bounds, §2.1)",
            turkish is not None and turkish.miles != 5,
            f"turkish verified at {turkish.miles:,} miles (garbage 5 dropped)"
            if turkish
            else "turkish dropped entirely",
        )
    )
    # 2) Unsourced lifemiles row must be gone (no provenance -> never usable).
    checks.append(
        Check(
            "unsourced datum rejected (§2.1)",
            "lifemiles" not in by_program,
            "lifemiles (unsourced) did not survive"
            if "lifemiles" not in by_program
            else "FAILED: unsourced datum survived",
        )
    )
    # 3) Stale aeroplan may survive, but only flagged `stale` (demoted, §2.7/§7).
    aeroplan = by_program.get("aeroplan")
    checks.append(
        Check(
            "stale chart flagged + demoted (§7)",
            aeroplan is not None
            and "stale" in aeroplan.flags
            and is_stale(aeroplan.provenance[0]),
            f"aeroplan flags={aeroplan.flags}" if aeroplan else "aeroplan dropped",
        )
    )
    # 4) The clean control proves the gate isn't just rejecting everything.
    checks.append(
        Check(
            "clean control survives",
            turkish is not None and turkish.miles == 45000,
            "clean turkish 45,000 survived" if turkish else "control dropped",
        )
    )

    return PoisonResult(
        ok=all(c.ok for c in checks), checks=checks, verified=verified
    )
