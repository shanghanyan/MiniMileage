"""Aggregator parsers — bytes -> normalized rows (§6).

Each parser turns one fetched document into a list of canonical rows. Two row
shapes exist, mirroring the two layers the aggregator serves:

  - `RawChartRow`  (Layer 4): a region-pair award-chart cost (no live seat).
  - `RawAwardRow`  (Layer 3): a specific O->D award with live seat availability.

A row is only produced when a *selector actually hits* — a parsed table cell, a
JSON field that existed, an RSS item that matched. Nothing is inferred. That is
the anti-hallucination contract (§2.1): no selector hit, no datum.

Formats supported with the standard library + httpx only (the demo runs with no
extra installs). `pdfplumber` / `feedparser` are optional accelerators; their
absence degrades gracefully to the stdlib paths.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional
from xml.etree import ElementTree

from .regions import (
    canonicalize_region,
    canonicalize_zone_pair,
    parse_distance_band,
)

log = logging.getLogger("mileage.aggregator.parse")

# pdfplumber is an OPTIONAL accelerator (binary PDF table extraction). Absence
# degrades gracefully: PDF targets parse to nothing instead of crashing, exactly
# as before, and a one-line install (`pip install pdfplumber`) lights them up.
try:  # pragma: no cover - exercised only when the extra is installed
    import pdfplumber  # type: ignore

    _HAS_PDFPLUMBER = True
except Exception:  # pragma: no cover
    pdfplumber = None  # type: ignore
    _HAS_PDFPLUMBER = False

_CABINS = {"economy", "premium_economy", "business", "first"}

# Maps wide-table column headers to canonical cabin names.
_WIDE_CABIN_MAP: dict[str, str] = {
    "economy": "economy",
    "premium economy": "premium_economy",
    "premium_economy": "premium_economy",
    "premium": "premium_economy",
    "business": "business",
    "first": "first",
    "first class": "first",
}


@dataclass
class RawChartRow:
    program: str
    region_a: str
    region_b: str
    cabin: str
    miles: int
    roundtrip: bool = False
    updated_at: Optional[str] = None
    # Distance-banded charts (Aeroplan): the great-circle [lo, hi] mile range the
    # row applies to. None for ordinary zone-pair charts (§A.4).
    distance_min: Optional[int] = None
    distance_max: Optional[int] = None


@dataclass
class RawAwardRow:
    program: str
    origin: str
    dest: str
    cabin: str
    miles: int
    seats: Optional[int] = None
    roundtrip: bool = False
    updated_at: Optional[str] = None


def normalize_one_way(miles: int, roundtrip: bool) -> tuple[int, list[str]]:
    """Round-trip charts (e.g. ANA) -> one-way, flagged (§6 carried-over fix)."""
    if roundtrip:
        return math.ceil(miles / 2), ["rt_to_ow_normalized"]
    return miles, []


def _to_int(value: str) -> Optional[int]:
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def _wide_miles(value: str) -> Optional[int]:
    """Parse a wide-format miles cell.

    Handles:
    - Plain integers with commas: "25,000" -> 25000
    - Ranges (lower bound = saver rate): "35,000-45,000" -> 35000
    - Unavailable markers ('—', '–', '', 'N/A', …) -> None
    """
    v = value.strip().replace(",", "")
    if not re.search(r"\d", v):
        return None  # no digits -> unavailable marker (em-dash, en-dash, etc.)
    # Range like "35000-45000" or "35000–45000" -> lower bound
    m = re.match(r"^(\d+)\s*[-–—]\s*\d+", v)
    if m:
        return int(m.group(1))
    digits = re.sub(r"[^0-9]", "", v)
    return int(digits) if digits else None


def _to_bool(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "rt", "roundtrip"}


# --------------------------------------------------------------------------- #
# HTML table parser (aggregators / blogs publishing an award chart)
# --------------------------------------------------------------------------- #
@dataclass
class _ParsedTable:
    header: list[str]
    rows: list[list[str]]


class _ChartTableParser(HTMLParser):
    """Collect EVERY <table> on the page as (header, rows).

    Real pages (e.g. awardtravelfinder.com) carry many tables — navigation,
    FAQ, the chart — so we cannot assume the chart is the first one. The parse
    functions below pick the table whose header actually matches (header-hit =
    selector-hit, the anti-hallucination contract); a page with no matching
    table yields nothing rather than guessing.

    `.header`/`.rows` expose the FIRST table for backward compatibility.
    """

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0          # nesting depth of open <table>s
        self._in_cell = False
        self._row: list[str] = []
        self._cell: list[str] = []
        self._cur_header: list[str] = []
        self._cur_rows: list[list[str]] = []
        self.tables: list[_ParsedTable] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self._depth == 0:
                self._cur_header = []
                self._cur_rows = []
                self._row = []
            self._depth += 1
        elif self._depth and tag in ("td", "th"):
            self._in_cell = True
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                if self._row:  # flush a final unterminated row
                    self._flush_row()
                self.tables.append(
                    _ParsedTable(header=self._cur_header, rows=self._cur_rows)
                )
        elif self._depth and tag in ("td", "th"):
            self._in_cell = False
            self._row.append("".join(self._cell).strip())
        elif self._depth and tag == "tr":
            self._flush_row()

    def _flush_row(self) -> None:
        if self._row:
            if not self._cur_header:
                self._cur_header = [c.lower() for c in self._row]
            else:
                self._cur_rows.append(self._row)
        self._row = []

    def handle_data(self, data):
        if self._in_cell:
            self._cell.append(data)

    # Backward-compatible single-table view (first table on the page).
    @property
    def header(self) -> list[str]:
        return self.tables[0].header if self.tables else []

    @property
    def rows(self) -> list[list[str]]:
        return self.tables[0].rows if self.tables else []


def _select_table(
    tables: list["_ParsedTable"], required: tuple[str, ...]
) -> Optional["_ParsedTable"]:
    """First table whose header contains every `required` column (selector-hit)."""
    for t in tables:
        if t.header and all(k in t.header for k in required):
            return t
    return None


def _select_wide_table(tables: list["_ParsedTable"]) -> Optional["_ParsedTable"]:
    """First table that looks like a wide award chart: from + to/distance + cabin."""
    for t in tables:
        hdr = [h.strip().lower() for h in t.header]
        if "from" not in hdr:
            continue
        if not any(c in hdr for c in ("to", "distance")):
            continue
        if not any(_WIDE_CABIN_MAP.get(h) for h in hdr):
            continue
        return t
    return None


def parse_chart_html(
    text: str,
    *,
    updated_at: Optional[str] = None,
    stats: Optional[dict] = None,
) -> list[RawChartRow]:
    parser = _ChartTableParser()
    try:
        parser.feed(text)
    except Exception as exc:
        log.info("html parse error: %s", exc)
        return []
    required = ("program", "from", "to", "cabin", "miles")
    table = _select_table(parser.tables, required)
    if table is None:
        log.info("no long-format chart table found")
        return []
    idx = {name: i for i, name in enumerate(table.header)}

    out: list[RawChartRow] = []
    dropped = 0
    for cells in table.rows:
        if len(cells) < len(required):
            continue
        miles = _to_int(cells[idx["miles"]])
        cabin = cells[idx["cabin"]].strip().lower()
        if miles is None or cabin not in _CABINS:
            continue  # selector miss -> drop, never guess
        region_a = canonicalize_region(cells[idx["from"]])
        region_b = canonicalize_region(cells[idx["to"]])
        if region_a is None or region_b is None:
            dropped += 1  # unmappable zone -> drop + count, never guess (§A)
            continue
        rt = _to_bool(cells[idx["roundtrip"]]) if "roundtrip" in idx else False
        out.append(
            RawChartRow(
                program=cells[idx["program"]].strip().lower(),
                region_a=region_a,
                region_b=region_b,
                cabin=cabin,
                miles=miles,
                roundtrip=rt,
                updated_at=updated_at,
            )
        )
    if stats is not None:
        stats["dropped"] = stats.get("dropped", 0) + dropped
    if dropped:
        log.info("html chart: dropped %d row(s) with uncanonicalizable region", dropped)
    return out


def parse_chart_html_wide(
    text: str,
    *,
    program: str,
    updated_at: Optional[str] = None,
    stats: Optional[dict] = None,
) -> list[RawChartRow]:
    """Parse a 'wide' award chart HTML table.

    Row format (order-independent, case-insensitive):
        from  |  to  (or distance)  |  economy  |  [premium]  |  business  |  first  |  [note]

    Each row produces one RawChartRow per non-null cabin cell.
    Range values like '35,000–45,000' use the lower bound (saver tier).
    Empty / '—' cells are skipped — selector-miss contract preserved.

    Used by awardtravelfinder.com chart pages (Aeroplan, LifeMiles, …).
    """
    parser = _ChartTableParser()
    try:
        parser.feed(text)
    except Exception as exc:
        log.info("html_wide parse error: %s", exc)
        return []

    table = _select_wide_table(parser.tables)
    if table is None:
        log.info("html_wide: no wide award-chart table found on page")
        return []

    out, dropped = _emit_wide_rows(
        table.header, table.rows, program=program, updated_at=updated_at
    )
    if out is None:  # no usable to/distance column
        log.info("html_wide: no 'to'/'distance' column on the chart table")
        return []
    if stats is not None:
        stats["dropped"] = stats.get("dropped", 0) + dropped
    if dropped:
        log.info("html_wide chart: dropped %d uncanonicalizable row(s)", dropped)
    return out


def _emit_wide_rows(
    raw_header: list[str],
    body_rows: list[list[str]],
    *,
    program: str,
    updated_at: Optional[str],
) -> tuple[Optional[list[RawChartRow]], int]:
    """Turn one wide award-chart table (header + rows) into RawChartRows.

    Shared by the HTML (`parse_chart_html_wide`) and PDF (`parse_chart_pdf`)
    paths so both honor the SAME anti-hallucination contract: a cell only
    becomes a datum when `from` + `to`/`distance` + a cabin column all hit, and
    an uncanonicalizable zone is dropped + counted, never guessed (§A).

    Returns `(rows, dropped)`, or `(None, 0)` when the table lacks a usable
    `to`/`distance` column (signals the caller it was not a real chart table).
    """
    header = [h.strip().lower() for h in raw_header]
    idx = {name: i for i, name in enumerate(header)}

    # Accept 'to' (LifeMiles zone pairs) or 'distance' (Aeroplan distance bands)
    zone_col = next((c for c in ("to", "distance") if c in idx), None)
    if zone_col is None or "from" not in idx:
        return None, 0
    is_distance = zone_col == "distance"

    # Collect cabin columns in header order
    cabin_cols: list[tuple[str, str]] = []  # (header_key, canonical_cabin)
    for label in header:
        canonical = _WIDE_CABIN_MAP.get(label)
        if canonical:
            cabin_cols.append((label, canonical))

    out: list[RawChartRow] = []
    prog = program.strip().lower()
    dropped = 0
    for cells in body_rows:
        if len(cells) <= max(idx["from"], idx[zone_col]):
            continue
        raw_a = (cells[idx["from"]] or "").strip()
        raw_b = (cells[idx[zone_col]] or "").strip()
        if not raw_a or not raw_b:
            continue

        # Canonicalize the geography (§A). Two shapes:
        #   - distance charts: `from` names a zone PAIR, `distance` is the band.
        #   - zone-pair charts: `from` and `to` are each one zone.
        dist_min: Optional[int] = None
        dist_max: Optional[int] = None
        if is_distance:
            pair = canonicalize_zone_pair(raw_a)
            band = parse_distance_band(raw_b)
            if pair is None or band is None:
                dropped += 1
                continue
            region_a, region_b = pair
            dist_min, dist_max = band
        else:
            region_a = canonicalize_region(raw_a)
            region_b = canonicalize_region(raw_b)
            if region_a is None or region_b is None:
                dropped += 1  # unmappable zone -> drop + count, never guess (§A)
                continue

        for label, canonical in cabin_cols:
            col_i = idx[label]
            if col_i >= len(cells):
                continue
            miles = _wide_miles(cells[col_i] or "")
            if miles is None:
                continue  # selector miss -> drop, never guess
            out.append(
                RawChartRow(
                    program=prog,
                    region_a=region_a,
                    region_b=region_b,
                    cabin=canonical,
                    miles=miles,
                    roundtrip=False,
                    updated_at=updated_at,
                    distance_min=dist_min,
                    distance_max=dist_max,
                )
            )
    return out, dropped


# --------------------------------------------------------------------------- #
# PDF parser (official airline award-chart PDFs — e.g. Aeroplan, KrisFlyer)
# --------------------------------------------------------------------------- #
def parse_chart_pdf(
    data: Optional[bytes],
    *,
    program: str,
    updated_at: Optional[str] = None,
    stats: Optional[dict] = None,
) -> list[RawChartRow]:
    """Extract wide award-chart rows from a PDF's tables (§6).

    Uses `pdfplumber` to pull the laid-out tables out of an official chart PDF
    (the highest-trust Aeroplan + KrisFlyer sources), then runs each table
    through the SAME wide-chart row logic as the HTML path (`_emit_wide_rows`),
    so geography canonicalization and the anti-hallucination contract are
    identical regardless of input format.

    Degrades gracefully: returns `[]` (never raises) when pdfplumber is not
    installed or no chart-shaped table is found — the source simply produces no
    data, exactly as before.
    """
    if not _HAS_PDFPLUMBER:
        log.info("pdf chart: pdfplumber not installed; skipping (pip install pdfplumber)")
        return []
    if not data:
        log.info("pdf chart: no bytes to parse")
        return []

    import io

    tables: list[_ParsedTable] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                for raw_table in page.extract_tables() or []:
                    if not raw_table or len(raw_table) < 2:
                        continue
                    header = [str(c or "").strip().lower() for c in raw_table[0]]
                    body = [
                        [str(c or "").strip() for c in row]
                        for row in raw_table[1:]
                    ]
                    tables.append(_ParsedTable(header=header, rows=body))
    except Exception as exc:
        log.info("pdf parse error: %s", exc)
        return []

    table = _select_wide_table(tables)
    if table is None:
        log.info("pdf chart: no wide award-chart table found in PDF")
        return []

    out, dropped = _emit_wide_rows(
        table.header, table.rows, program=program, updated_at=updated_at
    )
    if out is None:
        return []
    if stats is not None:
        stats["dropped"] = stats.get("dropped", 0) + dropped
    if dropped:
        log.info("pdf chart: dropped %d uncanonicalizable row(s)", dropped)
    return out


# --------------------------------------------------------------------------- #
# JSON parsers (award-space tools / aggregator JSON APIs)
# --------------------------------------------------------------------------- #
def _load_json(text: str):
    try:
        return json.loads(text)
    except Exception as exc:
        log.info("json parse error: %s", exc)
        return None


def parse_award_json(text: str) -> list[RawAwardRow]:
    """Parse a list of live award records: [{program,origin,dest,cabin,miles,seats}]."""
    data = _load_json(text)
    records = data.get("availability") if isinstance(data, dict) else data
    if not isinstance(records, list):
        return []
    out: list[RawAwardRow] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        miles = _to_int(r.get("miles", ""))
        cabin = str(r.get("cabin", "")).strip().lower()
        origin = str(r.get("origin", "")).strip().upper()
        dest = str(r.get("dest", "")).strip().upper()
        program = str(r.get("program", "")).strip().lower()
        if not (program and origin and dest and miles and cabin in _CABINS):
            continue
        seats = r.get("seats")
        out.append(
            RawAwardRow(
                program=program,
                origin=origin,
                dest=dest,
                cabin=cabin,
                miles=miles,
                seats=int(seats) if isinstance(seats, (int, float)) else None,
                roundtrip=_to_bool(r.get("roundtrip", False)),
                updated_at=r.get("updated_at") or r.get("last_seen"),
            )
        )
    return out


def parse_chart_json(text: str, *, stats: Optional[dict] = None) -> list[RawChartRow]:
    data = _load_json(text)
    records = data.get("charts") if isinstance(data, dict) else data
    if not isinstance(records, list):
        return []
    out: list[RawChartRow] = []
    dropped = 0
    for r in records:
        if not isinstance(r, dict):
            continue
        miles = _to_int(r.get("miles", ""))
        cabin = str(r.get("cabin", "")).strip().lower()
        if miles is None or cabin not in _CABINS:
            continue
        region_a = canonicalize_region(str(r.get("from", "")))
        region_b = canonicalize_region(str(r.get("to", "")))
        if region_a is None or region_b is None:
            dropped += 1  # unmappable zone -> drop + count, never guess (§A)
            continue
        out.append(
            RawChartRow(
                program=str(r.get("program", "")).strip().lower(),
                region_a=region_a,
                region_b=region_b,
                cabin=cabin,
                miles=miles,
                roundtrip=_to_bool(r.get("roundtrip", False)),
                updated_at=r.get("updated_at"),
            )
        )
    if stats is not None:
        stats["dropped"] = stats.get("dropped", 0) + dropped
    return out


# --------------------------------------------------------------------------- #
# RSS parser (award feeds publishing chart/award updates)
# --------------------------------------------------------------------------- #
# Each item embeds a structured payload in a <mileage:data> JSON blob so a real
# feed reader and our parser agree on the datum (no prose scraping).
def parse_rss(text: str) -> tuple[list[RawChartRow], list[RawAwardRow]]:
    charts: list[RawChartRow] = []
    awards: list[RawAwardRow] = []
    try:
        root = ElementTree.fromstring(text)
    except Exception as exc:
        log.info("rss parse error: %s", exc)
        return charts, awards

    for item in root.iter("item"):
        pub = item.findtext("pubDate")
        payload_el = None
        for child in item:
            if child.tag.endswith("data"):  # namespaced <mileage:data>
                payload_el = child
                break
        if payload_el is None or not (payload_el.text or "").strip():
            continue
        payload = _load_json(payload_el.text)
        if not isinstance(payload, dict):
            continue
        kind = payload.get("kind")
        if kind == "award":
            awards.extend(parse_award_json(json.dumps([payload])))
            for a in awards:
                a.updated_at = a.updated_at or pub
        elif kind == "chart":
            rows = parse_chart_json(json.dumps([payload]))
            for c in rows:
                c.updated_at = c.updated_at or pub
            charts.extend(rows)
    return charts, awards
