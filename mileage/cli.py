"""mileage CLI — answer a route end-to-end, no web stack needed (§4, §12 Phase 0).

    mileage quote --from LAX --to JFK --cabin economy \
        --currency capital_one --miles 20000 --card venture_x

    mileage demo            # run Demo A (honesty) and Demo B (value) side by side

Prints a verified verdict with provenance and confidence. Runs with zero API
keys: the curated provider supplies charts/ratios and a fallback fare, and live
providers self-disable, so the slice always produces an honest answer.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Optional

from .config import (
    Config,
    DEFAULT_CURRENCY,
    build_registry,
    build_repository,
    partner_programs,
)
from .domain.models import (
    AwardQuote,
    Cabin,
    FareQuote,
    Layer,
    Route,
    TransferRatio,
    User,
    Verdict,
    VerdictLabel,
)
from .domain.verdict import conclude_winner
from .graph.build import build_graph
from .graph.optimize import rank_paths
from .providers.base import Query
from .providers.registry import ProviderRegistry
from .store.repo import Repository
from .verify.crosscheck import verify_award_quotes, verify_fare


# --------------------------------------------------------------------------- #
# Orchestration: query -> providers -> verify -> graph -> conclude (§3)
# --------------------------------------------------------------------------- #
def run_quote(
    route: Route,
    user: User,
    currency: str,
    *,
    registry: ProviderRegistry,
    repo: Optional[Repository] = None,
    config: Optional[Config] = None,
) -> dict:
    config = config or Config.from_env()
    balance = user.balances.get(currency, 0)

    # L2 cash fare — the price-to-beat.
    fare_quotes = [
        q
        for q in registry.fetch(Query(route, Layer.FARES, currency))
        if isinstance(q, FareQuote)
    ]
    vfare = verify_fare(fare_quotes)

    # L4 ratios + chart-derived award costs.
    chart_quotes = registry.fetch(
        Query(route, Layer.CHARTS, currency, programs=partner_programs(config))
    )
    ratios = [q for q in chart_quotes if isinstance(q, TransferRatio)]
    award_quotes = [q for q in chart_quotes if isinstance(q, AwardQuote)]
    vawards = verify_award_quotes(award_quotes)

    if vfare is None:
        return {
            "route": route,
            "verdict": None,
            "error": "no_fare",
            "message": (
                "No verified cash fare (price-to-beat) found for this route. "
                "Configure AMADEUS_CLIENT_ID/SECRET or add it to "
                "knowledge/fares.yaml. Cannot compute cents-per-point honestly."
            ),
        }

    graph = build_graph(currency, ratios, vawards)
    options = rank_paths(
        graph,
        currency,
        vfare.cash_cents,
        portal_cpp=user.portal_cpp(),
        balance=balance,
        fare_confidence=vfare.confidence,
        fare_flags=vfare.flags,
    )
    portal = next(o for o in options if o.kind == "portal")
    transfers = [o for o in options if o.kind == "transfer"]
    verdict = conclude_winner(route, portal, transfers)

    if repo is not None:
        for a in vawards:
            repo.put_edge(
                {
                    "route_key": route.key(),
                    "program": a.program,
                    "miles": a.miles,
                    "confidence": a.confidence,
                    "flags": a.flags,
                }
            )
        repo.record_run(
            {
                "route_key": route.key(),
                "verdict": verdict.label.value,
                "currency": currency,
                "miles_held": balance,
                "fare_cents": vfare.cash_cents,
            }
        )

    return {
        "route": route,
        "verdict": verdict,
        "fare": vfare,
        "awards": vawards,
        "user": user,
        "currency": currency,
    }


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
_LABEL_BLURB = {
    VerdictLabel.PORTAL_ONLY: "PORTAL IS YOUR FLOOR",
    VerdictLabel.COMPARABLE: "COMPARABLE — transfer roughly ties the portal",
    VerdictLabel.BEST: "TRANSFER WINS",
    VerdictLabel.TENTATIVE_BEST: "TRANSFER WINS (tentative — see flags)",
}


def render(result: dict) -> str:
    route: Route = result["route"]
    if result.get("verdict") is None:
        return f"[{route.key()}] {result.get('message', 'no verdict')}"

    verdict: Verdict = result["verdict"]
    fare = result["fare"]
    user: User = result["user"]
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append(
        f"  {route.origin} -> {route.dest}  ·  {route.cabin.value}  ·  "
        f"{user.balances.get(result['currency'], 0):,} {result['currency']} "
        f"({user.card})"
    )
    lines.append("=" * 68)
    lines.append(f"  Price to beat: ${fare.cash_cents / 100:,.0f} "
                 f"[{', '.join(fare.flags) or 'live'}] "
                 f"conf={fare.confidence:.2f}")
    lines.append("")
    lines.append(f"  VERDICT: {verdict.label.value}  —  {_LABEL_BLURB[verdict.label]}")
    lines.append(f"  {verdict.rationale}")
    if verdict.flags:
        lines.append(f"  flags: {', '.join(verdict.flags)}")
    lines.append("")
    lines.append("  Ranked redemptions (cents per point):")
    lines.append("  " + "-" * 64)
    for o in verdict.options:
        marker = "*" if (verdict.best_transfer and o is verdict.best_transfer) else " "
        if o.kind == "portal":
            marker = "#" if verdict.label == VerdictLabel.PORTAL_ONLY else marker
        afford = "" if o.affordable else "  (need more points)"
        lines.append(
            f"  {marker} {o.cpp:5.2f}c/pt  {o.label:<28} "
            f"{o.source_points:>7,} pts  conf={o.confidence:.2f}{afford}"
        )
        if o.flags:
            lines.append(f"        flags: {', '.join(o.flags)}")
    lines.append("  " + "-" * 64)
    lines.append("  (* best affordable transfer   # portal floor wins)")
    return "\n".join(lines)


def _verdict_to_jsonable(result: dict) -> dict:
    route: Route = result["route"]
    out: dict = {"route": route.key()}
    verdict: Optional[Verdict] = result.get("verdict")
    if verdict is None:
        out["error"] = result.get("error")
        out["message"] = result.get("message")
        return out
    out["verdict"] = verdict.label.value
    out["rationale"] = verdict.rationale
    out["flags"] = verdict.flags
    out["fare_cents"] = result["fare"].cash_cents
    out["options"] = [
        {
            "label": o.label,
            "kind": o.kind,
            "cpp": o.cpp,
            "source_points": o.source_points,
            "affordable": o.affordable,
            "confidence": o.confidence,
            "flags": o.flags,
        }
        for o in verdict.options
    ]
    return out


# --------------------------------------------------------------------------- #
# Demos (§12 Phase 0)
# --------------------------------------------------------------------------- #
DEMOS = {
    "A": {
        "title": "Demo A — Honest floor (correctness)",
        "route": ("LAX", "JFK", "economy"),
        "miles": 20000,
        "card": "venture_x",
        "expect": "portal_only or comparable",
    },
    "B": {
        "title": "Demo B — Hidden value (moat)",
        "route": ("LAX", "IST", "business"),
        "miles": 90000,
        "card": "venture_x",
        "expect": "best (flagged no_live_space)",
    },
}


def run_demo(registry: ProviderRegistry, repo: Repository, config: Config) -> int:
    for key in ("A", "B"):
        spec = DEMOS[key]
        o, d, cabin = spec["route"]
        route = Route(o, d, Cabin(cabin))
        user = User(
            balances={DEFAULT_CURRENCY: spec["miles"]}, card=spec["card"]
        )
        print(f"\n{spec['title']}   (expected: {spec['expect']})")
        result = run_quote(
            route, user, DEFAULT_CURRENCY,
            registry=registry, repo=repo, config=config,
        )
        print(render(result))
    print()
    return 0


# --------------------------------------------------------------------------- #
# Argparse
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mileage", description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    q = sub.add_parser("quote", help="quote one route end-to-end")
    q.add_argument("--from", dest="origin", required=True)
    q.add_argument("--to", dest="dest", required=True)
    q.add_argument(
        "--cabin",
        default="economy",
        choices=[c.value for c in Cabin],
    )
    q.add_argument("--currency", default=DEFAULT_CURRENCY)
    q.add_argument("--miles", type=int, required=True)
    q.add_argument("--card", default="venture_x", choices=["venture", "venture_x"])
    q.add_argument("--json", action="store_true", help="machine-readable output")

    sub.add_parser("demo", help="run Demo A and Demo B side by side")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    registry = build_registry(config)
    repo = build_repository(config)

    try:
        if args.command == "demo":
            return run_demo(registry, repo, config)

        if args.command == "quote":
            route = Route(args.origin, args.dest, Cabin(args.cabin))
            user = User(
                balances={args.currency: args.miles}, card=args.card
            )
            result = run_quote(
                route, user, args.currency,
                registry=registry, repo=repo, config=config,
            )
            if args.json:
                print(json.dumps(_verdict_to_jsonable(result), indent=2))
            else:
                print(render(result))
            return 0
    finally:
        repo.close()

    return 1


if __name__ == "__main__":
    sys.exit(main())
