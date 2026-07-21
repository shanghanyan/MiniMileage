"""Build the redemption graph from verified edges (§3).

Nodes:
  - the source currency (e.g. "capital_one")
  - one node per partner program
  - a single SEAT sink representing the requested route/cabin

Edges (MultiDiGraph — parallel currency→program edges for base vs bonus):
  - currency -> program : a verified TransferRatio (attr: ratio, effective_ratio, ...)
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

# Max transfer hops (currency → … → program) before the SEAT edge. Caps
# combinatorial growth once program→program edges appear.
MAX_TRANSFER_HOPS = 2


def build_graph(
    currency: str,
    ratios: Iterable[TransferRatio],
    awards: Iterable[VerifiedAward],
) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    g.add_node(currency, kind="currency")
    g.add_node(SEAT_NODE, kind="seat")

    for r in ratios:
        if r.from_currency != currency:
            continue
        g.add_node(r.to_program, kind="program")
        key = "bonus" if r.is_bonus else "base"
        # Distinct keys so base + bonus both exist as parallel edges.
        if r.is_bonus and r.bonus_label:
            key = f"bonus:{r.bonus_label}"
        g.add_edge(
            r.from_currency,
            r.to_program,
            key=key,
            ratio=r.effective_ratio,
            base_ratio=r.ratio,
            bonus_multiplier=r.bonus_multiplier,
            confidence=r.confidence,
            provenance=r.provenance,
            flags=list(r.flags),
            bonus_label=r.bonus_label,
        )

    for a in awards:
        # Only wire programs the currency can actually reach.
        if a.program not in g:
            continue
        g.add_edge(
            a.program,
            SEAT_NODE,
            key="award",
            miles=a.miles,
            confidence=a.confidence,
            provenance=a.provenance,
            flags=list(a.flags),
            seats_available=a.seats_available,
        )
    return g
