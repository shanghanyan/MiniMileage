import os, sys
if os.environ.get("MILEAGE_OFFLINE", "1") not in ("0", "false", "no"):
    print("SKIP: set MILEAGE_OFFLINE=0 to run live scrape test")
    # Hard-exit only when run as a standalone script. Under pytest (conftest pins
    # MILEAGE_OFFLINE=1) a top-level sys.exit() raises SystemExit during
    # collection and aborts the whole suite; there pytest just finds no test_*
    # functions here, so the file is a silent no-op as intended.
    if "pytest" not in sys.modules:
        sys.exit(0)
    else:
        import pytest
        pytest.skip("set MILEAGE_OFFLINE=0 to run live scrape test", allow_module_level=True)

"""Live scrape smoke test — fetch -> parse -> resolve, per target (§6).

Walks every HTTP(S) target in `knowledge/sources.yaml` and runs three stages
against the REAL production fetch/parse/resolve stack (no fakes), stopping at
the first failure per target. It is intentionally NOT a pytest test: under a
normal `pytest` run `MILEAGE_OFFLINE=1` (conftest default) makes the guard
above exit 0 immediately. To actually hit the network:

    MILEAGE_OFFLINE=0 python tests/test_live_scrape.py

Each stage's failure is attributable to a specific target because we call the
Fetcher and parsers directly rather than `provider.fetch(Query(...))` (which
loops over every target internally and would erase per-target visibility).
"""

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.domain.models import Cabin, Layer, Route
from mileage.providers.aggregator.fetch import Fetcher
from mileage.providers.aggregator.parse import (
    parse_chart_html, parse_chart_html_wide, parse_award_json,
    parse_chart_json, parse_rss, parse_chart_pdf,
)
from mileage.providers.aggregator.provider import AggregatorProvider
from mileage.providers.aggregator.sources import load_targets
from mileage.domain.charts import lookup_award_miles

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "mileage" / "knowledge"

# Two fixed probe routes used by stage 3 (resolve). These mirror the demo
# scenarios so a chart that parses but resolves nothing is caught here.
_PROBES = [
    Route("LAX", "IST", Cabin.BUSINESS),  # Demo B: Aeroplan / LifeMiles / Turkish
    Route("LAX", "JFK", Cabin.ECONOMY),   # Demo A: Aeroplan
]

# Programs we hold to the strict bar: failing to resolve EITHER probe is a hard
# failure. Other programs (e.g. ANA, different hubs) only warn — their charts
# legitimately may not cover these exact city pairs.
_STRICT_PROGRAMS = {"aeroplan", "lifemiles"}

_CABIN_ABBR = {
    "economy": "eco",
    "premium_economy": "pe",
    "business": "biz",
    "first": "first",
}


def _parse_rows(target, result, stats):
    """Run the right parser for `target.format`, returning normalized rows.

    PDF parsing takes the undecoded `result.raw` bytes (the decoded `text` is
    lossy); the `stats` dict is threaded through every parser that accepts it so
    we can surface silent drops (uncanonicalizable regions) even when rows > 0.
    """
    fmt = target.format
    if fmt == "html_table":
        return parse_chart_html(result.text, stats=stats)
    if fmt == "html_table_wide":
        prog = target.program or target.name
        return parse_chart_html_wide(result.text, program=prog, stats=stats)
    if fmt == "json":
        if target.provides == "award":
            return parse_award_json(result.text)
        return parse_chart_json(result.text, stats=stats)
    if fmt == "rss":
        charts, awards = parse_rss(result.text)
        return awards if target.provides == "award" else charts
    if fmt == "pdf":
        prog = target.program or target.name
        return parse_chart_pdf(result.raw, program=prog, stats=stats)
    return []


def _looks_undecoded(text: str) -> bool:
    """Heuristic: a body that is mostly non-printable is an *undecoded* response
    (e.g. a Brotli/zstd payload httpx couldn't inflate), NOT an HTML shell. This
    is the failure mode that masquerades as a parser miss — surface it as such.
    """
    if not text:
        return False
    sample = text[:2000]
    printable = sum(1 for c in sample if c.isprintable() or c in "\r\n\t ")
    return (printable / len(sample)) < 0.85


def _diagnose_zero_rows(target, result) -> str:
    """Attribute a 0-row parse to a *specific* cause instead of always blaming
    the parser. The old message ("selector miss or wrong table structure")
    couldn't tell an undecoded/shell fetch apart from a real schema mismatch,
    which sent past debugging sessions after the parser for what was a fetch
    problem. This reports via/bytes and, for HTML, the actual <table> count.
    """
    fmt = target.format
    via = result.via
    n = len(result.text)
    prefix = f"0 rows (fmt={fmt}, via={via}, bytes={n}"

    if fmt in ("html_table", "html_table_wide"):
        tables = result.text.lower().count("<table")
        if _looks_undecoded(result.text):
            return (
                f"{prefix}, tables=?) — body is UNDECODED (mostly non-printable): "
                "server sent a content-encoding we can't inflate (install the "
                "[aggregator] extra for brotli/zstd). NOT a parser bug."
            )
        if tables == 0:
            return (
                f"{prefix}, tables=0) — no <table> in body: JS-rendered shell, "
                "wrong URL, or non-HTML content. A fetch/source problem, not the parser."
            )
        return (
            f"{prefix}, tables={tables}) — body has {tables} table(s) but none "
            "match the from/to/distance + cabin wide schema (schema mismatch — "
            "e.g. a guide/prose page). Parser working as designed on this shape."
        )

    if fmt == "pdf":
        return (
            f"{prefix}) — pdfplumber extracted no from/distance/cabin table grid. "
            "Official PDFs are visually laid out (merged cells / multi-row headers); "
            "needs a PDF-specific parser, not the wide-table selector."
        )

    return f"{prefix}) — parser produced no rows for this format."


def check_target(target, fetcher, agg):
    """Run fetch -> parse -> resolve for one target.

    Returns (status, detail) where status is one of "ok" / "WARN" / "FAIL".
    """
    # ---- Stage 1: fetch ---------------------------------------------------- #
    result = fetcher.get(target.url)
    if result is None:
        return "FAIL", "fetch returned None (chain exhausted — check logs above)"
    if not result.ok:
        return "FAIL", f"fetch not ok: status={result.status} via={result.via}"
    if len(result.text) < 200:
        return (
            "FAIL",
            f"fetch returned suspiciously short body ({len(result.text)} bytes) "
            "— possible JS shell or challenge page",
        )

    # ---- Stage 2: parse ---------------------------------------------------- #
    stats: dict = {}
    rows = _parse_rows(target, result, stats)
    dropped = stats.get("dropped", 0)
    if not rows:
        reason = _diagnose_zero_rows(target, result)
        # Live award space fluctuates, so 0 rows is a weak signal: warn, not fail.
        if target.provides == "award":
            return "WARN", reason
        return "FAIL", reason

    # ---- Stage 3: resolve (chart targets only) ----------------------------- #
    if target.provides != "chart":
        return "ok", f"{len(rows)} rows (dropped={dropped})"

    chart_by_program = agg._build_charts(rows)
    hit_detail = None
    for route in _PROBES:
        for program, chart in chart_by_program.items():
            hit = lookup_award_miles(
                program, chart, route, agg._region_map,
                airport_coords=agg._airport_coords,
            )
            if hit is not None:
                cab = _CABIN_ABBR.get(route.cabin.value, route.cabin.value)
                hit_detail = (
                    f"resolved {route.origin}->{route.dest} {cab} "
                    f"{hit.miles}mi ({program})"
                )
                break
        if hit_detail:
            break

    if hit_detail is None:
        reason = (
            "rows parsed but 0 resolved to a route quote — likely region label "
            f"mismatch (check drops={dropped})"
        )
        # Not resolving these exact city pairs isn't fatal for a program whose
        # hubs differ (e.g. ANA); only the strict programs hard-fail.
        if target.program in _STRICT_PROGRAMS:
            return "FAIL", reason
        return "WARN", reason

    return "ok", f"{len(rows)} rows (dropped={dropped})  {hit_detail}"


def main() -> int:
    fetcher = Fetcher(
        offline=False,
        impersonate=True,       # curl_cffi TLS fallback if installed
        use_wayback=True,
        timeout=15.0,
        max_429_retries=1,
    )
    # Reuse the production provider for _build_charts / _region_map /
    # _airport_coords so we don't reimplement chart assembly or geography.
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=False)

    targets = load_targets(_KNOWLEDGE / "sources.yaml")

    ok = warn = fail = 0
    total = len(targets)
    for target in targets:
        url = target.url
        if not (url.startswith("https://") or url.startswith("http://")):
            # file:// fixtures exercise the same parse path offline; skip them
            # in the live run but still account for them as a (trivial) pass.
            print(f"[{'ok':^5}]  {target.name:<26}  {url[:60]:<60}  <skipped: file:// fixture>")
            ok += 1
            continue

        try:
            status, detail = check_target(target, fetcher, agg)
        except Exception as exc:  # a parser/fetch bug is itself a target failure
            status, detail = "FAIL", f"unexpected error: {type(exc).__name__}: {exc}"

        print(f"[{status:^5}]  {target.name:<26}  {url[:60]:<60}  {detail}")
        if status == "ok":
            ok += 1
        elif status == "WARN":
            warn += 1
        else:
            fail += 1

    print(f"\n{ok}/{total} targets ok, {warn} warnings, {fail} failures")
    return 1 if fail > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
