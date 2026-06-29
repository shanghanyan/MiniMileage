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
from typing import Callable, Literal, Optional

from .config import (
    Config,
    DEFAULT_CURRENCY,
    build_registry,
    build_repository,
    load_federation,
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
from .serialize import quote_result_to_dict
from .providers.registry import ProviderRegistry
from .store.repo import Repository
from .verify.crosscheck import verify_award_quotes, verify_fare


# --------------------------------------------------------------------------- #
# Orchestration: query -> providers -> verify -> graph -> conclude (§3)
# --------------------------------------------------------------------------- #
PipelineStep = Literal["route", "gathering", "crosscheck", "redemptions"]


def run_quote(
    route: Route,
    user: User,
    currency: str,
    *,
    registry: ProviderRegistry,
    repo: Optional[Repository] = None,
    config: Optional[Config] = None,
    on_step: Optional[Callable[[PipelineStep], None]] = None,
) -> dict:
    config = config or Config.from_env()
    balance = user.balances.get(currency, 0)

    if on_step:
        on_step("route")

    if on_step:
        on_step("gathering")

    # L2 cash fare — the price-to-beat.
    fare_quotes = [
        q
        for q in registry.fetch(Query(route, Layer.FARES, currency))
        if isinstance(q, FareQuote)
    ]

    programs = partner_programs(config)

    # L4 ratios + chart-derived award costs.
    chart_quotes = registry.fetch(
        Query(route, Layer.CHARTS, currency, programs=programs)
    )
    ratios = [q for q in chart_quotes if isinstance(q, TransferRatio)]
    award_quotes = [q for q in chart_quotes if isinstance(q, AwardQuote)]

    # L3 live award space (Engine A / seats.aero). Pooled with chart quotes so
    # the verification core applies live precedence + cross-check (§2.5, §7).
    award_quotes += [
        q
        for q in registry.fetch(Query(route, Layer.AWARD, currency, programs=programs))
        if isinstance(q, AwardQuote)
    ]
    if on_step:
        on_step("crosscheck")

    vfare = verify_fare(fare_quotes)
    vawards = verify_award_quotes(award_quotes)

    if on_step:
        on_step("redemptions")

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
    awards = result.get("awards") or []
    live = [a for a in awards if a.seats_available is not None]
    if live:
        seat_bits = ", ".join(
            f"{a.program} {a.miles:,}mi ({a.seats_available} seats)" for a in live
        )
        lines.append(f"  Live award space: {seat_bits}")
    elif awards:
        lines.append(f"  Award space: chart-only (no live seat) — {len(awards)} program(s)")
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
        "expect": "best (live award space verified)",
    },
}


def run_sources(
    config: Config,
    repo: Repository,
    *,
    validate: bool = False,
    force: bool = False,
) -> int:
    """List Engine A targets; optionally run the monthly URL-rot health check."""
    from .providers.aggregator import AggregatorProvider

    federation = load_federation(config)
    agg = AggregatorProvider(
        sources_path=config.sources_path,
        knowledge_dir=config.knowledge_dir,
        enabled=config.aggregator_enabled,
        health_repo=repo,
    )
    if validate:
        agg.validate_urls(
            force=force,
            max_age_days=federation.health_check_days,
        )
    print(f"Engine A targets ({len(agg.targets)})  ·  provider health: "
          f"{agg.health().value}")
    print("-" * 68)
    for t in agg.targets:
        status = ""
        if t.last_checked:
            mark = "ok" if t.healthy() else "DEAD"
            status = (
                f"  [{mark} status={t.last_status} last_404={t.last_404} "
                f"checked={t.last_checked[:10]}]"
            )
        print(
            f"  {t.trust:.2f}  {t.provides:<6} {t.format:<10} {t.name}{status}"
        )
        print(f"            {t.url}")
    print("-" * 68)
    return 0


def run_providers(registry: ProviderRegistry) -> int:
    """Show federated provider health, quota, and cache cadence."""
    federation = registry.federation
    ttl_days = (registry.ttl_seconds or 0) / 86400
    print(f"Provider federation  ·  cache TTL {ttl_days:.0f}d  ·  "
          f"health check every {federation.health_check_days if federation else '?'}d")
    print("-" * 68)
    for row in registry.provider_status():
        q = ""
        if row["monthly_limit"] is not None:
            q = f"  quota {row['used']}/{row['monthly_limit']}"
            if row["remaining"] is not None and row["remaining"] <= 0:
                q += " EXHAUSTED"
        dis = " DISABLED" if row["disabled"] else ""
        print(
            f"  {row['trust']:.2f}  {row['health']:<9} {row['name']:<16} "
            f"[{', '.join(row['layers'])}]{q}{dis}"
        )
    stats = registry.stats
    print("-" * 68)
    print(f"  last run: {stats.cache_hits} cache hits, "
          f"{stats.cache_misses} misses, {stats.quota_skips} quota skips")
    return 0


def run_demo_degrade(
    registry: ProviderRegistry,
    repo: Repository,
    config: Config,
) -> int:
    """Phase 2 demo: disable or exhaust a provider -> fallback, both demos pass."""
    from .store.sqlite_repo import SQLiteRepository

    assert isinstance(repo, SQLiteRepository)

    federation = load_federation(config)
    route_b = Route("LAX", "IST", Cabin.BUSINESS)
    user_b = User(balances={DEFAULT_CURRENCY: 90000}, card="venture_x")

    print("\n=== Phase 2 — federation hardening demo ===\n")

    # 1) Warm cache: first run = misses, second = hits (zero quota on hits).
    registry.reset_stats()
    run_quote(route_b, user_b, DEFAULT_CURRENCY, registry=registry, repo=repo, config=config)
    miss_stats = registry.stats
    registry.reset_stats()
    run_quote(route_b, user_b, DEFAULT_CURRENCY, registry=registry, repo=repo, config=config)
    hit_stats = registry.stats
    print("1) Cache cadence (~2-day TTL)")
    print(f"   first run:  {miss_stats.cache_misses} cache misses")
    print(f"   second run: {hit_stats.cache_hits} cache hits, "
          f"{hit_stats.cache_misses} misses (hits cost zero quota)")

    # 2) Disable aggregator -> curated-only award data, verdict still computes.
    registry.disabled.add("aggregator")
    registry.reset_stats()
    result = run_quote(
        route_b, user_b, DEFAULT_CURRENCY,
        registry=registry, repo=repo, config=config,
    )
    registry.disabled.discard("aggregator")
    verdict = result["verdict"]
    winner_flags = verdict.best_transfer.flags if verdict.best_transfer else []
    print("\n2) Aggregator disabled -> curated fallback")
    print(f"   verdict: {verdict.label.value}")
    print(f"   winner flags: {', '.join(winner_flags) or '(none)'}")
    assert verdict.label.value in ("best", "tentative_best"), "Demo B must still pass"
    assert "no_live_space" in winner_flags, "degraded to chart-only without Engine A"

    # 3) Exhaust travelpayouts quota -> fare falls back to curated floor.
    tp_limit = federation.monthly_quota("travelpayouts")
    if tp_limit is not None:
        repo.quota_exhaust("travelpayouts", tp_limit)
    registry.cache.clear()
    registry.reset_stats()
    route_a = Route("LAX", "JFK", Cabin.ECONOMY)
    user_a = User(balances={DEFAULT_CURRENCY: 20000}, card="venture_x")
    result3 = run_quote(
        route_a, user_a, DEFAULT_CURRENCY,
        registry=registry, repo=repo, config=config,
    )
    if tp_limit is not None:
        repo.quota_reset("travelpayouts")
    fare_flags = result3["fare"].flags if result3.get("fare") else []
    print("\n3) Travelpayouts quota exhausted -> curated fare fallback")
    print(f"   quota skips this run: {registry.stats.quota_skips}")
    print(f"   fare flags: {', '.join(fare_flags) or '(none)'}")
    assert "hardcoded_fallback" in fare_flags, "must fall back to curated fares"

    # 4) Both canonical demos still pass end-to-end.
    print("\n4) Canonical demos (must still pass)")
    run_demo(registry, repo, config)
    return 0


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

    sub.add_parser(
        "demo-degrade",
        help="Phase 2: show cache hits, provider disable, quota fallback",
    )

    p = sub.add_parser("providers", help="provider federation status + quota")
    p.add_argument("--json", action="store_true")

    s = sub.add_parser("sources", help="list aggregator targets (Engine A)")
    s.add_argument(
        "--validate-urls",
        action="store_true",
        help="run URL-rot health check (monthly cadence; use --force to re-probe)",
    )
    s.add_argument(
        "--force",
        action="store_true",
        help="re-probe all targets even if checked within the monthly window",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    repo = build_repository(config)
    registry = build_registry(config, repo)

    try:
        if args.command == "sources":
            return run_sources(
                config, repo,
                validate=args.validate_urls,
                force=getattr(args, "force", False),
            )

        if args.command == "providers":
            if getattr(args, "json", False):
                print(json.dumps(registry.provider_status(), indent=2))
            else:
                run_providers(registry)
            return 0

        if args.command == "demo-degrade":
            return run_demo_degrade(registry, repo, config)

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
                print(json.dumps(quote_result_to_dict(result), indent=2))
            else:
                print(render(result))
            return 0
    finally:
        repo.close()

    return 1


if __name__ == "__main__":
    sys.exit(main())
