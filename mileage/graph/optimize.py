"""Rank redemption paths by cents-per-point (§7).

Enumerates every currency -> ... -> SEAT path on a MultiDiGraph (so base and
transfer-bonus edges are both considered), compounds the effective transfer
ratios along the hops, converts the seat's program-miles cost into source
points, and computes CPP = cash_cents / source_points. Always includes the
portal floor as a baseline option so the verdict can compare honestly.
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
from .build import MAX_TRANSFER_HOPS, SEAT_NODE


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


def _hop_label(node: str, edge: dict) -> str:
    """Human label for one transfer hop, including bonus annotation when present."""
    name = node.replace("_", " ").title()
    label = edge.get("bonus_label")
    if label:
        return f"{name} ({label})"
    if edge.get("bonus_multiplier", 1.0) != 1.0:
        pct = int(round((edge["bonus_multiplier"] - 1.0) * 100))
        return f"{name} (+{pct}% bonus)"
    return name


def rank_paths(
    graph: nx.MultiDiGraph | nx.DiGraph,
    currency: str,
    cash_cents: int,
    *,
    portal_cpp: float,
    balance: int,
    fare_confidence: float = 1.0,
    fare_flags: Optional[list[str]] = None,
    max_transfer_hops: int = MAX_TRANSFER_HOPS,
) -> list[PathOption]:
    fare_flags = fare_flags or []
    options: list[PathOption] = [
        _portal_option(cash_cents, portal_cpp, balance, fare_confidence, fare_flags)
    ]

    if currency not in graph or SEAT_NODE not in graph:
        return sorted(options, key=lambda o: o.cpp, reverse=True)

    # cutoff = nodes in path = currency + up to max_transfer_hops + SEAT
    cutoff = max_transfer_hops + 2

    if isinstance(graph, nx.MultiDiGraph):
        edge_paths = nx.all_simple_edge_paths(graph, currency, SEAT_NODE, cutoff=cutoff)
        for edge_path in edge_paths:
            # edge_path: list of (u, v, key)
            ratios: list[float] = []
            confidences: list[float] = [fare_confidence]
            provenance: list[Provenance] = []
            flags: set[str] = set(fare_flags)
            hop_labels: list[str] = []
            program = edge_path[-1][0]  # node feeding SEAT
            seats_available: Optional[int] = None
            transfer_hops = 0

            for u, v, key in edge_path:
                edge = graph.edges[u, v, key]
                confidences.append(edge.get("confidence", 0.5))
                if edge.get("provenance"):
                    provenance.append(edge["provenance"])
                flags.update(edge.get("flags", []))
                if v == SEAT_NODE:
                    miles = edge["miles"]
                    seats_available = edge.get("seats_available")
                else:
                    ratios.append(edge.get("ratio", edge.get("effective_ratio", 1.0)))
                    hop_labels.append(_hop_label(v, edge))
                    transfer_hops += 1

            if seats_available is not None:
                flags.add(f"{seats_available} seats")
            if transfer_hops > 1:
                flags.add("multi_hop")

            eff_ratio = compound_ratio(ratios)
            source_points = source_points_for_award(miles, eff_ratio)
            path_cpp = cpp_fn(cash_cents, source_points)
            path_conf = 1.0
            for c in confidences:
                path_conf *= c

            label = "Capital One -> " + " -> ".join(hop_labels)
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
    else:
        # Legacy DiGraph path (tests / callers that still build a simple graph).
        for path in nx.all_simple_paths(graph, currency, SEAT_NODE, cutoff=cutoff):
            ratios = []
            confidences = [fare_confidence]
            provenance = []
            flags_set: set[str] = set(fare_flags)
            program = path[-2]
            seats_available = None
            for u, v in zip(path, path[1:]):
                edge = graph.edges[u, v]
                confidences.append(edge.get("confidence", 0.5))
                if edge.get("provenance"):
                    provenance.append(edge["provenance"])
                flags_set.update(edge.get("flags", []))
                if v == SEAT_NODE:
                    miles = edge["miles"]
                    seats_available = edge.get("seats_available")
                else:
                    ratios.append(edge["ratio"])
            if seats_available is not None:
                flags_set.add(f"{seats_available} seats")
            if len(path) - 2 > 1:
                flags_set.add("multi_hop")
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
                    flags=sorted(flags_set),
                    provenance=provenance,
                )
            )

    return sorted(options, key=lambda o: o.cpp, reverse=True)
