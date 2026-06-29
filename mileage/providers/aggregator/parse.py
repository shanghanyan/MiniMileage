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

log = logging.getLogger("mileage.aggregator.parse")

_CABINS = {"economy", "premium_economy", "business", "first"}


@dataclass
class RawChartRow:
    program: str
    region_a: str
    region_b: str
    cabin: str
    miles: int
    roundtrip: bool = False
    updated_at: Optional[str] = None


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


def _to_bool(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "rt", "roundtrip"}


# --------------------------------------------------------------------------- #
# HTML table parser (aggregators / blogs publishing an award chart)
# --------------------------------------------------------------------------- #
class _ChartTableParser(HTMLParser):
    """Extract rows from the first <table> carrying the expected headers.

    Expected header cells (case-insensitive, order-independent):
        program | from | to | cabin | miles | roundtrip(optional)
    """

    def __init__(self) -> None:
        super().__init__()
        self._in_table = False
        self._in_cell = False
        self._row: list[str] = []
        self._cell: list[str] = []
        self.header: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "table" and not self.rows and not self.header:
            self._in_table = True
        elif self._in_table and tag in ("td", "th"):
            self._in_cell = True
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table":
            self._in_table = False
        elif self._in_table and tag in ("td", "th"):
            self._in_cell = False
            self._row.append("".join(self._cell).strip())
        elif self._in_table and tag == "tr":
            if self._row:
                if not self.header:
                    self.header = [c.lower() for c in self._row]
                else:
                    self.rows.append(self._row)
            self._row = []

    def handle_data(self, data):
        if self._in_cell:
            self._cell.append(data)


def parse_chart_html(text: str, *, updated_at: Optional[str] = None) -> list[RawChartRow]:
    parser = _ChartTableParser()
    try:
        parser.feed(text)
    except Exception as exc:
        log.info("html parse error: %s", exc)
        return []
    if not parser.header:
        return []
    idx = {name: i for i, name in enumerate(parser.header)}
    required = ("program", "from", "to", "cabin", "miles")
    if not all(k in idx for k in required):
        log.info("html table missing required headers: %s", parser.header)
        return []

    out: list[RawChartRow] = []
    for cells in parser.rows:
        if len(cells) < len(required):
            continue
        miles = _to_int(cells[idx["miles"]])
        cabin = cells[idx["cabin"]].strip().lower()
        if miles is None or cabin not in _CABINS:
            continue  # selector miss -> drop, never guess
        rt = _to_bool(cells[idx["roundtrip"]]) if "roundtrip" in idx else False
        out.append(
            RawChartRow(
                program=cells[idx["program"]].strip().lower(),
                region_a=cells[idx["from"]].strip().lower(),
                region_b=cells[idx["to"]].strip().lower(),
                cabin=cabin,
                miles=miles,
                roundtrip=rt,
                updated_at=updated_at,
            )
        )
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


def parse_chart_json(text: str) -> list[RawChartRow]:
    data = _load_json(text)
    records = data.get("charts") if isinstance(data, dict) else data
    if not isinstance(records, list):
        return []
    out: list[RawChartRow] = []
    for r in records:
        if not isinstance(r, dict):
            continue
        miles = _to_int(r.get("miles", ""))
        cabin = str(r.get("cabin", "")).strip().lower()
        if miles is None or cabin not in _CABINS:
            continue
        out.append(
            RawChartRow(
                program=str(r.get("program", "")).strip().lower(),
                region_a=str(r.get("from", "")).strip().lower(),
                region_b=str(r.get("to", "")).strip().lower(),
                cabin=cabin,
                miles=miles,
                roundtrip=_to_bool(r.get("roundtrip", False)),
                updated_at=r.get("updated_at"),
            )
        )
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
