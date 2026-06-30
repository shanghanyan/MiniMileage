# Mileage

A points-to-flights optimizer. North star: an "Expedia for points" that, across
many cards and programs, tells you how to convert miles into the right seat —
cheapest, fastest, most comfortable. This repo is the working vertical slice:
one transferable currency end-to-end (**Capital One → Star Alliance partners →
award space**), CLI-only, single-user, no web stack, no "Brain".

**Phases shipped: 0 (vertical slice) + 1 (Aggregator / Engine A) + 2 (federation
hardening) + 3 (UI + API) + 4 (multi-user + shared memory layer).** Award space
is real scraped data; the provider registry enforces quota guards, 2-day cache
cadence, and ordered fallbacks. The beige web app runs the same pipeline as the
CLI via FastAPI. The memory layer is swappable to Redis/Upstash for a hosted,
multi-user service with a shared cache, a global quota counter, and per-user
balances. See `Cursor-Mileage-Plan.md` for the full staged architecture.

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

## What Phase 2 adds — provider federation hardening

The registry is now production-shaped for free-tier APIs (§5):

- **Ordered fallbacks** — `knowledge/providers.yaml` defines trust, monthly
  quotas, and per-layer trust (e.g. Travelpayouts cached fares at 0.6 beat
  curated hardcoded fares at 0.25; Amadeus live fares at 0.9 beat both).
- **Quota guards** — SQLite tracks per-provider monthly usage; exhausted
  providers are skipped and the next fallback is tried (never crashes).
- **2-day cache cadence** — cache hits cost **zero quota**; interactive
  re-runs within the TTL are free (~15 calls/route/month per provider).
- **Monthly URL-rot check** — `sources --validate-urls` persists health to
  SQLite and skips re-probing within 30 days unless `--force`.
- **Graceful degradation demo** — disable or exhaust a provider mid-run and
  both canonical demos still pass, flagged honestly (`single_source`,
  `no_live_space`, `hardcoded_fallback` as appropriate).

```bash
# Provider health, quota used/remaining, cache stats
python -m mileage.cli providers

# Phase 2 demo: cache hits, disable aggregator, exhaust quota -> fallback
python -m mileage.cli demo-degrade

# Disable specific providers (comma-separated)
MILEAGE_DISABLE_PROVIDERS=aggregator,amadeus python -m mileage.cli demo

# Monthly health check (skip if checked within 30d; --force to re-probe)
python -m mileage.cli sources --validate-urls
python -m mileage.cli sources --validate-urls --force
```

## What Phase 3 adds — UI + API (single-user)

FastAPI orchestrator exposing the real pipeline to the beige web app:

- **`POST /redemptions`** — start a quote run; returns `run_id`.
- **`GET /status/{run_id}`** — poll pipeline progress through the 4 steps
  (Route → Gathering → Cross-check → Redemptions) and fetch the verdict.
- **`GET /freshness`** — provider health, cache TTL, and aggregator source checks.

The Vite/React app in `ui/` mirrors `mileage-ui-mockup.html` and polls the API.
Demo A and Demo B presets run in the browser with the same honesty rules as the CLI.

```bash
# Terminal 1 — API on :8000
pip install -e .
uvicorn mileage.api.app:app --reload --port 8000

# Terminal 2 — UI on :5173 (proxies to :8000)
cd ui && npm install && npm run dev
```

Open http://localhost:5173 — use **Demo A** for the portal floor verdict, **Demo B**
for the gold-highlighted best transfer path.

```bash
# Phase 3 API tests
python tests/test_phase3.py
```

## What Phase 4 adds — multi-user + shared memory layer

The Phase 0 storage seams (`Cache` / `RateLimiter` / `Lock` / `QuotaGuard`) are
now assembled into one swappable `StoreBundle` (`store/stores.py`) and the API
holds **one shared registry** for its lifetime. That single change makes the
multi-user properties real (§9):

- **Shared hot cache** — most market data (charts, ratios, fares, award space)
  is user-independent, so one user's lookup serves the rest. Concurrent users on
  the same route trigger **one scrape, both served from cache** (the registry
  populates the cache *inside* the de-dupe lock; a concurrent waiter reads it
  instead of re-scraping).
- **Global quota counter** — the free-tier budget caps *your key* across *all*
  users, so the counter is charged once per live fetch, not once per user.
- **Per-user balances** — `balances`, `card`, and `preferences` are the only
  user-scoped data; they live in the `Repository` and verdicts are computed
  against *each user's own* holdings, never the request body.
- **Bearer auth** (`api/auth.py`) — with `MILEAGE_AUTH=1` the bearer token is
  the user id and balances are loaded server-side (Supabase Auth / Clerk are the
  production swap; both ultimately hand the backend a verified user id).
- **Background job queue** (`store/jobs.py`) — a worker pool warms the shared
  cache off the request path.
- **Redis/Upstash backend** (`store/redis_impl.py`) — `RedisCache`,
  `RedisRateLimiter` (atomic token-bucket Lua), `RedisLock` (`SETNX` + wait),
  and `RedisQuotaGuard` (atomic global counter). It's an **adapter swap, not a
  rewrite**: set `MILEAGE_REDIS_URL` and the same callers use Redis. If the
  server is unreachable, it logs and falls back to the in-process stores, so
  local runs need nothing extra.

```bash
# Phase 4 demo: two users, one route -> one scrape, both served from cache,
# shared global quota counter, and per-user verdicts (alice 30k -> portal_only,
# bob 90k -> best via Turkish).
python -m mileage.cli demo-multiuser

# Multi-user API: bearer token = user id; balances loaded server-side.
MILEAGE_AUTH=1 uvicorn mileage.api.app:app --port 8000
curl -X PUT localhost:8000/users/bob \
  -H 'content-type: application/json' \
  -d '{"card":"venture_x","balances":{"capital_one":90000}}'
curl -X POST localhost:8000/redemptions \
  -H 'authorization: Bearer bob' -H 'content-type: application/json' \
  -d '{"origin":"LAX","dest":"IST","cabin":"business"}'

# Swap the memory layer to Redis/Upstash (optional; graceful fallback if down).
pip install -e .[multiuser]
MILEAGE_REDIS_URL=redis://localhost:6379/0 python -m mileage.cli demo-multiuser

# Phase 4 tests (Redis adapters exercised via fakeredis if installed)
python tests/test_phase4.py
```

In the browser, the UI picks up an optional `?token=alice` query param (or
`VITE_API_TOKEN`) and sends it as a bearer token — open two tabs with
`?token=alice` and `?token=bob` against an `MILEAGE_AUTH=1` API to watch each
user get a verdict against their own balances.

## What Phase 8 adds — discovery intake (email) + local extractor

The aggregator now has a second intake mode (§6.1): the mailbox
`occulosequor@gmail.com` is a standing feed. `mileage discover` polls unread
mail over IMAP (App Password only — no Gmail API/OAuth), takes each body as a
document, and runs it through a **local, keyless** deterministic extractor
(`providers/aggregator/extract/`) that turns prose into chart rows. Every
number passes a **verbatim-grounding guard** — a `miles` value that doesn't
appear literally in the source is dropped — and a `"<program> devaluation"`
subject flips that program's charts to `stale`. Extracted rows are persisted to
`knowledge/discovered_charts.json` and resolved by the aggregator through the
**same `_build_charts → verify → graph` path** as scraped URLs, flagged
`llm_extracted` (so they can only ever be `tentative_best`, never `best`, until
an independent source confirms). The extractor sits behind an `LLMExtractor`
interface, so a local Qwen/Ollama backend is a drop-in later — the grounding
guard gates the output either way.

```bash
# Set GMAIL_ADDRESS + GMAIL_APP_PASSWORD in .env (App Password, no OAuth).
# With no creds (or MILEAGE_OFFLINE=1) it reads the bundled .eml fixtures.
python -m mileage.cli discover            # poll, extract, persist
python -m mileage.cli discover --dry-run  # show extracted rows, write nothing
```

## Offline / deterministic mode

`MILEAGE_OFFLINE=1` pins the aggregator to its `file://` fixtures — no live
HTTP, no Wayback. The test suite sets this automatically (`tests/conftest.py`
for pytest, plus a one-line guard in each standalone test), so the suite is
**hermetic and can't hang on a blocked network** — the failure mode where live
URLs in `sources.yaml` made each fetch block on a 10s timeout under the
politeness backoff. A transient probe failure (status 0) no longer marks a live
source permanently dead in `--validate-urls`; only a real 404/410 does.

```bash
python tests/test_phase8.py      # discovery intake + extractor (offline)
MILEAGE_OFFLINE=1 python -m mileage.cli demo
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

Phase 0 runs with **zero API keys**. Without Amadeus keys the fare chain is
**Travelpayouts cached** → curated hardcoded fallback. Set
`AMADEUS_CLIENT_ID` / `AMADEUS_CLIENT_SECRET` for live fares.

## Architecture (the inverted structure, §4)

```
mileage/
  domain/      # pure, no I/O: models, ratios, charts, cpp, verdict
  providers/   # federation: aggregator + curated + amadeus + travelpayouts + stubs
    federation.py  # loads knowledge/providers.yaml (trust, quota, layer order)
    aggregator/    # Engine A — real scraper
    brain/         # Engine B — QUARANTINED
  verify/      # crosscheck, trust, freshness, bounds
  graph/       # NetworkX CPP-by-product model + ranking
  store/       # Repository (SQLite) + quota guard + Cache/RateLimiter/Lock
    stores.py    # StoreBundle: the swappable memory layer, assembled in one place
    inproc.py    # in-process impls (Phase 0-3 default)
    redis_impl.py # Redis/Upstash impls (Phase 4 multi-user)
    jobs.py      # background job queue (off-request scrape refresh)
  knowledge/   # ratios, charts, fares, sources, providers, travelpayouts_cache
  api/         # FastAPI orchestrator (Phase 3) + bearer auth (Phase 4)
  cli.py  config.py  serialize.py
ui/            # Vite + React web app (Phase 3)
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
