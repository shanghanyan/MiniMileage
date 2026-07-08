"""Dump the ACTUAL table shapes pdfplumber / the HTML wide-table parser see on
the fallback chart targets currently reporting 0 rows on the Live Scrape page.

The offline tests (`tests/test_pdf_chart.py`, etc.) only exercise synthetic
fixtures shaped like what we *expect* the real page/PDF to look like. If the
live page/PDF has drifted (different header wording, merged cells, extra
columns, a redesign), the selector silently misses and we get "0 rows" with no
way to see WHY from inside the sandbox that fetched it (no network here).

This script fetches each target for real, saves the raw bytes AND a compact
dump of the table headers/first rows it actually finds, so the parser
selectors (`_select_wide_table`, `_is_aeroplan_distance_pdf_table`,
`_is_zone_matrix_table` in mileage/providers/aggregator/parse.py) can be
adjusted to match reality instead of guesswork.

Run on your own machine (needs real network — this can't run in the sandbox):

    .venv/bin/python scripts/dump_scrape_target_shapes.py

Writes, per target, into `_debug_dumps/` at the repo root:
  - <name>.raw.pdf / <name>.raw.html   — the exact bytes fetched
  - <name>.shape.txt                   — human-readable table/header dump
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.providers.aggregator.fetch import Fetcher
from mileage.providers.aggregator.parse import _ChartTableParser
from mileage.providers.aggregator.provider import AggregatorProvider

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "mileage" / "knowledge"
_OUT = Path(__file__).resolve().parent.parent / "_debug_dumps"

# The fallback targets currently showing 0 rows on the Live Scrape page.
# Edit this set if a different/additional target needs diagnosing.
_TARGET_NAMES = {
    "aeroplan-official-pdf",
    "krisflyer-official-pdf",
    "lifemiles-chart-pdf",
    "10x-turkish-chart",
    "10x-ana-chart",
}


def dump_pdf(name: str, data: bytes) -> str:
    import pdfplumber

    lines = [f"=== {name} — {len(data)} bytes ==="]
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        lines.append(f"pages: {len(pdf.pages)}")
        shown = 0
        for pi, page in enumerate(pdf.pages):
            tables = page.extract_tables() or []
            if not tables:
                continue
            text = (page.extract_text() or "")[:300]
            lines.append(f"\n--- page {pi} --- text[:300]={text!r}")
            for ti, t in enumerate(tables):
                lines.append(f"  table {ti}: {len(t)} row(s), header={t[0] if t else None!r}")
                for row in t[1:4]:
                    lines.append(f"    {row}")
            shown += 1
            if shown >= 12:
                lines.append("\n... (truncated — showing first 12 pages with tables)")
                break
    return "\n".join(lines)


def dump_html_wide(name: str, text: str) -> str:
    parser = _ChartTableParser()
    parser.feed(text)
    lines = [f"=== {name} — {len(text)} chars, {len(parser.tables)} table(s) ==="]
    for ti, t in enumerate(parser.tables):
        lines.append(f"\ntable {ti}: header={t.header}")
        for row in t.rows[:3]:
            lines.append(f"  {row}")
    return "\n".join(lines)


def main() -> int:
    _OUT.mkdir(exist_ok=True)
    agg = AggregatorProvider(knowledge_dir=_KNOWLEDGE, offline=False)
    fetcher = Fetcher(offline=False, impersonate=True, use_wayback=False, timeout=25.0)

    found = {t.name for t in agg.targets} & _TARGET_NAMES
    missing = _TARGET_NAMES - found
    if missing:
        print(f"NOTE: not found in sources.yaml (skipping): {sorted(missing)}")

    for target in agg.targets:
        if target.name not in _TARGET_NAMES:
            continue
        print(f"fetching {target.name} ({target.url}) ...")
        result = fetcher.get(target.url)
        if result is None or not result.ok:
            status = getattr(result, "status", None)
            via = getattr(result, "via", None)
            print(f"  FAILED to fetch (result={result!r}, status={status}, via={via})")
            (_OUT / f"{target.name}.shape.txt").write_text(
                f"FETCH FAILED for {target.url}\nresult={result!r}\n", encoding="utf-8"
            )
            continue

        raw = result.raw or result.text.encode("utf-8", "ignore")
        ext = "pdf" if target.format == "pdf" else "html"
        (_OUT / f"{target.name}.raw.{ext}").write_bytes(raw)

        try:
            if target.format == "pdf":
                shape = dump_pdf(target.name, raw)
            else:
                shape = dump_html_wide(target.name, result.text)
        except Exception as exc:  # noqa: BLE001
            shape = f"=== {target.name} — dump raised {type(exc).__name__}: {exc} ==="

        (_OUT / f"{target.name}.shape.txt").write_text(shape, encoding="utf-8")
        print(f"  wrote {target.name}.shape.txt ({len(shape)} chars) + raw.{ext}")

    print(f"\nDone. Dumps in {_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
