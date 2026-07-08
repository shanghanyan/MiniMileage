"""Regression tests for live_scrape.check_target / _resolve_detail (§6).

`check_target` is the fetch -> parse -> resolve path shared by
`tests/test_live_scrape.py` (a CLI harness that only runs live, with
`MILEAGE_OFFLINE=0`, and is a no-op under normal pytest) and the
`GET /scrape/live` endpoint the UI's Live Scrape page calls.

Because the CLI harness is offline-skipped by default, `check_target` /
`_resolve_detail` had ZERO test coverage under CI — which is exactly how a
`self` vs `agg` typo in `_resolve_detail` (a free function, not a method, so
`self` was never defined) went unnoticed: every chart target's resolve stage
raised `NameError`, silently caught by `run_live_scrape`'s try/except and
reported as a hard `fail`. Fetch and parse both worked; only the resolve step
was broken, but the Live Scrape page showed every single target failing.

These tests exercise `check_target` OFFLINE end to end (fetch -> parse ->
resolve) via a `FakeFetcher`, no network, so that bug — or one like it in the
resolve stage specifically — fails CI immediately instead of only surfacing
against live traffic.

Run offline:  python tests/test_live_scrape_check_target.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("MILEAGE_OFFLINE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.providers.aggregator.fetch import FetchResult
from mileage.providers.aggregator.live_scrape import check_target
from mileage.providers.aggregator.provider import AggregatorProvider
from mileage.providers.aggregator.sources import Target

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "mileage" / "knowledge"
_FIXTURES = _KNOWLEDGE / "fixtures"


class _FakeFetcher:
    """Maps exactly one URL -> a canned body. No network, ever (§ test_phase8b)."""

    offline = False  # check_target's offline-branch message checks this attr

    def __init__(self, url: str, body: str) -> None:
        self._url = url
        self._body = body

    def get(self, url: str):
        if url != self._url:
            return None
        return FetchResult(
            url=url, text=self._body, status=200, final_url=url, via="file",
            raw=self._body.encode("utf-8"),
        )


def test_check_target_resolves_aeroplan_wide_chart() -> None:
    """Wide-table chart target: fetch -> parse -> RESOLVE, all via check_target.

    This is the exact call path (`check_target` -> `_resolve_detail`) that
    crashed with `NameError: name 'self' is not defined` before the fix.
    """
    url = "https://example.test/aeroplan-chart"
    body = (_FIXTURES / "atf_aeroplan_chart.html").read_text(encoding="utf-8")
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    agg.fetcher = _FakeFetcher(url, body)

    target = Target(
        name="atf-aeroplan-test", url=url, format="html_table_wide",
        provides="chart", program="aeroplan", role="primary",
    )
    res = check_target(target, agg)
    assert res.status == "ok", res.detail
    assert res.rows > 0
    assert res.resolved is not None and "aeroplan" in res.resolved


def test_check_target_resolves_krisflyer_destination_chart() -> None:
    """Hub-based destination-table target also resolves end to end."""
    url = "https://example.test/krisflyer-guide"
    body = (_FIXTURES / "atf_krisflyer_guide.html").read_text(encoding="utf-8")
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    agg.fetcher = _FakeFetcher(url, body)

    target = Target(
        name="atf-krisflyer-test", url=url, format="html_table_destination",
        provides="chart", program="krisflyer", hub="SIN", role="primary",
    )
    res = check_target(target, agg)
    assert res.status == "ok", res.detail
    assert res.rows > 0
    assert res.resolved is not None and "117000mi" in res.resolved


def test_check_target_reports_unresolved_rows_as_fail_or_warn() -> None:
    """Rows that parse but resolve NOTHING must not crash — and must not read
    as 'ok'. Guards the other branch of `_resolve_detail`'s caller."""
    url = "https://example.test/turkish-guide"
    body = (_FIXTURES / "atf_turkish_guide.html").read_text(encoding="utf-8")
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=True)
    agg.fetcher = _FakeFetcher(url, body)

    # Wrong hub on purpose: Turkish rows are hubbed at IST, not SIN, so nothing
    # the DEFAULT_PROBES ask for should resolve against this chart.
    target = Target(
        name="atf-turkish-wrong-hub", url=url, format="html_table_destination",
        provides="chart", program="turkish", hub="SIN", role="primary",
    )
    res = check_target(target, agg)
    assert res.status in ("fail", "warn"), res.detail
    assert res.resolved is None


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
