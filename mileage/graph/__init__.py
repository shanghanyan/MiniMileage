"""Graph + optimizer — the CPP-by-product model (§3, §7).

A NetworkX DiGraph whose edges are verified transfer ratios and award costs.
The optimizer enumerates currency -> program -> seat paths, compounds ratios per
hop, computes cents-per-point, and ranks. Multi-hop ready for the north star;
single-hop in Phase 0.
"""

from .build import build_graph, SEAT_NODE
from .optimize import rank_paths

__all__ = ["build_graph", "rank_paths", "SEAT_NODE"]
