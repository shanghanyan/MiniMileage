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
    region_a: Optional[str]
    region_b: Optional[str]
    cabin: str
    miles: int
    roundtrip: bool = False
    updated_at: Optional[str] = None
    # Distance-banded charts (Aeroplan): the great-circle [lo, hi] mile range the
    # row applies to. None for ordinary zone-pair charts (§A.4).
    distance_min: Optional[int] = None
    distance_max: Optional[int] = None
    # Exact per-airport rows (hub-based "destination table" charts — Turkish /
    # KrisFlyer guide pages). When set, this row is keyed on a SPECIFIC O->D
    # airport pair, not a region pair, so the resolver returns the destination's
    # own price instead of collapsing a whole region to one (often wrong) number
    # — e.g. IST->LAX business (100k) differs from IST->ORD (90k). region_a/
    # region_b stay None for these; the resolver matches on the airport pair.
    origin_airport: Optional[str] = None
    dest_airport: Optional[str] = None


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
    # ALL plain text of the page between the END of the previous table and the
    # START of this one (headings, captions, intro paragraphs — everything,
    # tags stripped, whitespace-collapsed). Used by parsers that need a caption
    # OUTSIDE the table to know what it means: a "Routes between X and Y" <p>
    # caption (10xtravel ANA seasonal zone-pair tables).
    preceding_text: str = ""
    # ONLY the text of the single most recent heading tag (h1-h6) seen before
    # this table — NOT accumulated with surrounding paragraphs, so an exact
    # match like "Economy Class" isn't buried inside "...Economy Class The
    # following award chart applies...". Used by 10xtravel's Turkish zone
    # matrix, where the cabin name IS a clean heading immediately above the
    # table (see `_strict_cabin_heading`).
    preceding_heading: str = ""


class _ChartTableParser(HTMLParser):
    """Collect EVERY <table> on the page as (header, rows, preceding text).

    Real pages (e.g. awardtravelfinder.com) carry many tables — navigation,
    FAQ, the chart — so we cannot assume the chart is the first one. The parse
    functions below pick the table whose header actually matches (header-hit =
    selector-hit, the anti-hallucination contract); a page with no matching
    table yields nothing rather than guessing.

    `.header`/`.rows` expose the FIRST table for backward compatibility.
    """

    # Tags whose text content must NOT leak into `preceding_text` (script/style
    # bodies are not prose and would just be noise for the context regexes).
    _SKIP_TEXT_TAGS = {"script", "style"}
    _HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self) -> None:
        super().__init__()
        self._depth = 0          # nesting depth of open <table>s
        self._in_cell = False
        self._row: list[str] = []
        self._cell: list[str] = []
        self._cur_header: list[str] = []
        self._cur_rows: list[list[str]] = []
        self._cur_pretext = ""
        self._cur_preheading = ""
        self.tables: list[_ParsedTable] = []
        # Text seen at depth 0 since the last table closed (candidate context
        # for the NEXT table); reset once consumed at that table's open tag.
        self._pending_text: list[str] = []
        self._skip_text_depth = 0  # >0 while inside a script/style tag
        # The most recently CLOSED heading tag's own text (persists until the
        # next heading overwrites it), and whether we're inside one now.
        self._last_heading = ""
        self._in_heading = False
        self._pending_heading: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self._depth == 0:
                self._cur_header = []
                self._cur_rows = []
                self._row = []
                self._cur_pretext = re.sub(
                    r"\s+", " ", " ".join(self._pending_text)
                ).strip()
                self._cur_preheading = self._last_heading
                self._pending_text = []
            self._depth += 1
        elif self._depth and tag in ("td", "th"):
            self._in_cell = True
            self._cell = []
        elif self._depth == 0 and tag in self._SKIP_TEXT_TAGS:
            self._skip_text_depth += 1
        elif self._depth == 0 and tag in self._HEADING_TAGS:
            self._in_heading = True
            self._pending_heading = []

    def handle_endtag(self, tag):
        if tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0:
                if self._row:  # flush a final unterminated row
                    self._flush_row()
                self.tables.append(
                    _ParsedTable(
                        header=self._cur_header, rows=self._cur_rows,
                        preceding_text=self._cur_pretext,
                        preceding_heading=self._cur_preheading,
                    )
                )
        elif self._depth and tag in ("td", "th"):
            self._in_cell = False
            self._row.append("".join(self._cell).strip())
        elif self._depth and tag == "tr":
            self._flush_row()
        elif self._depth == 0 and tag in self._SKIP_TEXT_TAGS and self._skip_text_depth:
            self._skip_text_depth -= 1
        elif self._depth == 0 and tag in self._HEADING_TAGS and self._in_heading:
            self._in_heading = False
            self._last_heading = re.sub(
                r"\s+", " ", " ".join(self._pending_heading)
            ).strip()

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
        elif self._depth == 0 and not self._skip_text_depth:
            self._pending_text.append(data)
            if self._in_heading:
                self._pending_heading.append(data)

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
# Destination-table parser (hub-based "guide" pages — Turkish, KrisFlyer)
# --------------------------------------------------------------------------- #
# A guide page is NOT a from/to region chart. It is a hub-based, per-destination
# table: origin is IMPLICITLY the program's hub (IST for Turkish, SIN for
# KrisFlyer) and every row is one destination airport with its own price:
#
#   Destination | Code | Economy<tier> | Business<tier> | First<tier> | ...
#   Los Angeles | LAX  | 50,000        | 100,000        | 135,000     | ...
#
# Real pages carry several such tables (one per world region) and mix regions
# within a single table (KrisFlyer's "Asia" table spans South + SE Asia), so we
# consume EVERY destination-shaped table on the page and key each row on its
# exact IATA code — the resolver then answers the queried city precisely rather
# than collapsing a region (where IST->LAX 100k != IST->ORD 90k) to one number.
_IATA_RE = re.compile(r"^[A-Z]{3}$")

# Cabin columns on guide pages carry a tier suffix ("EconomyOff-Peak",
# "BusinessSaver", "First/SuitesSaver"). We match by substring, longest/most-
# specific first so "premium economy" beats "economy" and "first/suites" -> first.
_DEST_CABIN_KEYS: list[tuple[str, str]] = [
    ("premium economy", "premium_economy"),
    ("premiumeconomy", "premium_economy"),
    ("business", "business"),
    ("first", "first"),
    ("suite", "first"),
    ("economy", "economy"),
]


def _dest_cabin(header_cell: str) -> Optional[str]:
    """Map a destination-table header cell to a canonical cabin, or None."""
    h = re.sub(r"[^a-z0-9]+", " ", header_cell.lower()).strip()
    if not h:
        return None
    # 'premium economy' must win over 'economy' even though both substrings hit.
    if "premium" in h and "economy" in h:
        return "premium_economy"
    for needle, cabin in _DEST_CABIN_KEYS:
        if needle in h:
            return cabin
    return None


def _is_destination_table(header: list[str]) -> bool:
    """True for a hub-based per-destination table: a destination/code column
    plus >=1 cabin column, and NOT a from/to wide chart (guarded so a page that
    happens to carry both shapes routes each to its own parser)."""
    hdr = [h.strip().lower() for h in header]
    if "from" in hdr:  # that's a wide region chart — not ours
        return False
    if not any(h in ("code", "destination", "airport") for h in hdr):
        return False
    return any(_dest_cabin(h) for h in hdr)


def parse_chart_destination_table(
    text: str,
    *,
    program: str,
    hub: str,
    updated_at: Optional[str] = None,
    stats: Optional[dict] = None,
) -> list[RawChartRow]:
    """Parse hub-based per-destination "guide" tables into exact-airport rows.

    `hub` is the program's origin airport (IST, SIN, …); every row's origin is
    the hub and its destination is the row's IATA `code`. Each non-null cabin
    cell yields one `RawChartRow` keyed on the exact (hub, code) airport pair
    (region_a/region_b left None). A row without a 3-letter code column, or with
    no parseable cabin cell, is a selector miss and produces nothing — the same
    no-hallucination contract as every other parser.
    """
    parser = _ChartTableParser()
    try:
        parser.feed(text)
    except Exception as exc:
        log.info("destination-table parse error: %s", exc)
        return []

    hub = (hub or "").strip().upper()
    if not _IATA_RE.match(hub):
        log.info("destination-table: invalid/absent hub %r for %s", hub, program)
        return []

    prog = program.strip().lower()
    out: list[RawChartRow] = []
    dropped = 0
    matched_any = False
    for t in parser.tables:
        if not _is_destination_table(t.header):
            continue
        matched_any = True
        header = [h.strip().lower() for h in t.header]
        idx = {name: i for i, name in enumerate(header)}
        code_i = idx.get("code", idx.get("airport"))
        cabin_cols = [
            (i, _dest_cabin(h)) for i, h in enumerate(header) if _dest_cabin(h)
        ]
        for cells in t.rows:
            # Resolve the destination IATA code. Prefer an explicit code column;
            # otherwise scan for a lone 3-letter uppercase token in the row.
            code = None
            if code_i is not None and code_i < len(cells):
                cand = (cells[code_i] or "").strip().upper()
                if _IATA_RE.match(cand):
                    code = cand
            if code is None:
                for c in cells:
                    cand = (c or "").strip().upper()
                    if _IATA_RE.match(cand):
                        code = cand
                        break
            if code is None:
                dropped += 1  # no resolvable destination -> drop + count
                continue
            if code == hub:
                continue  # hub->hub is not a real award
            for col_i, cabin in cabin_cols:
                if col_i >= len(cells):
                    continue
                miles = _wide_miles(cells[col_i] or "")
                if miles is None:
                    continue  # '—'/blank -> selector miss, never guessed
                out.append(
                    RawChartRow(
                        program=prog,
                        region_a=None,
                        region_b=None,
                        cabin=cabin,
                        miles=miles,
                        roundtrip=False,
                        updated_at=updated_at,
                        origin_airport=hub,
                        dest_airport=code,
                    )
                )
    if not matched_any:
        log.info("destination-table: no per-destination table found on page")
    if stats is not None:
        stats["dropped"] = stats.get("dropped", 0) + dropped
    if dropped:
        log.info("destination-table: dropped %d row(s) with no IATA code", dropped)
    return out


# --------------------------------------------------------------------------- #
# PDF parser (official airline award-chart PDFs — e.g. Aeroplan, KrisFlyer)
# --------------------------------------------------------------------------- #
@dataclass
class _PdfTableContext:
    """One pdfplumber table plus the page text around it (cabin hints)."""

    table: _ParsedTable
    page_text: str = ""


def _extract_pdf_tables(data: bytes) -> list[_PdfTableContext]:
    """Pull every table grid out of a PDF via pdfplumber."""
    import io

    out: list[_PdfTableContext] = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            page_text = (page.extract_text() or "").lower()
            for raw_table in page.extract_tables() or []:
                if not raw_table or len(raw_table) < 2:
                    continue
                header = [str(c or "").strip().lower() for c in raw_table[0]]
                body = [
                    [str(c or "").strip() for c in row]
                    for row in raw_table[1:]
                ]
                out.append(
                    _PdfTableContext(
                        table=_ParsedTable(header=header, rows=body),
                        page_text=page_text,
                    )
                )
    return out


def _parse_zone_number(label: str) -> Optional[int]:
    """Pull a program zone index out of a matrix row/column label."""
    if not label:
        return None
    m = re.search(r"zone\s*(\d{1,2})\b", label, re.I)
    if m:
        return int(m.group(1))
    stripped = label.strip()
    if stripped.isdigit():
        return int(stripped)
    nums = re.findall(r"\b(\d{1,2})\b", label)
    if nums and "zone" in label.lower():
        return int(nums[0])
    return None


def _matrix_zone_key(label: str) -> Optional[str | int]:
    """Map a matrix axis label to a zone number or a canonical region token."""
    zone = _parse_zone_number(label)
    if zone is not None:
        return zone
    return canonicalize_region(label)


def _matrix_miles(value: str) -> Optional[int]:
    """Parse a zone-matrix cell — handles thousands ('8.5' -> 8500) and ranges."""
    v = (value or "").strip()
    if not v or v.lower() in {"n/a", "na", "-", "—", "–", "^"}:
        return None
    cleaned = re.sub(r"[^\d.,\-+–—]", "", v).replace(",", "")
    if not re.search(r"\d", cleaned):
        return None
    m = re.match(r"^(\d+(?:\.\d+)?)", cleaned)
    if not m:
        return None
    num = float(m.group(1))
    # Official PDFs (KrisFlyer, …) quote miles in thousands.
    if num < 500:
        num *= 1000
    return int(num)


def _detect_matrix_cabin(*texts: str) -> Optional[str]:
    """Best-effort single cabin from page/section text (simple matrices)."""
    joined = " ".join(t for t in texts if t).lower()
    if not joined:
        return None
    if re.search(r"premium\s+economy", joined):
        return "premium_economy"
    if re.search(r"\bbusiness\b", joined):
        return "business"
    if re.search(r"first|suites", joined):
        return "first"
    if re.search(r"\beconomy\b", joined):
        return "economy"
    return None


_SAVER_MATRIX_CABINS = ["economy", "premium_economy", "business", "first"]
_ADVANTAGE_MATRIX_CABINS = ["economy", "business", "first"]


def _matrix_block_cabins(page_text: str) -> list[str]:
    """Cabin order for multi-row zone matrices (KrisFlyer official PDFs).

    Saver chart pages carry four rows per origin zone (eco / prem / biz /
    first); Advantage-only pages carry three (no premium economy — the PDF
    text says to refer to the Saver chart for premium).
    """
    t = page_text.lower()
    has_saver = "saver award" in t
    has_advantage = "advantage award" in t
    if has_saver:
        return list(_SAVER_MATRIX_CABINS)
    if has_advantage:
        return list(_ADVANTAGE_MATRIX_CABINS)
    return []


def _zone_matrix_column_keys(header: list[str]) -> Optional[list[str | int]]:
    """Return parsed zone keys for columns 1..N, or None if not a matrix."""
    if len(header) < 3:
        return None
    keys: list[str | int] = []
    for cell in header[1:]:
        key = _matrix_zone_key(cell)
        if key is None:
            return None
        keys.append(key)
    return keys if len(keys) >= 2 else None


def _is_numeric_zone_matrix(table: _ParsedTable) -> bool:
    """True when column headers are numeric zone indices (KrisFlyer PDFs)."""
    col_keys = _zone_matrix_column_keys(table.header)
    return col_keys is not None and all(isinstance(k, int) for k in col_keys)


def _is_zone_matrix_table(table: _ParsedTable) -> bool:
    """True when a table is a zone×zone matrix (not a wide or destination chart)."""
    hdr = [h.strip().lower() for h in table.header]
    if "from" in hdr or any(c in hdr for c in ("to", "distance")):
        return False
    if _is_destination_table(table.header):
        return False
    col_keys = _zone_matrix_column_keys(table.header)
    if col_keys is None:
        return False
    if _is_numeric_zone_matrix(table):
        return len(table.rows) >= 1
    hits = 0
    for row in table.rows:
        if not row:
            continue
        if _matrix_zone_key(row[0]) is not None:
            hits += 1
    return hits >= 2


def _region_pair_for_matrix_keys(
    program: str, key_a: str | int, key_b: str | int
) -> Optional[tuple[str, str]]:
    """Turn two matrix axis keys into (region_a, region_b) chart tokens."""
    prog = program.strip().lower()

    def _token(key: str | int) -> Optional[str]:
        if isinstance(key, int):
            return f"{prog}_zone_{key}"
        return key

    a, b = _token(key_a), _token(key_b)
    if a is None or b is None:
        return None
    return (a, b)


def _emit_zone_matrix_rows(
    table: _ParsedTable,
    *,
    program: str,
    cabin: Optional[str],
    block_cabins: list[str],
    updated_at: Optional[str],
) -> tuple[list[RawChartRow], int]:
    """Expand one zone×zone matrix table into region-pair chart rows."""
    col_keys = _zone_matrix_column_keys(table.header)
    if col_keys is None:
        return [], 0

    prog = program.strip().lower()
    out: list[RawChartRow] = []
    dropped = 0
    multi_row = len(block_cabins) > 1
    if not multi_row and (cabin is None or cabin not in _CABINS):
        return [], 0

    current_origin: str | int | None = None
    block_idx = 0
    for row in table.rows:
        if len(row) < 2:
            continue
        row_key = _matrix_zone_key(row[0]) if (row[0] or "").strip() else None
        if multi_row:
            if row_key is not None:
                current_origin = row_key
                block_idx = 0
            elif current_origin is None:
                dropped += 1
                continue
        else:
            if row_key is None:
                dropped += 1
                continue
            current_origin = row_key

        if multi_row:
            if block_idx >= len(block_cabins):
                continue
            row_cabin = block_cabins[block_idx]
            block_idx += 1
        else:
            row_cabin = cabin  # type: ignore[assignment]

        for j, col_key in enumerate(col_keys, start=1):
            if j >= len(row):
                continue
            miles = _matrix_miles(row[j])
            if miles is None:
                continue
            pair = _region_pair_for_matrix_keys(prog, current_origin, col_key)
            if pair is None:
                dropped += 1
                continue
            region_a, region_b = pair
            out.append(
                RawChartRow(
                    program=prog,
                    region_a=region_a,
                    region_b=region_b,
                    cabin=row_cabin,
                    miles=miles,
                    roundtrip=False,
                    updated_at=updated_at,
                )
            )
    return out, dropped


# --------------------------------------------------------------------------- #
# HTML zone-matrix parser (10xtravel Turkish: region-NUMBER zone x zone grid,
# one table per cabin, cabin named in a preceding <h3> heading — not a column).
# --------------------------------------------------------------------------- #
# Matches ONLY a clean, standalone "Economy Class" / "Premium Economy Class"
# heading — the FULL heading text, nothing else (see `preceding_heading`,
# which carries just the one h1-h6 tag's own text, not surrounding prose).
# Deliberately a full match (^...$), so "Economy to Business Class"
# (10xtravel's UPGRADE-cost tables — a same-shaped-but-different-meaning
# matrix on the same page) does NOT match: matching it would silently
# mislabel upgrade-fare data as award-chart data.
_CABIN_CLASS_HEADING_RE = re.compile(
    r"^(premium\s+economy|economy|business|first)\s+class$", re.I
)


def _strict_cabin_heading(heading: str) -> Optional[str]:
    """A cabin name ONLY when the heading is EXACTLY "<Cabin> Class".

    Returns None (selector miss) for a blank/missing heading, and for
    anything that isn't that exact two/three-word shape — e.g. "Economy to
    Business Class" (an upgrade/conversion table, not an award chart, despite
    sharing the exact same zone-matrix grid shape).
    """
    norm = re.sub(r"\s+", " ", (heading or "")).strip()
    if not norm:
        return None
    m = _CABIN_CLASS_HEADING_RE.match(norm)
    if not m:
        return None
    word = m.group(1).lower()
    return "premium_economy" if "premium" in word else word


def parse_chart_html_zone_matrix(
    text: str,
    *,
    program: str,
    updated_at: Optional[str] = None,
    stats: Optional[dict] = None,
) -> list[RawChartRow]:
    """Parse an HTML region-number zone x zone matrix, one table per cabin.

    Shape (10xtravel's Turkish Miles&Smiles page after its 2026 redesign):
    a legend table (region number -> name), then ONE zone x zone matrix table
    PER CABIN, each preceded by a clean "<Cabin> Class" heading (§ live-scrape
    debugging 2026-07-06). Reuses the exact same matrix-cell parsing
    (`_is_zone_matrix_table` / `_emit_zone_matrix_rows`) as the PDF zone-matrix
    path (KrisFlyer) — the grid shape is identical; only where the cabin name
    comes from differs (a page heading here vs. a page-text hint in a PDF).
    """
    parser = _ChartTableParser()
    try:
        parser.feed(text)
    except Exception as exc:
        log.info("html zone matrix parse error: %s", exc)
        return []

    out: list[RawChartRow] = []
    dropped = 0
    matched = False
    for table in parser.tables:
        if not _is_zone_matrix_table(table):
            continue
        cabin = _strict_cabin_heading(table.preceding_heading)
        if cabin is None:
            # Either an upgrade/conversion matrix (rejected on purpose) or a
            # matrix-shaped table with no attributable cabin heading — a
            # selector miss, never guessed.
            continue
        matched = True
        rows, row_dropped = _emit_zone_matrix_rows(
            table, program=program, cabin=cabin, block_cabins=[],
            updated_at=updated_at,
        )
        out.extend(rows)
        dropped += row_dropped
    if not matched:
        log.info("html zone matrix: no cabin-attributable zone matrix found")
        return []
    if stats is not None:
        stats["dropped"] = stats.get("dropped", 0) + dropped
        stats["html_route"] = "zone_matrix"
    if dropped:
        log.info("html zone matrix: dropped %d uncanonicalizable row(s)", dropped)
    return out


# --------------------------------------------------------------------------- #
# HTML seasonal zone-pair parser (10xtravel ANA: a small round-trip "season x
# cabin" table per zone pair, the pair named in a preceding caption).
# --------------------------------------------------------------------------- #
_ANA_SEASON_CABIN_MAP = {
    "economy class": "economy",
    "premium economy class": "premium_economy",
    "business class": "business",
    "first class": "first",
}

_ZONE_NUM_RE = re.compile(r"zone\s*(\d{1,2})", re.I)


def _is_seasonal_zone_table(table: "_ParsedTable") -> bool:
    """True for a 'season | <cabin class> | ...' round-trip table."""
    hdr = [h.strip().lower() for h in table.header]
    if not hdr or hdr[0] != "season":
        return False
    return any(h in _ANA_SEASON_CABIN_MAP for h in hdr)


def _zone_pair_from_caption(text: str) -> Optional[tuple[int, int]]:
    """Pull the two zone numbers out of a 'Routes between X (Zone N) and Y
    (Zone M)' caption. Takes the LAST two 'Zone N' mentions in the captured
    context so an intro paragraph naming other zones earlier doesn't win."""
    nums = _ZONE_NUM_RE.findall(text or "")
    if len(nums) < 2:
        return None
    return int(nums[-2]), int(nums[-1])


def _build_zone_region_legend(tables: list["_ParsedTable"]) -> dict[int, str]:
    """Read a 'Zone name | Zone number | Geographic regions included' legend
    table into {zone_number: canonical_region}, via the SAME region-name
    canonicalizer every other chart uses (§A) — no new region vocabulary. A
    zone whose name doesn't canonicalize is simply absent from the map, which
    downstream drops+counts any row that needs it (never guessed)."""
    legend: dict[int, str] = {}
    for table in tables:
        hdr = [h.strip().lower() for h in table.header]
        if not ({"zone name", "zone number"} <= set(hdr)):
            continue
        name_i, num_i = hdr.index("zone name"), hdr.index("zone number")
        for row in table.rows:
            if len(row) <= max(name_i, num_i):
                continue
            m = _ZONE_NUM_RE.search(row[num_i] or "")
            if not m:
                continue
            region = canonicalize_region(row[name_i] or "")
            if region is None:
                continue
            legend.setdefault(int(m.group(1)), region)
    return legend


def parse_chart_html_seasonal_zones(
    text: str,
    *,
    program: str,
    updated_at: Optional[str] = None,
    stats: Optional[dict] = None,
) -> list[RawChartRow]:
    """Parse 10xtravel's ANA-style page: a zone legend + many small per-zone-
    pair round-trip tables (season x cabin), each preceded by a "Routes
    between X (Zone N) and Y (Zone M)" caption (§ live-scrape debugging
    2026-07-06). Scope: the ANA-operated international zone-pair tables only —
    the domestic distance-band tables and partner-operated departure tables
    on the same page use two OTHER shapes and are intentionally left for a
    follow-up rather than guessed at.

    Only the LOW-SEASON row is emitted per cabin (the cheapest/"Saver"-
    equivalent tier, consistent with how every other chart in this codebase
    reports its lowest available price). All rows are flagged `roundtrip=True`
    per the page's own stated convention ("divide the following prices in
    half" for one-way) — `normalize_one_way` converts downstream, same as the
    existing ANA wide-chart source.
    """
    parser = _ChartTableParser()
    try:
        parser.feed(text)
    except Exception as exc:
        log.info("html seasonal zones parse error: %s", exc)
        return []

    legend = _build_zone_region_legend(parser.tables)
    if not legend:
        log.info("html seasonal zones: no zone legend table found")
        return []

    prog = program.strip().lower()
    out: list[RawChartRow] = []
    dropped = 0
    matched = False
    for table in parser.tables:
        if not _is_seasonal_zone_table(table):
            continue
        zone_pair = _zone_pair_from_caption(table.preceding_text)
        if zone_pair is None:
            dropped += 1
            continue
        zone_a, zone_b = zone_pair
        region_a, region_b = legend.get(zone_a), legend.get(zone_b)
        if region_a is None or region_b is None:
            dropped += 1  # zone not in the legend -> drop + count, never guess
            continue
        matched = True

        hdr = [h.strip().lower() for h in table.header]
        cabin_cols = [
            (i, _ANA_SEASON_CABIN_MAP[h]) for i, h in enumerate(hdr)
            if h in _ANA_SEASON_CABIN_MAP
        ]
        for row in table.rows:
            if not row or "low" not in (row[0] or "").strip().lower():
                continue  # only the low-season (cheapest) row
            for col_i, cabin in cabin_cols:
                if col_i >= len(row):
                    continue
                miles = _wide_miles(row[col_i] or "")
                if miles is None:
                    continue
                out.append(
                    RawChartRow(
                        program=prog,
                        region_a=region_a,
                        region_b=region_b,
                        cabin=cabin,
                        miles=miles,
                        roundtrip=True,
                        updated_at=updated_at,
                    )
                )
    if not matched:
        log.info("html seasonal zones: no zone-pair table resolved against the legend")
        return []
    if stats is not None:
        stats["dropped"] = stats.get("dropped", 0) + dropped
        stats["html_route"] = "seasonal_zones"
    if dropped:
        log.info("html seasonal zones: dropped %d row(s)", dropped)
    return out


def _parse_pdf_wide(
    tables: list[_ParsedTable],
    *,
    program: str,
    updated_at: Optional[str],
    stats: Optional[dict],
) -> list[RawChartRow]:
    table = _select_wide_table(tables)
    if table is None:
        return []
    out, dropped = _emit_wide_rows(
        table.header, table.rows, program=program, updated_at=updated_at
    )
    if out is None:
        return []
    if stats is not None:
        stats["dropped"] = stats.get("dropped", 0) + dropped
        stats["pdf_route"] = "wide"
    if dropped:
        log.info("pdf wide: dropped %d uncanonicalizable row(s)", dropped)
    return out


def _parse_pdf_zone_matrix(
    contexts: list[_PdfTableContext],
    *,
    program: str,
    updated_at: Optional[str],
    stats: Optional[dict],
) -> list[RawChartRow]:
    """Parse zone×zone matrix tables (KrisFlyer zones 1–13, LifeMiles regions, …)."""
    out: list[RawChartRow] = []
    dropped = 0
    matched = False
    for ctx in contexts:
        table = ctx.table
        if not _is_zone_matrix_table(table):
            continue
        matched = True
        block_cabins = (
            _matrix_block_cabins(ctx.page_text)
            if _is_numeric_zone_matrix(table)
            else []
        )
        cabin = None if block_cabins else _detect_matrix_cabin(
            ctx.page_text, " ".join(table.header)
        )
        if not block_cabins and cabin is None:
            log.info("pdf zone matrix: table matched but no cabin hint on page")
            continue
        rows, row_dropped = _emit_zone_matrix_rows(
            table,
            program=program,
            cabin=cabin,
            block_cabins=block_cabins,
            updated_at=updated_at,
        )
        out.extend(rows)
        dropped += row_dropped
    if not matched:
        return []
    if stats is not None:
        stats["dropped"] = stats.get("dropped", 0) + dropped
        stats["pdf_route"] = "zone_matrix"
    if dropped:
        log.info("pdf zone matrix: dropped %d uncanonicalizable row(s)", dropped)
    return out


_PAGE_ZONE_RE = re.compile(
    r"((?:Within|Between)\s+[A-Za-z][^\n]{2,80}?)\s+zones?\b",
    re.I,
)


def _pdf_pts_miles(value: str) -> Optional[int]:
    """Parse Aeroplan PDF cells like ``6,000 pts`` or ``Starting at\\n6,000 pts``."""
    if not value or "median" in value.lower():
        return None
    return _to_int(value)


def _aeroplan_pdf_cabin_cols(header: list[str]) -> dict[str, int]:
    """Map canonical cabin -> first column index on an Aeroplan distance PDF table."""
    cols: dict[str, int] = {}
    for i, raw in enumerate(header):
        if not raw:
            continue
        h = raw.strip().lower()
        if "premium" in h and "economy" in h:
            cols.setdefault("premium_economy", i)
        elif h == "economy":
            cols.setdefault("economy", i)
        elif h == "business":
            cols.setdefault("business", i)
        elif h == "first":
            cols.setdefault("first", i)
    return cols


def _is_aeroplan_distance_pdf_table(header: list[str]) -> bool:
    joined = " ".join(h for h in header if h)
    if "distance" not in joined or "operated" not in joined:
        return False
    return bool(_aeroplan_pdf_cabin_cols(header))


def _zone_pair_from_page_text(text: str) -> Optional[tuple[str, str]]:
    m = _PAGE_ZONE_RE.search(text or "")
    if not m:
        return None
    return canonicalize_zone_pair(m.group(1).strip())


def _emit_aeroplan_distance_pdf_rows(
    table: _ParsedTable,
    *,
    zone_pair: tuple[str, str],
    program: str,
    updated_at: Optional[str],
) -> tuple[list[RawChartRow], int]:
    """Parse one Aeroplan official distance-band table (partner fixed rows)."""
    cabin_cols = _aeroplan_pdf_cabin_cols(table.header)
    if not cabin_cols:
        return [], 0
    hdr = table.header
    try:
        op_i = next(i for i, h in enumerate(hdr) if h and "operated" in h)
    except StopIteration:
        return [], 0

    region_a, region_b = zone_pair
    prog = program.strip().lower()
    out: list[RawChartRow] = []
    dropped = 0
    current_band: Optional[tuple[int, int]] = None

    for row in table.rows:
        if len(row) <= op_i:
            continue
        dist_cell = (row[0] or "").strip()
        if dist_cell:
            band = parse_distance_band(dist_cell)
            if band is not None:
                current_band = band
        operated = (row[op_i] or "").lower()
        if "all other partners" not in operated:
            continue
        if current_band is None:
            dropped += 1
            continue
        dist_min, dist_max = current_band
        for cabin, col_i in cabin_cols.items():
            if col_i >= len(row):
                continue
            miles = _pdf_pts_miles(row[col_i] or "")
            if miles is None:
                continue
            out.append(
                RawChartRow(
                    program=prog,
                    region_a=region_a,
                    region_b=region_b,
                    cabin=cabin,
                    miles=miles,
                    roundtrip=False,
                    updated_at=updated_at,
                    distance_min=dist_min,
                    distance_max=dist_max,
                )
            )
    return out, dropped


def _parse_pdf_aeroplan_distance(
    contexts: list[_PdfTableContext],
    *,
    program: str,
    updated_at: Optional[str],
    stats: Optional[dict],
) -> list[RawChartRow]:
    """Parse Aeroplan official PDF distance-band tables (zone title + grid)."""
    out: list[RawChartRow] = []
    dropped = 0
    matched = False
    for ctx in contexts:
        table = ctx.table
        if not _is_aeroplan_distance_pdf_table(table.header):
            continue
        zone_pair = _zone_pair_from_page_text(ctx.page_text)
        if zone_pair is None:
            log.info("pdf aeroplan distance: table without page zone title")
            continue
        matched = True
        rows, row_dropped = _emit_aeroplan_distance_pdf_rows(
            table,
            zone_pair=zone_pair,
            program=program,
            updated_at=updated_at,
        )
        out.extend(rows)
        dropped += row_dropped
    if not matched:
        return []
    if stats is not None:
        stats["dropped"] = stats.get("dropped", 0) + dropped
        stats["pdf_route"] = "aeroplan_distance"
    if dropped:
        log.info("pdf aeroplan distance: dropped %d row(s)", dropped)
    return out


def _parse_pdf_lifemiles_text(
    data: bytes,
    *,
    program: str,
    updated_at: Optional[str] = None,
    stats: Optional[dict] = None,
) -> list[RawChartRow]:
    """Parse the 2022 Thrifty Traveler LifeMiles zone PDF via page text (§6).

    pdfplumber's default table extractor returns nothing for this layout — the
    chart is a lower-triangular region matrix with X/I/O cabin rows per origin.
    """
    import io

    if not _HAS_PDFPLUMBER:
        return []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            text = pdf.pages[0].extract_text() or ""
    except Exception as exc:
        log.info("lifemiles pdf text: %s", exc)
        return []

    _LM_REGIONS = [
        "North of Central America",
        "South of Central America",
        "Hawaii",
        "Rest of North America",
        "United States 1",
        "United States 2",
        "United States 3",
        "Mexico",
        "Carribean",
        "North of South America",
        "South of South America",
        "Brazil",
        "Europe 1",
        "Europe 2",
        "Europe 3",
        "Middle East / North Africa",
        "South Africa",
        "North Asia",
        "Central Asia",
        "South Asia",
    ]
    _LM_CANON = {
        "north of central america": "north_america",
        "south of central america": "north_america",
        "hawaii": "north_america",
        "rest of north america": "north_america",
        "united states 1": "north_america",
        "united states 2": "north_america",
        "united states 3": "north_america",
        "mexico": "north_america",
        "carribean": "north_america",
        "caribbean": "north_america",
        "north of south america": "south_america",
        "south of south america": "south_america",
        "brazil": "south_america",
        "europe 1": "europe",
        "europe 2": "europe",
        "europe 3": "europe",
        "middle east / north africa": "middle_east",
        "south africa": "africa",
        "north asia": "north_asia",
        "central asia": "south_asia",
        "south asia": "south_asia",
    }
    _LM_CABIN = {"X": "economy", "I": "economy", "O": "business"}
    _CABIN_RE = re.compile(r"^([XIO])\s+((?:\d{1,3}(?:,\d{3})*\s*)+)$")
    _REGION_CABIN_RE = re.compile(
        r"^(.+?)\s+([XIO])\s+((?:\d{1,3}(?:,\d{3})*\s*)+)$"
    )
    _MILES_RE = re.compile(r"\d{1,3}(?:,\d{3})*")
    _SKIP_PREFIX = (
        "Star Alliance", "In order", "Find the", "From/to", "Pinpoint",
        "Below the", "El Salvador)",
    )

    def _canon(name: str) -> Optional[str]:
        return _LM_CANON.get(name.lower().strip())

    def _emit(origin: str, cabin_code: str, values: list[int]) -> None:
        cab = _LM_CABIN.get(cabin_code)
        if cab is None or origin not in _LM_REGIONS:
            return
        ra = _canon(origin)
        if ra is None:
            return
        oi = _LM_REGIONS.index(origin)
        for j, miles in enumerate(values):
            di = oi + j
            if di >= len(_LM_REGIONS):
                break
            rb = _canon(_LM_REGIONS[di])
            if rb is None:
                continue
            out.append(
                RawChartRow(
                    program=program,
                    region_a=ra,
                    region_b=rb,
                    cabin=cab,
                    miles=miles,
                    roundtrip=False,
                    updated_at=updated_at,
                )
            )

    out: list[RawChartRow] = []
    current_origin: Optional[str] = None
    for line in text.split("\n"):
        line = line.strip()
        if not line or any(line.startswith(p) for p in _SKIP_PREFIX):
            continue
        if line.startswith("("):
            continue
        m = _REGION_CABIN_RE.match(line)
        if m:
            current_origin = m.group(1).strip()
            vals = [int(x.replace(",", "")) for x in _MILES_RE.findall(m.group(3))]
            _emit(current_origin, m.group(2), vals)
            continue
        m = _CABIN_RE.match(line)
        if m and current_origin:
            vals = [int(x.replace(",", "")) for x in _MILES_RE.findall(m.group(2))]
            _emit(current_origin, m.group(1), vals)
            continue
        if not re.search(r"\d", line) and len(line) > 2 and line not in {"Oceania"}:
            current_origin = line.strip()

    if stats is not None and out:
        stats["pdf_route"] = "lifemiles_text"
    return out


def parse_chart_pdf(
    data: Optional[bytes],
    *,
    program: str,
    updated_at: Optional[str] = None,
    stats: Optional[dict] = None,
) -> list[RawChartRow]:
    """Route PDF bytes through wide-table, Aeroplan-distance, then zone-matrix parsers (§6).

    Official airline PDFs arrive in three shapes:

      - **Wide table** (ATF-style HTML mirrors): ``from | distance/to | cabins…``
      - **Aeroplan distance PDF**: page title names the zone pair; table rows
        are distance bands × ``All other partners`` fixed partner pricing
      - **Zone matrix** (KrisFlyer zones 1–13, LifeMiles region grids)
        row/column headers name zones; each cell is the one-way miles for that
        pair. Numeric zone indices become ``{program}_zone_{n}`` region tokens;
        named headers (``North America``, ``Europe``, …) canonicalize through
        ``canonicalize_region``.

    Degrades gracefully: returns ``[]`` (never raises) when pdfplumber is not
    installed or neither parser shape matches.
    """
    if not _HAS_PDFPLUMBER:
        log.info("pdf chart: pdfplumber not installed; skipping (pip install pdfplumber)")
        return []
    if not data:
        log.info("pdf chart: no bytes to parse")
        return []

    try:
        contexts = _extract_pdf_tables(data)
    except Exception as exc:
        log.info("pdf parse error: %s", exc)
        return []

    if not contexts:
        log.info("pdf chart: no tables extracted from PDF")
        if program == "lifemiles":
            return _parse_pdf_lifemiles_text(
                data, program=program, updated_at=updated_at, stats=stats
            )
        return []

    tables = [ctx.table for ctx in contexts]

    out = _parse_pdf_wide(
        tables, program=program, updated_at=updated_at, stats=stats
    )
    if out:
        return out

    out = _parse_pdf_aeroplan_distance(
        contexts, program=program, updated_at=updated_at, stats=stats
    )
    if out:
        return out

    out = _parse_pdf_zone_matrix(
        contexts, program=program, updated_at=updated_at, stats=stats
    )
    if out:
        return out

    if program == "lifemiles":
        return _parse_pdf_lifemiles_text(
            data, program=program, updated_at=updated_at, stats=stats
        )

    log.info("pdf chart: no wide, aeroplan-distance, or zone-matrix chart found")
    return []


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
