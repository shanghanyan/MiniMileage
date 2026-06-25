"""Pure domain core — no I/O. The durable value of the product.

Rule (Cursor-Mileage-Plan.md §4): `domain/` and `verify/` never import from
`providers/`, `aggregator/`, or `brain/`. Dependencies point inward only.
"""

from .models import (
    Cabin,
    Layer,
    Provenance,
    Route,
    AwardQuote,
    FareQuote,
    TransferRatio,
    User,
    Verdict,
    VerdictLabel,
    PathOption,
    PORTAL_CPP,
)

__all__ = [
    "Cabin",
    "Layer",
    "Provenance",
    "Route",
    "AwardQuote",
    "FareQuote",
    "TransferRatio",
    "User",
    "Verdict",
    "VerdictLabel",
    "PathOption",
    "PORTAL_CPP",
]
