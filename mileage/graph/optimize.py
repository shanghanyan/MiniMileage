"""Rank redemption paths by cents-per-point (§7).

Enumerates every currency -> ... -> SEAT path, compounds the transfer ratios
along the hops, converts the seat's program-miles cost into source points, and
computes CPP = cash_cents / source_points. Always includes the portal floor as
a baseline option so the verdict can compare honestly.
"""

from __future__ import annotations

from typing import Optional

import networkx as nx

from ..domain.cpp import (
    cpp as cpp_fn,
    compound_ratio,
    portal_points_needed,
    source_points_for_award,
)
from ..domain.models import PathOption, Provenance
from .build import SEAT_NODE


def _portal_option(
    cash_cents: int,
    portal_cpp: float,
    balance: int,
    fare_confidence: float,
    fare_flags: list[str],
) -> PathOption:
    pts = portal_points_needed(cash_cents, portal_cpp)
    return PathOption(
        label="Capital One portal",
        kind="portal",
        cpp=portal_cpp,
        source_points=int(pts),
        cash_cents=cash_cents,
        program=None,
        affordable=balance >= pts,
        # The 1.25c rate is contractual, but the recommendation to cover THIS
        # fare is only as trustworthy as the price-to-beat. Confidence is bounded
        # by the fare so no row ever exceeds its weakest load-bearing input.
        confidence=round(fare_confidence, 3),
        flags=list(fare_flags),
    )


def rank_paths(
    graph: nx.DiGraph,
    currency: str,
    cash_cents: int,
    *,
    portal_cpp: float,
    balance: int,
    fare_confidence: float = 1.0,
    fare_flags: Optional[list[str]] = None,
) -> list[PathOption]:
    fare_flags = fare_flags or []
    options: list[PathOption] = [
        _portal_option(cash_cents, portal_cpp, balance, fare_confidence, fare_flags)
    ]

    if currency not in graph or SEAT_NODE not in graph:
        return sorted(options, key=lambda o: o.cpp, reverse=True)

    for path in nx.all_simple_paths(graph, currency, SEAT_NODE):
        ratios: list[float] = []
        confidences: list[float] = [fare_confidence]
        provenance: list[Provenance] = []
        flags: set[str] = set(fare_flags)
        program = path[-2]  # node feeding SEAT
        seats_available: Optional[int] = None

        for u, v in zip(path, path[1:]):
            edge = graph.edges[u, v]
            confidences.append(edge.get("confidence", 0.5))
            if edge.get("provenance"):
                provenance.append(edge["provenance"])
            flags.update(edge.get("flags", []))
            if v == SEAT_NODE:
                miles = edge["miles"]
                seats_available = edge.get("seats_available")
            else:
                ratios.append(edge["ratio"])

        if seats_available is not None:
            flags.add(f"{seats_available} seats")

        eff_ratio = compound_ratio(ratios)
        source_points = source_points_for_award(miles, eff_ratio)
        path_cpp = cpp_fn(cash_cents, source_points)
        path_conf = 1.0
        for c in confidences:
            path_conf *= c

        label = "Capital One -> " + " -> ".join(path[1:-1])
        options.append(
            PathOption(
                label=label,
                kind="transfer",
                cpp=round(path_cpp, 4),
                source_points=int(source_points),
                cash_cents=cash_cents,
                program=program,
                affordable=balance >= source_points,
                confidence=round(path_conf, 3),
                flags=sorted(flags),
                provenance=provenance,
            )
        )

    return sorted(options, key=lambda o: o.cpp, reverse=True)
