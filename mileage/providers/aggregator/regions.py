"""Region-label canonicalization — the parser-side fix for §A (live-data gap).

`parse.py` reads award-chart cells as raw human text ("North America",
"Atlantic", "Within North America", "0–4,000 mi"). `domain/charts.py` resolves a
route only when a band's region pair equals the route's canonical region tokens
(`north_america`, `europe`, …) EXACTLY. Without a normalizer in between, real
pages never match — `"north america"` (space) != `"north_america"` (underscore),
and distance bands have no token at all. That is why scraping real ATF /
10xtravel chart pages produced parsed rows that never became route quotes.

This module is the missing layer. It lives on the parser side (Engine A), NOT in
`domain/` or `verify/` (which must never import providers). It maps human zone
labels to the canonical tokens used in `charts.yaml::region_map`, and returns
`None` for anything it cannot map — so an unrecognized zone is *dropped and
counted*, never guessed (same no-hallucination contract as the rest of §6).

Two shapes of chart cell are handled:

  - Zone-pair charts (LifeMiles/Turkish ATF): a `from` and a `to` cell, each a
    single zone -> `canonicalize_region`.
  - Distance-band charts (Aeroplan): a `from` cell naming a zone *pair*
    ("Between North America and Atlantic") plus a separate distance band
    ("0–4,000 mi") -> `canonicalize_zone_pair` + `parse_distance_band`.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# Canonical region tokens (must match knowledge/charts.yaml::region_map values).
_CANONICAL = {
    "north_america",
    "europe",
    "north_asia",
    "southeast_asia",
    "south_asia",
    "middle_east",
    "oceania",
    "south_america",
    "africa",
}

# Exact (normalized) label -> token. Normalization lowercases, replaces any
# non-alphanumeric run with a single space, and strips — so "North_America",
# "north america", and "NORTH-AMERICA" all collapse to "north america".
_EXACT: dict[str, str] = {
    # North America
    "north america": "north_america",
    "n america": "north_america",
    "within north america": "north_america",
    "continental us": "north_america",
    "continental united states": "north_america",
    "united states": "north_america",
    "usa": "north_america",
    "us": "north_america",
    "u s": "north_america",
    "u s a": "north_america",
    "us domestic": "north_america",      # LifeMiles within-US rows
    "domestic us": "north_america",
    "canada": "north_america",
    "mexico": "north_america",
    "hawaii": "north_america",
    # Europe (Aeroplan calls the transatlantic zone "Atlantic")
    "europe": "europe",
    "atlantic": "europe",
    "eu": "europe",
    "uk": "europe",
    "united kingdom": "europe",
    "schengen": "europe",
    "western europe": "europe",
    "eastern europe": "europe",
    # North Asia (Aeroplan's "Pacific" is the closest single zone we model)
    "north asia": "north_asia",
    "northeast asia": "north_asia",
    "ne asia": "north_asia",
    "pacific": "north_asia",
    "japan": "north_asia",
    "korea": "north_asia",
    "south korea": "north_asia",
    "china": "north_asia",
    "hong kong": "north_asia",
    "taiwan": "north_asia",
    # Southeast Asia
    "southeast asia": "southeast_asia",
    "south east asia": "southeast_asia",
    "se asia": "southeast_asia",
    "singapore": "southeast_asia",
    "thailand": "southeast_asia",
    "malaysia": "southeast_asia",
    "indonesia": "southeast_asia",
    "vietnam": "southeast_asia",
    "philippines": "southeast_asia",
    # South Asia
    "south asia": "south_asia",
    "indian subcontinent": "south_asia",
    "india": "south_asia",
    # Middle East
    "middle east": "middle_east",
    "gulf": "middle_east",
    "dubai": "middle_east",              # Emirates ATF chart hub token (2026-07-20)
    "abu dhabi": "middle_east",          # Etihad ATF chart hub token (2026-07-20)
    # Oceania
    "oceania": "oceania",
    "south pacific": "oceania",
    "australia": "oceania",
    "new zealand": "oceania",
    "nz": "oceania",
    "australia nz": "oceania",           # LifeMiles "Australia / NZ" zone
    "australia new zealand": "oceania",
    # South America
    "south america": "south_america",
    "southern south america": "south_america",
    "latin america south": "south_america",
    # Africa
    "africa": "africa",
    "sub saharan africa": "africa",
    "southern africa": "africa",
    "north africa": "africa",            # EVA Star Alliance chart column (§ 2026-07-08)
    "central south africa": "africa",    # EVA "Central, South Africa" column
    # Oceania (EVA's Star Alliance chart calls it "South West Pacific")
    "south west pacific": "oceania",
    "southwest pacific": "oceania",
    # North America (EVA folds Hawaii + Central America into one column; we
    # don't have a separate Central America bucket in the 9-region taxonomy,
    # so this is a deliberate coarser-precision choice, not a guess at a made-
    # up number — same tradeoff already accepted for plain "hawaii" above).
    "hawaii central america": "north_america",
}

# Multi-word aliases safe to match as substrings inside a longer phrase (used to
# pull each side out of a zone-pair label like "Between North America and
# Atlantic"). Ordered longest-first so "north america" wins over a bare token.
_CONTAINS: list[tuple[str, str]] = sorted(
    ((k, v) for k, v in _EXACT.items() if " " in k),
    key=lambda kv: len(kv[0]),
    reverse=True,
)

# Canonical tokens are themselves valid inputs (idempotent): the existing
# awardatlas fixture already emits "north_america"/"europe".
for _tok in _CANONICAL:
    _EXACT.setdefault(_tok.replace("_", " "), _tok)


def _norm(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(label).lower()).strip()


def canonicalize_region(label: str) -> Optional[str]:
    """Map a single human zone label to a canonical token, or None if unmapped.

    None means "drop this row" — we never guess a region we don't recognize.
    """
    if not label:
        return None
    norm = _norm(label)
    if not norm:
        return None
    hit = _lookup(norm)
    if hit:
        return hit
    # Retry once after dropping a trailing/inline qualifier in parentheses or
    # brackets — real charts tag a single region with a program zone code:
    # "Hawaii (Zone 5)" -> "Hawaii", "Japan (Zone 1-A)" -> "Japan". Composite
    # multi-region zones ("Asia 2, Russia 3 (Zone 4)") still fail _lookup after
    # stripping, so they stay dropped + counted — never guessed (§A contract).
    stripped = _norm(re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", str(label)))
    if stripped and stripped != norm:
        return _lookup(stripped)
    return None


def _lookup(norm: str) -> Optional[str]:
    """Exact map, then a safe multi-word containment match. Single short tokens
    are NOT matched as substrings (so "us" never matches inside "australia")."""
    if norm in _EXACT:
        return _EXACT[norm]
    for alias, token in _CONTAINS:
        if alias in norm:
            return token
    return None


def canonicalize_zone_pair(label: str) -> Optional[Tuple[str, str]]:
    """Pull a canonical (region_a, region_b) pair out of one cell.

    Handles the Aeroplan-style `from` column whose single cell names a pair:
      "Between North America and Atlantic" -> ("north_america", "europe")
      "Within North America"               -> ("north_america", "north_america")
    Returns None if fewer than the needed tokens can be canonicalized.
    """
    if not label:
        return None
    norm = _norm(label)
    if norm.startswith("within "):
        tok = canonicalize_region(norm[len("within "):])
        return (tok, tok) if tok else None
    if norm.startswith("between "):
        norm = norm[len("between "):]
    # Split on connective words/punctuation, canonicalize each side. A pair needs
    # BOTH sides to canonicalize — "Between North America and <unmapped>" returns
    # None (drop), never a guessed (X, X). Only the explicit "Within X" form
    # above produces a same-region pair.
    parts = re.split(r"\b(?:and|to|vs|or)\b|[/&,]", norm)
    tokens: list[str] = []
    for part in parts:
        tok = canonicalize_region(part.strip())
        if tok and tok not in tokens:
            tokens.append(tok)
    if len(tokens) == 2:
        return (tokens[0], tokens[1])
    return None


def parse_distance_band(label: str) -> Optional[Tuple[int, int]]:
    """Parse a distance-band cell into (low_miles, high_miles).

    "0–4,000 mi"        -> (0, 4000)
    "1,501-2,750 miles" -> (1501, 2750)
    "6,001+ mi"         -> (6001, 999999)   open-ended upper band
    Returns None if no usable numeric band is present.
    """
    if not label:
        return None
    text = str(label).replace(",", "")
    nums = re.findall(r"\d+", text)
    if not nums:
        return None
    lo = int(nums[0])
    if len(nums) >= 2:
        hi = int(nums[1])
    elif "+" in text or "over" in text.lower() or "above" in text.lower():
        hi = 999_999
    else:
        # A single number with no "+": treat as an exact-ish ceiling band.
        hi = lo
    if hi < lo:
        lo, hi = hi, lo
    return (lo, hi)
