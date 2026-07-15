"""Loyalty-program name → canonical program id (matches knowledge/ratios.yaml).

Used by both the deterministic prose scanner (finding program mentions in
sentences) and `OllamaExtractor` (normalizing free-text model output like
``turkish miles&smiles`` → ``turkish``).
"""

from __future__ import annotations

from typing import Optional

# Phrase / brand -> canonical program id.
PROGRAM_ALIASES: dict[str, str] = {
    "turkish": "turkish",
    "miles&smiles": "turkish",
    "miles and smiles": "turkish",
    "aeroplan": "aeroplan",
    "air canada": "aeroplan",
    "lifemiles": "lifemiles",
    "avianca": "lifemiles",
    "ana": "ana",
    "ana mileage": "ana",
    "ana mileage club": "ana",
    "krisflyer": "krisflyer",
    "singapore airlines": "krisflyer",
    "singapore krisflyer": "krisflyer",
    "eva air": "eva",
    "infinity mileagelands": "eva",
}

_CANONICAL_IDS = frozenset(PROGRAM_ALIASES.values())

# Longest phrases first so "ana mileage club" wins over bare "ana".
_ALIASES_LONGEST_FIRST: list[tuple[str, str]] = sorted(
    PROGRAM_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True
)


def canonicalize_program(text: str) -> Optional[str]:
    """Map a program label (model output or prose fragment) to a canonical id."""
    if not text or not str(text).strip():
        return None
    low = str(text).strip().lower()

    exact = PROGRAM_ALIASES.get(low)
    if exact is not None:
        return exact
    if low in _CANONICAL_IDS:
        return low

    best_len = -1
    best_pos = len(low) + 1
    best_canonical: Optional[str] = None
    for phrase, canonical in _ALIASES_LONGEST_FIRST:
        pos = low.find(phrase)
        if pos < 0:
            continue
        if len(phrase) > best_len or (len(phrase) == best_len and pos < best_pos):
            best_len = len(phrase)
            best_pos = pos
            best_canonical = canonical
    return best_canonical
