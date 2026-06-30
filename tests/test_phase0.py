"""Phase 0 guarantees as executable tests.

Runs standalone (`python tests/test_phase0.py`) and is pytest-discoverable.
Asserts the two properties the prose must not overclaim:

  1. The engine STRUCTURALLY cannot route Capital One -> United, even if a
     malicious/erroneous United award is injected (United is not a Cap One
     transfer partner; absence is enforced, not incidental).
  2. No redemption's confidence can exceed the confidence of its weakest
     load-bearing input (in Phase 0, the stubbed cash price-to-beat).
"""

from __future__ import annotations

import os as _os; _os.environ.setdefault("MILEAGE_OFFLINE", "1")  # hermetic standalone runs

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.config import Config, build_registry, partner_programs
from mileage.domain.models import (
    AwardQuote,
    Cabin,
    Layer,
    Provenance,
    Route,
    TransferRatio,
    User,
)
from mileage.graph.build import SEAT_NODE, build_graph
from mileage.graph.optimize import rank_paths
from mileage.providers.base import Query
from mileage.cli import run_quote

_CONFIG = Config()


def test_united_is_unreachable_even_if_injected() -> None:
    """A planted United award must not produce any redemption path."""
    currency = "capital_one"
    route = Route("LAX", "JFK", Cabin.ECONOMY)

    # Real curated ratios (United is intentionally absent).
    assert "united" not in partner_programs(_CONFIG), (
        "United must not be a declared Capital One transfer partner"
    )
    ratios = [
        TransferRatio(from_currency=currency, to_program="turkish", ratio=1.0),
        TransferRatio(from_currency=currency, to_program="aeroplan", ratio=1.0),
    ]

    # Inject a bogus, fully-formed United award (the adversarial case).
    from mileage.verify.crosscheck import VerifiedAward

    poisoned = [
        VerifiedAward(
            program="united",
            route=route,
            miles=12500,
            confidence=0.99,
            flags=[],
            provenance=[Provenance(source_name="malicious")],
        )
    ]

    graph = build_graph(currency, ratios, poisoned)
    assert "united" not in graph.nodes, "United must never enter the graph"

    options = rank_paths(
        graph, currency, 15800, portal_cpp=1.25, balance=20000
    )
    programs_used = {o.program for o in options if o.kind == "transfer"}
    assert "united" not in programs_used, (
        "No path may route through United without a Capital One -> United ratio"
    )


def test_confidence_never_exceeds_weakest_input() -> None:
    """Every option's confidence <= the cash price-to-beat confidence."""
    registry = build_registry(_CONFIG)
    for origin, dest, cabin, miles in [
        ("LAX", "JFK", Cabin.ECONOMY, 20000),
        ("LAX", "IST", Cabin.BUSINESS, 90000),
    ]:
        route = Route(origin, dest, cabin)
        user = User(balances={"capital_one": miles}, card="venture_x")
        result = run_quote(route, user, "capital_one", registry=registry)
        fare_conf = result["fare"].confidence
        for opt in result["verdict"].options:
            assert opt.confidence <= fare_conf + 1e-9, (
                f"{route.key()} {opt.label}: confidence {opt.confidence} "
                f"exceeds weakest input (fare conf {fare_conf})"
            )


if __name__ == "__main__":
    test_united_is_unreachable_even_if_injected()
    test_confidence_never_exceeds_weakest_input()
    print("OK: United unreachable + confidence bounded by weakest input")
