"""Transfer-bonus multi-path ranking tests."""

from __future__ import annotations

import os as _os
from datetime import date

_os.environ.setdefault("MILEAGE_OFFLINE", "1")

from mileage.domain.models import Cabin, Layer, Provenance, Route, TransferRatio
from mileage.graph.build import build_graph
from mileage.graph.optimize import rank_paths
from mileage.providers.base import Query
from mileage.providers.curated import CuratedProvider, _partner_entries
from mileage.verify.crosscheck import VerifiedAward


def test_effective_ratio_property() -> None:
    r = TransferRatio(
        from_currency="capital_one",
        to_program="aeroplan",
        ratio=1.0,
        bonus_multiplier=1.3,
        flags=["transfer_bonus"],
    )
    assert r.effective_ratio == 1.3
    assert r.is_bonus


def test_partner_entries_emits_base_and_active_bonus() -> None:
    rows = _partner_entries(
        "aeroplan",
        {
            "ratio": 1.0,
            "bonus": 1.3,
            "valid_from": "2026-07-01",
            "valid_until": "2026-12-31",
            "label": "+30% Aeroplan transfer bonus",
        },
        today=date(2026, 7, 20),
    )
    assert len(rows) == 2
    assert rows[0][1] == 1.0  # base multiplier
    assert rows[1][1] == 1.3
    assert "transfer_bonus" in rows[1][2]


def test_partner_entries_skips_expired_bonus() -> None:
    rows = _partner_entries(
        "aeroplan",
        {
            "ratio": 1.0,
            "bonus": 1.3,
            "valid_until": "2026-06-01",
        },
        today=date(2026, 7, 20),
    )
    assert len(rows) == 1
    assert rows[0][1] == 1.0


def test_curated_loads_aeroplan_bonus_when_in_window() -> None:
    provider = CuratedProvider(as_of=date(2026, 7, 20))
    ratios = [
        q
        for q in provider.fetch(Query(Route("LAX", "IST", Cabin.BUSINESS), Layer.CHARTS))
        if isinstance(q, TransferRatio) and q.to_program == "aeroplan"
    ]
    assert len(ratios) == 2
    assert any(r.is_bonus for r in ratios)
    assert any(not r.is_bonus for r in ratios)


def test_curated_skips_aeroplan_bonus_outside_window() -> None:
    provider = CuratedProvider(as_of=date(2027, 1, 15))
    ratios = [
        q
        for q in provider.fetch(Query(Route("LAX", "IST", Cabin.BUSINESS), Layer.CHARTS))
        if isinstance(q, TransferRatio) and q.to_program == "aeroplan"
    ]
    assert len(ratios) == 1
    assert ratios[0].bonus_multiplier == 1.0


def test_bonus_path_beats_base_path() -> None:
    """Same award miles: +30% bonus needs fewer source points → higher CPP."""
    currency = "capital_one"
    ratios = [
        TransferRatio(from_currency=currency, to_program="aeroplan", ratio=1.0),
        TransferRatio(
            from_currency=currency,
            to_program="aeroplan",
            ratio=1.0,
            bonus_multiplier=1.3,
            flags=["transfer_bonus"],
            bonus_label="+30% Aeroplan transfer bonus",
        ),
    ]
    awards = [
        VerifiedAward(
            program="aeroplan",
            route=Route("LAX", "IST", Cabin.BUSINESS),
            miles=90000,
            confidence=0.9,
            flags=[],
            provenance=[Provenance(source_name="test")],
        )
    ]
    graph = build_graph(currency, ratios, awards)
    # Parallel edges currency→aeroplan
    assert graph.number_of_edges(currency, "aeroplan") == 2

    options = rank_paths(graph, currency, 450000, portal_cpp=1.25, balance=200000)
    transfer = [o for o in options if o.kind == "transfer"]
    assert len(transfer) == 2

    bonus = next(o for o in transfer if "transfer_bonus" in o.flags or "30%" in o.label)
    base = next(o for o in transfer if o is not bonus)
    assert bonus.cpp > base.cpp
    assert bonus.source_points < base.source_points
    assert bonus.cpp == max(o.cpp for o in transfer)


def test_multi_hop_flag_when_two_transfer_hops() -> None:
    """Program→program edge compounds; path gets multi_hop flag."""
    currency = "capital_one"
    ratios = [
        TransferRatio(from_currency=currency, to_program="aeroplan", ratio=1.0),
    ]
    # Manually build a 2-hop: C1 → aeroplan → turkish → SEAT
    from mileage.graph.build import SEAT_NODE
    import networkx as nx

    g = nx.MultiDiGraph()
    g.add_node(currency, kind="currency")
    g.add_node("aeroplan", kind="program")
    g.add_node("turkish", kind="program")
    g.add_node(SEAT_NODE, kind="seat")
    g.add_edge(currency, "aeroplan", key="base", ratio=1.0, confidence=1.0, flags=[], provenance=None)
    g.add_edge(
        "aeroplan",
        "turkish",
        key="partner",
        ratio=1.0,
        confidence=0.8,
        flags=["program_transfer"],
        provenance=None,
    )
    g.add_edge(
        "turkish",
        SEAT_NODE,
        key="award",
        miles=45000,
        confidence=0.9,
        flags=[],
        provenance=None,
        seats_available=None,
    )
    options = rank_paths(g, currency, 90000, portal_cpp=1.25, balance=100000)
    multi = [o for o in options if o.kind == "transfer" and "multi_hop" in o.flags]
    assert len(multi) == 1
    assert "Aeroplan" in multi[0].label and "Turkish" in multi[0].label
