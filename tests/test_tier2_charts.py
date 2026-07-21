"""Tier 2 URL expansion (2026-07-20) — proves the sources.yaml additions.

Runs standalone (`python tests/test_tier2_charts.py`) and is pytest-
discoverable. OFFLINE and deterministic: each test reconstructs a faithful
HTML snippet of the REAL live page fetched during this session (verbatim
headers/values, same reconstruction discipline used for the EVA/10x-krisflyer
additions in sources.yaml on 2026-07-08) and runs it through the ACTUAL
`parse_chart_html_wide` + `canonicalize_region` in this repo — not hand-traced,
not hardcoded expected output. This is what justifies the trust/role/comments
attached to the seven new `atf-*` targets in sources.yaml plus the
`dubai`/`abu dhabi` -> middle_east additions in regions.py.

Not a live network fetch through the real Fetcher stack (this sandbox has no
HTTP egress from its shell — see mileage-sandbox-network-limits memory); the
open risk is the real page's raw HTML structure differing from this
reconstruction, not the region-mapping logic, which these tests do prove.
Run `mileage sources --validate-urls --deep` on your own machine for the
final live confirmation, same as every other addition in sources.yaml.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.providers.aggregator.parse import parse_chart_html_wide
from mileage.providers.aggregator.regions import canonicalize_region


def _wide_html(rows: list[tuple]) -> str:
    """Wrap (from, to, economy, business, first, note) tuples in the same
    decoy-nav + real-table shape ATF pages actually use (see
    atf_aeroplan_chart.html) so `_select_wide_table` has to pick correctly."""
    body_rows = "\n".join(
        f"<tr><td>{a}</td><td>{b}</td><td>{e}</td><td>{biz}</td><td>{f}</td><td>{n}</td></tr>"
        for a, b, e, biz, f, n in rows
    )
    return f"""<!doctype html><html><body>
<nav><table><tr><th>Programs</th><th>Cards</th></tr><tr><td>X</td><td>Y</td></tr></table></nav>
<table>
<thead><tr><th>From</th><th>To</th><th>Economy</th><th>Business</th><th>First</th><th>Note</th></tr></thead>
<tbody>
{body_rows}
</tbody>
</table>
</body></html>"""


def test_flying_blue_zone_pair_resolves() -> None:
    """Verbatim rows from awardtravelfinder.com/award-charts/flying-blue
    (fetched 2026-07-20). Ranges use the lower bound per _emit_wide_rows."""
    html = _wide_html([
        ("Europe", "North Africa", "10,000-20,000", "25,000-45,000", "—", ""),
        ("North America", "Europe (transatlantic)", "25,000-55,000", "55,000-120,000", "100,000-200,000", ""),
        ("North America", "Middle East", "25,000-55,000", "55,000-120,000", "—", ""),
    ])
    stats: dict = {}
    rows = parse_chart_html_wide(html, program="flying_blue", stats=stats)
    triples = {(r.region_a, r.region_b, r.cabin, r.miles) for r in rows}
    assert ("europe", "africa", "economy", 10000) in triples
    assert ("north_america", "europe", "business", 55000) in triples
    assert ("north_america", "middle_east", "economy", 25000) in triples
    assert stats.get("dropped", 0) == 0


def test_qantas_zone_pair_partial_resolve() -> None:
    """Verbatim rows from awardtravelfinder.com/award-charts/qantas (fetched
    2026-07-20). 5 of 8 real rows resolve; 3 are dropped + counted, never
    guessed — proving the 5/8 real-coverage claim in sources.yaml."""
    html = _wide_html([
        ("Australia", "Within Australia (short)", "9,000", "20,000", "—", ""),   # drop
        ("Australia", "Within Australia (long)", "14,000-20,000", "41,000-62,000", "—", ""),  # drop
        ("Australia", "New Zealand", "20,000", "41,000", "—", ""),
        ("Australia", "Southeast Asia", "29,000-40,000", "72,000-86,000", "—", ""),
        ("Australia", "Northeast Asia", "40,000-52,000", "86,000-108,000", "—", ""),
        ("Australia", "US / Canada", "40,000-62,000", "115,000-167,000", "194,000-259,000", ""),  # drop
        ("Australia", "Europe", "48,000-62,000", "115,000-167,000", "194,000-259,000", ""),
        ("Australia", "Middle East / Africa", "48,000-62,000", "115,000-167,000", "194,000-259,000", ""),
    ])
    stats: dict = {}
    rows = parse_chart_html_wide(html, program="qantas", stats=stats)
    resolved_to = {r.region_b for r in rows}
    assert resolved_to == {"oceania", "southeast_asia", "north_asia", "europe", "middle_east"}
    assert stats.get("dropped", 0) == 3  # the two "Within Australia" rows + "US / Canada"
    europe_biz = [r for r in rows if r.region_b == "europe" and r.cabin == "business"]
    assert europe_biz and europe_biz[0].miles == 115000  # lower bound of the range


def test_etihad_hub_token_fully_resolves() -> None:
    """Verbatim rows from awardtravelfinder.com/award-charts/etihad-airways
    (fetched 2026-07-20). Requires the 'abu dhabi' -> middle_east addition in
    regions.py — all 6 real rows resolve with it, proving the sources.yaml
    claim of full coverage for this source."""
    html = _wide_html([
        ("Abu Dhabi", "Middle East", "5,000-10,000", "15,000-20,000", "—", ""),
        ("Abu Dhabi", "Indian Subcontinent", "13,000-15,000", "30,000-35,000", "55,000-80,000", ""),
        ("Abu Dhabi", "Europe (London ~3,400mi)", "30,000", "70,000", "120,000", ""),
        ("Abu Dhabi", "SE Asia (Singapore ~3,400mi)", "30,000", "70,000", "120,000", ""),
        ("Abu Dhabi", "North America (NYC ~6,800mi)", "60,000", "120,000", "160,000", ""),
        ("Abu Dhabi", "Australia (SYD ~7,500mi)", "60,000", "120,000", "160,000", ""),
    ])
    assert canonicalize_region("Abu Dhabi") == "middle_east"
    stats: dict = {}
    rows = parse_chart_html_wide(html, program="etihad", stats=stats)
    assert stats.get("dropped", 0) == 0
    resolved_to = {r.region_b for r in rows}
    assert resolved_to == {
        "middle_east", "south_asia", "europe", "southeast_asia",
        "north_america", "oceania",
    }
    assert all(r.region_a == "middle_east" for r in rows)  # every row hubs off Abu Dhabi


def test_emirates_hub_token_partial_resolve() -> None:
    """Verbatim rows from awardtravelfinder.com/award-charts/emirates
    (fetched 2026-07-20). Requires 'dubai' -> middle_east. Only 2 of 4 rows
    resolve — bare 'Asia' is genuinely ambiguous (north/southeast/south) and
    is correctly dropped, not guessed, proving the 2/4 claim in sources.yaml."""
    html = _wide_html([
        ("Dubai", "Europe", "~30,000", "~70,000", "~120,000", ""),
        ("Dubai", "North America", "~60,000", "~120,000", "~163,500", ""),
        ("Dubai", "Asia (Short-Haul)", "~25,000", "~60,000", "—", ""),   # drop
        ("Dubai", "Asia (Long-Haul)", "~37,000", "~95,000", "—", ""),    # drop
    ])
    assert canonicalize_region("Dubai") == "middle_east"
    stats: dict = {}
    rows = parse_chart_html_wide(html, program="emirates", stats=stats)
    resolved_to = {r.region_b for r in rows}
    assert resolved_to == {"europe", "north_america"}
    assert stats.get("dropped", 0) == 2


def test_avios_distance_band_shape_does_not_resolve_today() -> None:
    """Verbatim rows from awardtravelfinder.com/award-charts/british-airways
    (fetched 2026-07-20) — proves the sources.yaml claim that this shape
    (Zone N / distance-range, no region names) yields 0 rows with the
    existing html_table_wide parser, safely (dropped + counted), not wrongly.
    This is the gap a future 'global distance-band' parser needs to close."""
    html = _wide_html([
        ("Zone 1", "0-650 miles", "4,000", "7,750", "—", "Off-peak"),
        ("Zone 3", "1,151-2,000 miles", "8,500", "17,000", "25,500", "Off-peak"),
        ("Zone 8", "6,501+ miles", "32,500", "68,000", "102,000", "Off-peak"),
    ])
    stats: dict = {}
    rows = parse_chart_html_wide(html, program="avios", stats=stats)
    assert rows == []
    assert stats.get("dropped", 0) == 3  # neither "Zone N" nor the mi-range canonicalizes


def test_cathay_distance_band_shape_does_not_resolve_today() -> None:
    """Same distance-band gap as Avios, verbatim from
    awardtravelfinder.com/award-charts/cathay-pacific (fetched 2026-07-20)."""
    html = _wide_html([
        ("0-750 mi", "Short-haul Asia", "7,000", "16,000", "25,000", ""),
        ("5,001-7,500 mi", "Ultra long-haul (e.g. HKG-LHR)", "27,000", "88,000", "125,000", ""),
    ])
    stats: dict = {}
    rows = parse_chart_html_wide(html, program="cathay_pacific", stats=stats)
    assert rows == []
    assert stats.get("dropped", 0) == 2


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
