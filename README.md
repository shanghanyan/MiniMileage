# Mileage

A points-to-flights optimizer. North star: an "Expedia for points" that, across
many cards and programs, tells you how to convert miles into the right seat —
cheapest, fastest, most comfortable. This repo is the working vertical slice:
one transferable currency end-to-end (**Capital One → Star Alliance partners →
award space**), CLI-only, single-user, no web stack, no "Brain".

**Phases shipped: 0 (vertical slice) + 1 (the Aggregator / Engine A).** Award
space is now real scraped data, normalized to the same `AwardQuote` contract as
every API provider. See `Cursor-Mileage-Plan.md` for the full staged
architecture.

## What Phase 0 does

Given a route, cabin, currency, and balance, it computes a **verified verdict**
with provenance and confidence:

- `portal_only` — no transfer beats your Capital One portal floor.
- `comparable` — best transfer is within 20% of the portal floor.
- `best` / `tentative_best` — a transfer beats the portal by ≥20%.

It is built to be **honest**: a datum enters the graph only with verifiable
provenance (no hallucinations), and "just use your portal" is a valid answer.

The verification / provenance / verdict skeleton is real and working. As of
Phase 1, **award space is real scraped data** (see below); cash fares come from
Amadeus when a key is set, otherwise a curated fallback flagged as such.

## What Phase 1 adds — the Aggregator (Engine A)

Engine A is the **default Layer 3 (award space) + Layer 4 (charts) source**. It
is a real scraper, not a stub:

- **Resilient fetch stack** (`providers/aggregator/fetch.py`): `httpx` for plain
  pages, optional `curl_cffi` TLS/JA4 impersonation, and a **Wayback Machine**
  snapshot fallback on `403/429/5xx`/network error. `file://` targets are served
  from disk so the *same* parse path runs offline and deterministically.
- **Adaptive politeness** (`politeness.py`): a per-domain throttle that backs off
  on `429` and recovers on `200`, with jitter and source rotation. Scheduling
  efficiency, not evasion; behind an interface so the Phase 4 Redis-shared
  limiter is an adapter swap.
- **Parsers** (`parse.py`): HTML tables, JSON award-space, and RSS feeds → one
  normalized row shape. A datum is produced **only when a selector actually
  hits** (anti-hallucination, §2.1).
- **Targets** (`knowledge/sources.yaml` + `knowledge/fixtures/`): an ordered,
  trust-weighted list. The seeded `file://` fixtures stand in for public pages;
  swapping one for a real `https://` URL is a config change, not a code change.
- **Carried-over fixes:** round-trip→one-way normalization, freshness de-dupe,
  `--validate-urls` + `last_404` URL-rot health check, and trust-weighted
  cross-check across genuinely independent sources.

Because scraped quotes carry their own provenance, the verification core now has
**independent sources to cross-check**, and **live award space takes precedence
over static charts** (§2.5): a confirmed seat clears the `no_live_space` caveat.

```bash
# Inspect Engine A's targets and run the URL-rot health check
python -m mileage.cli sources --validate-urls

# Fall back to curated-only (Engine A off) — graceful degradation
MILEAGE_NO_AGGREGATOR=1 python -m mileage.cli demo
```

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

# Demo B — value: expect best, with verified live award space (seats shown)
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
  providers/   # one interface, many sources; aggregator + curated + amadeus + stubs
    aggregator/  # Engine A — real scraper (fetch + politeness + parse + sources)
    brain/       # Engine B — QUARANTINED, empty
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
