"""Verbatim-number grounding — the hard anti-hallucination guard (§6.2).

The one thing extraction cannot afford to invent is a number. Constrained
decoding guarantees a *parsable* row; grounding guarantees the `miles` integer
was actually *present in the source*. A schema-valid row with a fabricated
number is the dangerous failure mode, and this kills it deterministically
before it can ever become an `AwardQuote`.

The match is comma/space/non-breaking-space-insensitive, so "45,000",
"45 000", "45000", and "45k" in the source all ground a parsed 45000.
"""

from __future__ import annotations

import re


def _digit_runs(text: str) -> set[int]:
    """Every integer literally written in `text`, normalized to a plain int.

    Handles thousands separators (commas, spaces, NBSP) and the "k"/"K"
    shorthand ("45k" -> 45000, "1.5k" -> 1500).
    """
    out: set[int] = set()
    norm = text.replace(" ", " ")
    # "45k" / "1.5k" shorthand
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[kK]\b", norm):
        try:
            out.add(int(round(float(m.group(1)) * 1000)))
        except ValueError:
            continue
    # Plain numbers with optional thousands separators (comma or single space).
    for m in re.finditer(r"\d{1,3}(?:[,\s]\d{3})+|\d+", norm):
        digits = re.sub(r"[^0-9]", "", m.group(0))
        if digits:
            out.add(int(digits))
    return out


def number_is_grounded(miles: int, source_text: str) -> bool:
    """True iff `miles` appears literally in `source_text` (separator-insensitive)."""
    if miles <= 0:
        return False
    return miles in _digit_runs(source_text)
