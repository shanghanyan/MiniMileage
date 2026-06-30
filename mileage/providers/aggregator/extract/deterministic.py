"""Deterministic, keyless extractor (§6.2 default backend).

No model, no cloud key, no download — so discovery runs and is testable offline,
and its output is byte-for-byte reproducible (which is what lets the extraction
eval be a deterministic CI gate instead of a vibe).

It reads a document (email body, blog article, transcript — HTML or plain text)
and emits `RawChartRow`s by matching co-occurring, *literally present* signals
in a sentence: a loyalty PROGRAM, a CABIN, a REGION pair, and a MILES number.
Every emitted number passes the verbatim-grounding guard (`grounding.py`); a
sentence missing any signal yields nothing (we omit rather than guess).

The same `LLMExtractor.extract(document, source_hint=...)` signature a local
Qwen backend would implement — so swapping the backend changes accuracy, not
the contract or the safety guarantees.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import List, Optional

from ..parse import RawChartRow, _CABINS
from .grounding import number_is_grounded

# Program name / brand -> canonical program id (matches knowledge/ratios.yaml).
_PROGRAMS: dict[str, str] = {
    "turkish": "turkish",
    "miles&smiles": "turkish",
    "miles and smiles": "turkish",
    "aeroplan": "aeroplan",
    "air canada": "aeroplan",
    "lifemiles": "lifemiles",
    "avianca": "lifemiles",
    "ana": "ana",
    "ana mileage": "ana",
    "krisflyer": "krisflyer",
    "singapore airlines": "krisflyer",
    "singapore krisflyer": "krisflyer",
}

# Region word / city / country -> canonical region token (knowledge/charts.yaml
# region_map values). Cities/countries fold to their region so prose resolves.
_REGIONS: dict[str, str] = {
    "north america": "north_america",
    "united states": "north_america",
    "the us": "north_america",
    "u.s.": "north_america",
    "usa": "north_america",
    "us ": "north_america",
    "canada": "north_america",
    "new york": "north_america",
    "los angeles": "north_america",
    "europe": "europe",
    "european": "europe",
    "istanbul": "europe",
    "turkey": "europe",
    "london": "europe",
    "paris": "europe",
    "frankfurt": "europe",
    "north asia": "north_asia",
    "asia": "north_asia",
    "japan": "north_asia",
    "tokyo": "north_asia",
    "seoul": "north_asia",
}

# Cabin phrasing -> canonical cabin (a subset of parse._CABINS).
_CABIN_WORDS: list[tuple[str, str]] = [
    ("premium economy", "premium_economy"),
    ("business class", "business"),
    ("business", "business"),
    ("first class", "first"),
    ("first", "first"),
    ("economy", "economy"),
    ("coach", "economy"),
]

# A miles/points figure: plausible award range, attached to a points/miles/for
# context so we don't grab "100,000-point sign-up bonus" or "$4,150" noise.
_MILES_CTX = re.compile(
    r"(?:(?:for|costs?|priced at|just|only|=|:)\s*)?"
    r"(\d{1,3}(?:[,\s]\d{3})+|\d{4,6})"
    r"(?:\s*(?:miles|miles\.|points|pts|k\b))?",
    re.IGNORECASE,
)
_MILES_NEAR_KEYWORD = re.compile(
    r"(\d{1,3}(?:[,\s]\d{3})+|\d{4,6})\s*(?:miles|points|pts)\b", re.IGNORECASE
)
_MIN_MILES, _MAX_MILES = 3_000, 400_000


class _TextExtractor(HTMLParser):
    """Strip tags -> visible text (skipping <script>/<style>)."""

    def __init__(self) -> None:
        super().__init__()
        self._skip = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip and data.strip():
            self._chunks.append(data)

    def text(self) -> str:
        return " ".join(self._chunks)


def html_to_text(document: str) -> str:
    if "<" not in document or ">" not in document:
        return document
    p = _TextExtractor()
    try:
        p.feed(document)
    except Exception:
        return re.sub(r"<[^>]+>", " ", document)
    return p.text()


def _find_program(sentence: str) -> Optional[str]:
    low = sentence.lower()
    best: Optional[tuple[int, str]] = None
    for phrase, canonical in _PROGRAMS.items():
        pos = low.find(phrase)
        if pos >= 0 and (best is None or pos < best[0]):
            best = (pos, canonical)
    return best[1] if best else None


def _nearest_program_start(text: str, number_pos: int, max_back: int) -> Optional[int]:
    """Start index of the program mention closest *before* `number_pos`.

    Anchoring the claim to its own program mention (rather than a fixed-width
    window) is what stops one list item's program/region bleeding into the
    next. Falls back to the nearest mention just after the number.
    """
    low = text.lower()
    before: Optional[int] = None
    after: Optional[int] = None
    for phrase in _PROGRAMS:
        start = 0
        while True:
            pos = low.find(phrase, start)
            if pos < 0:
                break
            if pos <= number_pos and number_pos - pos <= max_back:
                if before is None or pos > before:
                    before = pos
            elif pos > number_pos and pos - number_pos <= 40:
                if after is None or pos < after:
                    after = pos
            start = pos + len(phrase)
    return before if before is not None else after


# Number is a sign-up/welcome bonus, not an award price — never a chart row.
_BONUS_CTX = re.compile(
    r"earn|bonus|welcome offer|sign[\s-]?up|intro|after (?:you )?spend|"
    r"statement credit|points back",
    re.IGNORECASE,
)


def _find_regions(sentence: str) -> list[str]:
    low = sentence.lower()
    hits: list[tuple[int, str]] = []
    for phrase, canonical in _REGIONS.items():
        pos = low.find(phrase)
        if pos >= 0:
            hits.append((pos, canonical))
    hits.sort()
    ordered: list[str] = []
    for _, region in hits:
        if region not in ordered:
            ordered.append(region)
    return ordered


def _find_cabins(sentence: str) -> list[tuple[int, str]]:
    low = sentence.lower()
    found: list[tuple[int, str]] = []
    claimed: list[tuple[int, int]] = []  # spans already matched (longest-first)
    for phrase, canonical in _CABIN_WORDS:
        start = 0
        while True:
            pos = low.find(phrase, start)
            if pos < 0:
                break
            span = (pos, pos + len(phrase))
            if not any(a <= pos < b for a, b in claimed):
                found.append((pos, canonical))
                claimed.append(span)
            start = pos + len(phrase)
    found.sort()
    # de-dupe same cabin keeping first position
    seen: set[str] = set()
    out: list[tuple[int, str]] = []
    for pos, c in found:
        if c not in seen:
            seen.add(c)
            out.append((pos, c))
    return out


def _find_miles(sentence: str) -> list[tuple[int, int]]:
    """Ordered (position, value) miles candidates within the plausible range."""
    cands: list[tuple[int, int]] = []
    seen_pos: set[int] = set()
    # Prefer numbers explicitly suffixed with miles/points.
    for m in _MILES_NEAR_KEYWORD.finditer(sentence):
        val = int(re.sub(r"[^0-9]", "", m.group(1)))
        if _MIN_MILES <= val <= _MAX_MILES:
            cands.append((m.start(1), val))
            seen_pos.add(m.start(1))
    if cands:
        return sorted(cands)
    # Fall back to context-attached numbers.
    for m in _MILES_CTX.finditer(sentence):
        if m.start(1) in seen_pos:
            continue
        val = int(re.sub(r"[^0-9]", "", m.group(1)))
        if _MIN_MILES <= val <= _MAX_MILES:
            cands.append((m.start(1), val))
    return sorted(cands)


# How far on each side of a miles figure to look for its program/cabin/regions.
# Generous to the left (the number usually trails the description), tighter to
# the right, so adjacent items in a list don't bleed into each other.
_WINDOW_BEFORE, _WINDOW_AFTER = 220, 60


class DeterministicExtractor:
    """Keyless `LLMExtractor` backend (see module docstring).

    Strategy: anchor on each grounded miles figure, then read the program,
    cabin, and region pair from a bounded text window around it. This survives
    line-wrapping, em-dashes, and HTML — the signals rarely sit in one
    "sentence" — while staying conservative (a window missing any signal yields
    nothing). The miles number is still hard-grounded against the source.
    """

    name = "deterministic"

    def extract(self, document: str, *, source_hint: str = "") -> List[RawChartRow]:
        text = html_to_text(document)
        # Collapse whitespace so wrapping/indentation doesn't fragment windows.
        text = re.sub(r"\s+", " ", text).strip()
        rows: list[RawChartRow] = []
        for pos, miles in _find_miles(text):
            row = self._row_for_number(text, pos, miles)
            if row is not None:
                rows.append(row)
        # De-dupe identical rows (a newsletter often repeats its headline).
        uniq: dict[tuple, RawChartRow] = {}
        for r in rows:
            uniq[(r.program, r.region_a, r.region_b, r.cabin, r.miles)] = r
        return list(uniq.values())

    def _row_for_number(
        self, text: str, pos: int, miles: int
    ) -> RawChartRow | None:
        # Reject sign-up/welcome bonuses (a number, but not an award price).
        local_ctx = text[max(0, pos - 40):pos + 20]
        if _BONUS_CTX.search(local_ctx):
            return None

        # Anchor the claim to its nearest program mention, then read only the
        # clause from that mention to just past the number — no cross-item bleed.
        start = _nearest_program_start(text, pos, _WINDOW_BEFORE)
        if start is None:
            return None
        clause = text[start:pos + _WINDOW_AFTER]

        program = _find_program(clause)
        if program is None:
            return None
        cabins = _find_cabins(clause)
        if not cabins:
            return None
        regions = _find_regions(clause)
        if not regions:
            return None
        if not number_is_grounded(miles, clause):
            return None  # hard guard: never emit an ungrounded number

        # Cabin nearest the number wins (the clause usually holds exactly one).
        local = pos - start
        cabin = min(cabins, key=lambda c: abs(c[0] - local))[1]
        if cabin not in _CABINS:
            return None

        # Region pair: two distinct hints -> (origin, dest); one hint -> assume
        # the common US-newsletter framing "<somewhere> to <region>" from NA.
        if len(regions) >= 2:
            region_a, region_b = regions[0], regions[1]
        else:
            other = regions[0]
            region_a, region_b = (
                ("north_america", other)
                if other != "north_america"
                else ("north_america", "north_america")
            )
        roundtrip = bool(
            re.search(r"round[\s-]?trip|return ticket", clause, re.IGNORECASE)
        )
        return RawChartRow(
            program=program,
            region_a=region_a,
            region_b=region_b,
            cabin=cabin,
            miles=miles,
            roundtrip=roundtrip,
        )
