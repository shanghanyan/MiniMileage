"""Transfer-ratio logic for Capital One transferable miles.

Pure logic only: the ratio *data* lives in knowledge/ratios.yaml and is loaded
by providers/curated.py into `TransferRatio` rows. This module answers questions
*about* a set of ratios.

Load-bearing domain fact (Cursor-Mileage-Plan.md §0): Capital One does NOT
transfer directly to United MileagePlus. That is represented structurally by the
absence of a `capital_one -> united` ratio, and `has_direct_transfer` will
report False for it.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .models import TransferRatio


def index_by_program(ratios: Iterable[TransferRatio]) -> dict[str, TransferRatio]:
    """Most-trusted ratio per destination program."""
    best: dict[str, TransferRatio] = {}
    for r in ratios:
        cur = best.get(r.to_program)
        if cur is None or r.confidence > cur.confidence:
            best[r.to_program] = r
    return best


def has_direct_transfer(
    ratios: Iterable[TransferRatio], from_currency: str, to_program: str
) -> bool:
    return any(
        r.from_currency == from_currency and r.to_program == to_program
        for r in ratios
    )


def ratio_for(
    ratios: Iterable[TransferRatio], from_currency: str, to_program: str
) -> Optional[TransferRatio]:
    candidates = [
        r
        for r in ratios
        if r.from_currency == from_currency and r.to_program == to_program
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.confidence)
