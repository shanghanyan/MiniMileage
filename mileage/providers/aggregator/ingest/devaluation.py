"""Devaluation fast-path (§6.2/§D) — proactive staleness from headlines.

A subject/title matching `"{program} devaluation"` / `"award chart change"`
immediately bumps that program's charts to `stale` in the store, instead of
waiting for the next scheduled run to notice. The mechanism lives entirely in
`store/` (`program_staleness`), so `domain/`/`verify/` are untouched — the
aggregator caps the affected quotes' `source_updated_at` before the freshness
cutoff on emit, and the existing `verify/crosscheck.py` demotes them.

Detection is shared by every intake: email subjects (`email_source.py`) and
blog/transcript titles (`creators.py` / `transcripts.py`) all funnel through
`detect_devaluation`. The program list is the known loyalty programs only — a
headline naming no known program flags nothing.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

log = logging.getLogger("mileage.aggregator.ingest.devaluation")

# Program -> brand aliases a "<program> devaluation" headline can name.
_DEVALUATION_PROGRAMS: dict[str, list[str]] = {
    "turkish": ["turkish", "miles&smiles", "miles and smiles"],
    "aeroplan": ["aeroplan", "air canada"],
    "lifemiles": ["lifemiles", "avianca"],
    "ana": ["ana"],
    "krisflyer": ["krisflyer", "singapore"],
}

# Phrases that signal a chart change/devaluation (case-insensitive).
_DEVALUATION_TRIGGERS = (
    "devaluation",
    "award chart change",
    "chart change",
    "devalues",
    "devalued",
    "devaluing",
)


def detect_devaluation(text: str) -> Optional[str]:
    """Return the program a devaluation subject/title flags stale, or None."""
    low = (text or "").lower()
    if not any(trigger in low for trigger in _DEVALUATION_TRIGGERS):
        return None
    for program, aliases in _DEVALUATION_PROGRAMS.items():
        if any(alias in low for alias in aliases):
            return program
    return None


def mark_devaluations_stale(
    repo: Any,
    programs: Iterable[str],
    *,
    reason: Optional[str] = None,
) -> set[str]:
    """Persist each program as stale in the store (devaluation fast-path).

    Returns the set actually marked. A repo without `mark_program_stale`
    (or `None`) is a no-op — discovery still records `stale_programs` in
    `discovered_charts.json` as the fallback path.
    """
    marked: set[str] = set()
    if repo is None or not hasattr(repo, "mark_program_stale"):
        return marked
    for program in programs:
        program = str(program).strip().lower()
        if not program:
            continue
        try:
            repo.mark_program_stale(program, reason=reason)
            marked.add(program)
            log.info("devaluation: marked %s stale (%s)", program, reason or "headline")
        except Exception as exc:  # store hiccup must not break discovery
            log.warning("could not mark %s stale: %s", program, exc)
    return marked
