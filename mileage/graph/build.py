"""Build the redemption graph from verified edges (§3).

Nodes:
  - the source currency (e.g. "capital_one")
  - one node per partner program
  - a single SEAT sink representing the requested route/cabin

Edges:
  - currency -> program : a verified TransferRatio (attr: ratio, confidence, ...)
  - program  -> SEAT    : a verified award cost (attr: miles, confidence, flags)

A program only connects to SEAT if there is a verified award cost for it, and
only contributes a path if the currency can reach it — so the absence of a
Capital One -> United ratio structurally prevents any United path.
"""

from __future__ import annotations

from typing import Iterable

import networkx as nx

from ..domain.models import TransferRatio
from ..verify.crosscheck import VerifiedAward

SEAT_NODE = "__SEAT__"


def build_graph(
    currency: str,
    ratios: Iterable[TransferRatio],
    awards: Iterable[VerifiedAward],
) -> nx.DiGraph:
    g = nx.DiGraph()
    g.add_node(currency, kind="currency")
    g.add_node(SEAT_NODE, kind="seat")

    for r in ratios:
        if r.from_currency != currency:
            continue
        g.add_node(r.to_program, kind="program")
        g.add_edge(
            r.from_currency,
            r.to_program,
            ratio=r.ratio,
            confidence=r.confidence,
            provenance=r.provenance,
            flags=list(r.flags),
        )

    for a in awards:
        # Only wire programs the currency can actually reach.
        if a.program not in g:
            continue
        g.add_edge(
            a.program,
            SEAT_NODE,
            miles=a.miles,
            confidence=a.confidence,
            provenance=a.provenance,
            flags=list(a.flags),
            seats_available=a.seats_available,
        )
    return g
