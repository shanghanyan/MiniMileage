# Mileage

A points-to-flights optimizer. North star: an "Expedia for points" that, across
many cards and programs, tells you how to convert miles into the right seat —
cheapest, fastest, most comfortable. This repo is the **Phase 0 vertical slice**:
one transferable currency end-to-end (**Capital One → Star Alliance partners →
award charts**), CLI-only, single-user, no web stack, no scraper, no "Brain".

See `Cursor-Mileage-Plan.md` for the full staged architecture.

## What Phase 0 does

Given a route, cabin, currency, and balance, it computes a **verified verdict**
with provenance and confidence:

- `portal_only` — no transfer beats your Capital One portal floor.
- `comparable` — best transfer is within 20% of the portal floor.
- `best` / `tentative_best` — a transfer beats the portal by ≥20%.

It is built to be **honest**: a datum enters the graph only with verifiable
provenance (no hallucinations), and "just use your portal" is a valid answer.

The verification / provenance / verdict skeleton is real and working; the data feeding it (cash fares and award space) is still stubbed and flagged as such.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt        # or: pip install -e .
```

## Run

```bash
# Both demos side by side
python -m mileage.cli demo

# Demo A — honesty: expect portal_only / comparable
python -m mileage.cli quote --from LAX --to JFK --cabin economy \
    --currency capital_one --miles 20000 --card venture_x

# Demo B — value: expect best, flagged no_live_space
python -m mileage.cli quote --from LAX --to IST --cabin business \
    --currency capital_one --miles 90000 --card venture_x

# Machine-readable
python -m mileage.cli quote --from LAX --to IST --cabin business --miles 90000 --json
```

Phase 0 runs with **zero API keys**. To use live cash fares, set
`AMADEUS_CLIENT_ID` / `AMADEUS_CLIENT_SECRET`; otherwise the curated fallback
fares in `mileage/knowledge/fares.yaml` are used (flagged `hardcoded_fallback`).

## Architecture (the inverted structure, §4)

```
mileage/
  domain/      # pure, no I/O: models, ratios, charts, cpp, verdict
  providers/   # one interface, many sources; curated (default) + amadeus + stubs
    aggregator/  # Engine A — Phase 1 placeholder
    brain/       # Engine B — QUARANTINED, empty in Phase 0
  verify/      # crosscheck, trust, freshness, bounds (no-hallucination rules)
  graph/       # NetworkX CPP-by-product model + ranking
  store/       # Repository (SQLite) + Cache/RateLimiter/Lock interfaces
  knowledge/   # versioned ratios.yaml / charts.yaml / fares.yaml / sources.yaml
  cli.py  config.py
```

`domain/` and `verify/` never import from `providers/`. The storage interfaces
exist from Phase 0 so the multi-user Redis/Turso move (Phase 4) is an adapter
swap, not a rewrite.

## Load-bearing fact

**Capital One does not transfer directly to United MileagePlus.** That absence
is structural — there is no `united` entry in `knowledge/ratios.yaml` — so no
United path can ever be fabricated. Value is found by routing through Star
Alliance partners and compounding cents-per-point.
# MiniMileage
