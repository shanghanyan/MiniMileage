# Mileage — Revised Architecture & Build Plan

*A points-to-flights optimizer. North star: an "Expedia for points" that, across many cards and many programs, tells you how to convert miles into the right seat — cheapest, fastest, or most comfortable. This plan stages that vision behind one honest, working vertical slice you can ship now, and is built multi-user-ready from the first line.*

---

## 0. Goal, right-sized — and the two demos we keep

There are three goals in play. Be explicit about which one each phase serves.

- **North star (someday):** multi-card × multi-program × multi-airline, with live award space, schedules, cash-fare comparison, and limited-time transfer-bonus alerts — "Expedia, plus how to pay with points to save money / time / comfort." **Multi-user.**
- **The original mini-goal:** LAX→NYC, United, 20k Capital One miles. A domestic transcon economy redemption — the case where the **portal floor usually wins**. Weak as a *value* demo, but excellent as a *correctness/honesty* test. **We keep it.**
- **The intermediate goal we build first (the vertical slice):** **one transferable currency end-to-end — Capital One → its Star Alliance partners → award space** — delivering the thing Expedia cannot: *"portal vs. which transfer partner, at what cents-per-point, and is there actually a seat."*

**The two demos (kept side by side, both run end-to-end at every phase):**

- **Demo A — "Honest floor" (correctness).** `LAX→JFK, economy, United, 20,000 Capital One miles`. Expected verdict: **`portal_only`** or **`comparable`** — the tool says *"just use your portal, transfer doesn't beat it."* Proves the anti-hallucination + honesty rules: the product is willing to tell you *not* to bother.
- **Demo B — "Hidden value" (moat).** An international premium-cabin redemption, e.g. `LAX→IST (or SFO→NRT), business, ~80–100k Capital One miles`, routed `C1 → Turkish / Aeroplan / ANA`. Expected verdict: **`best`** at ~4–7¢/point with award space shown. Proves the value Expedia/Google Flights can't give: which partner, at what CPP, with a seat that exists.

The load-bearing domain fact carries over unchanged: **Capital One does not transfer directly to United MileagePlus.** Value is found by routing through Star Alliance partners (LifeMiles, Turkish, KrisFlyer, Aeroplan, ANA) and compounding cents-per-point across each hop.

**Decisions locked (this revision):** multi-user is a planned future phase; **lean on the aggregator first**, treat seats.aero as an *optional* paid upgrade; **keep both demos**.

---

## 1. The key reframe: data layers, not "scrapers"

The product is **not** primarily a scraping project. It is a **federation of data sources across four layers**, only one of which genuinely needs scraping today. This plan organizes around *what question each source answers*.

| Layer | Answers | Primary source | Fallbacks | Needs scraping? |
|---|---|---|---|---|
| **1. Schedules/routes** | which flights fly O→D, and when | **Amadeus Schedules** (free tier) | AeroDataBox, aviationstack | No |
| **2. Cash fares** | the price-to-beat | **Amadeus Flight Offers Search** (free tier) | Travelpayouts (free cached), Duffel (test) | No |
| **3. Award availability** | is there a saver seat in miles | **Aggregator scrape (Engine A)** | seats.aero Partner API *(optional, paid)* | **Yes — our default** |
| **4. Ratios + award charts** | how points convert; what a seat costs in miles | **Curated `knowledge/*.yaml`** + **Aggregator** | — | Refresh-only |

> **aviationstack reality check:** it is a flight *status/tracker* API — Layer 1 only, and weakly (no fares, no award space). Don't build the core on it. Its free tier (100/mo, HTTP-only) is irrelevant to the parts that matter.

> **Decision applied:** Layer 3 (award space) leans on the **aggregator first**. seats.aero stays wired as an *optional* provider behind the same interface, switched on with a key when/if you choose to pay — no rearchitecting.

The honest moat is **Layers 3 and 4** — exactly the data Expedia/Google Flights don't expose. Layers 1–2 are commodity APIs we consume, never our differentiator.

---

## 2. Design principles (the contract)

1. **No hallucinations.** A datum enters the graph only from a source that produced a *verifiable* value — an API field that parsed, or a scrape with a selector hit. Anything flagged hallucinated is never usable.
2. **Source-agnostic core.** The domain core consumes a normalized `AwardQuote`/`FareQuote`, not a scraping-shaped row. Scraping is *one* way to produce a quote.
3. **Cross-check only where sources are independent.** Two sources mirroring the *same* published chart are not independent; their "agreement" is not verification. Cross-check earns its name only across genuinely independent providers (e.g., aggregator scrape vs. seats.aero).
4. **Graceful degradation.** A useful, honest answer even if a provider — or a whole layer — returns nothing. Lower confidence, say so, never crash.
5. **Live precedence.** Live award availability overrides static zone charts when present.
6. **Honest conclusions.** Never name a winner unless verified data exists on both the portal floor and the transfer path. "Portal is your floor" is a valid, frequent answer.
7. **Provenance & freshness are first-class.** Every datum carries source, timestamp, trust weight, and age-decayed confidence.
8. **Local-first, multi-user-ready.** Runs on a laptop today as a single-user CLI; **no design choice blocks the multi-user service later.** Storage, cache, rate-limiting, and locking all sit behind interfaces (§9) so the single→multi-user move is adapter swaps, not a rewrite.

---

## 3. System topology

```
                          ┌──────────────────────────────┐
                          │          MILEAGE UI           │
                          │  (beige web app; later)        │
                          └───────────────┬───────────────┘
                                          │ HTTP/JSON (or CLI first)
                          ┌───────────────▼───────────────┐
                          │        ORCHESTRATOR            │
                          │  query → providers → verify →  │
                          │  graph → conclude              │
                          └───────────────┬───────────────┘
                                          │
        ┌─────────────────────────────────┼──────────────────────────────────┐
        │                  PROVIDER REGISTRY (federate by capability)         │
        │                                                                     │
        │  L1 Schedules     L2 Cash fares     L3 Award space   L4 Ratios/charts│
        │  ┌───────────┐    ┌────────────┐    ┌────────────┐   ┌─────────────┐ │
        │  │ Amadeus   │    │ Amadeus    │    │ AGGREGATOR │   │ curated.yaml│ │
        │  │ AeroDataB.│    │ Travelpay. │    │ (Engine A) │   │  + AGGREG.  │ │
        │  │ aviation- │    │ Duffel     │    │ ─ optional─│   │             │ │
        │  │  stack    │    │            │    │ seats.aero │   │             │ │
        │  └───────────┘    └────────────┘    └─────┬──────┘   └─────────────┘ │
        │                                           │     (Engine B "Brain"    │
        │                                           │      QUARANTINED — §8)   │
        └─────────────────────────────────┬─────────┴─────────────────────────┘
                                           │  AwardQuote[] / FareQuote[]
                          ┌────────────────▼───────────────┐
                          │        VERIFICATION CORE        │
                          │ cross-check (independent only) ·│
                          │ trust · freshness · bounds      │
                          └────────────────┬────────────────┘
                                           │ verified edges
                          ┌────────────────▼───────────────┐
                          │        GRAPH + OPTIMIZER        │
                          │ NetworkX · CPP-by-product ·     │
                          │ rank · conclude_winner (≥20%)   │
                          └────────────────┬────────────────┘
                                           │
                          ┌────────────────▼───────────────┐
                          │      MEMORY / STORAGE (§9)      │
                          │ SQLite (truth) + Cache/Lock/    │
                          │ RateLimiter interfaces          │
                          │ in-proc now → Redis/Upstash     │
                          │ when multi-user                 │
                          └─────────────────────────────────┘
```

The core consumes normalized quotes from 0..N providers identically, so any provider — or whole layer — can fail without crashing the run.

---

## 4. Repository layout (the inverted structure)

The **domain core is the spine**; every provider, engine, bandit, and observability piece is an **optional plugin you can delete without touching the core.**

```
mileage/
  domain/                  # pure, no I/O — the durable value
    ratios.py              # Capital One transfer partners + ratios
    charts.py              # partner award-chart logic (Aeroplan bands, etc.)
    cpp.py                 # cents-per-point math, per-hop compounding
    verdict.py             # conclude_winner: portal_only / comparable / best
    models.py              # AwardQuote, FareQuote, Route, User — source-agnostic
  providers/               # data sources behind ONE interface
    base.py                # Provider.capabilities(), .fetch(query) -> Quote[]
    registry.py            # federate by capability → health → remaining quota
    amadeus.py             # L1 schedules + L2 cash fares (free tier primary)
    travelpayouts.py       # L2 cached cash fares (free fallback)
    aviationstack.py       # L1 schedules (weak fallback)
    seats_aero.py          # L3 award availability (OPTIONAL, paid — off by default)
    curated.py             # L4 ratios/charts from versioned YAML
    aggregator/            # ENGINE A — real, working scraper (L3 default + L4)
      __init__.py
      fetch.py             # httpx + curl_cffi; Wayback / RSS / PDF fallbacks
      politeness.py        # adaptive throttle + source rotation (simple first)
    brain/                 # ENGINE B — QUARANTINED, see §8
      README.md            # boundaries + "do not import from core"
  verify/
    crosscheck.py          # only meaningful across INDEPENDENT providers
    trust.py  freshness.py  bounds.py
  graph/
    build.py  optimize.py  # NetworkX DiGraph, rank, conclude
  store/                   # see §9
    repo.py                # Repository: get/put edge, runs, user balances
    cache.py               # Cache, RateLimiter, Lock interfaces
    inproc.py              # in-process impls (Phase 0–3)
    redis_impl.py          # Redis/Upstash impls (Phase 4, multi-user)
    sqlite_repo.py         # durable; Turso/Supabase later, same interface
  knowledge/
    ratios.yaml            # Capital One → partners (changes ~quarterly)
    charts.yaml            # partner award charts / zone bands
    sources.yaml           # aggregator target list, ordered, with trust
  cli.py                   # answer the route end-to-end — NO web stack needed
  config.py                # provider keys, cadence, cache TTLs
```

Rules that keep this honest:
- `domain/` and `verify/` **never import** from `providers/`, `aggregator/`, or `brain/`. Dependencies point inward only.
- A bandit/policy lives **inside** a provider as a swappable strategy; invisible to the core.
- `brain/` is import-isolated; the working product never depends on it.

---

## 5. Provider federation & quota strategy

**One interface, many providers, routed by capability.** Each provider declares the layers it serves and its quota/health. The registry resolves a query by: capability match → healthy → cache-first → remaining quota → trust order. Same-capability providers are tried in order as fallbacks.

```python
class Provider(Protocol):
    name: str
    def capabilities(self) -> set[Layer]: ...        # {SCHEDULES, FARES, AWARD, CHARTS}
    def remaining_quota(self) -> int | None: ...
    def fetch(self, q: Query) -> list[Quote]: ...     # normalized output
```

**Use all of them — federated, not pooled blindly.** aviationstack, Amadeus, AeroDataBox, Travelpayouts, Duffel each fill *different* layers; combining them is complementary. Where two providers serve the *same* layer, pool them as ordered fallbacks to stretch free quota.

**Self rate-limiting (yes, easily).**
- A per-provider token-bucket limiter behind the `RateLimiter` interface (§9): in-process now, **Redis when multi-user** so the bucket is shared across workers.
- A response cache keyed by `(provider, route, date, cabin)` with TTL = refresh cadence.
- **Cadence:** refresh every ~2 days → ~15 calls/route/month per provider, well under every free tier; cache hits serve everything in between, so interactive use costs zero quota.
- A monthly quota guard: when `remaining_quota` is low, the registry skips that provider and falls back instead of erroring.

> **Multi-user caveat (important):** free-tier quotas are **global to your key**, not per-user. With many users you *must* centralize the counter (§9) — and you lean hard on the **shared cache**, since most data is user-independent (one user's lookup serves the rest).

**Recommended free starting set:**
- **Amadeus for Developers (Self-Service)** — *primary* for L1 schedules + L2 cash fares; real data, generous free quota. Start here.
- **Travelpayouts/Aviasales** — free cached cash fares; cheap "fare to beat."
- **Aggregator (Engine A)** — *default* L3 award space + L4 charts (§6).
- **AeroDataBox** (cheap) / **aviationstack** (free) — L1 fallbacks only.
- **seats.aero Partner API** — *optional* paid L3 upgrade, wired but off until you choose it.
- **Duffel** (test mode) — live offers + a real booking path later.

---

## 6. The aggregator (Engine A) — first-class, default award-space source

You want the aggregator doing real work for **anything you can't reliably get from a free API** — which, with seats.aero optional, now means **Layer 4 (charts/ratios) and Layer 3 (award space) by default.**

- **Fetch stack:** `httpx` for plain pages, `curl_cffi` (TLS/JA4 impersonation) for header/TLS-only checks, plus Wayback / RSS (feedparser) / PDF (pdfplumber) fallbacks. **No browser, no sensor-forging** — that's the Brain, quarantined.
- **Targets:** `knowledge/sources.yaml` — an ordered, trust-weighted list of *public* aggregators, blogs, RSS award feeds, PDF charts, and award-space tools that don't sit behind a heavy WAF.
- **Carried-over fixes:** adaptive per-domain throttle + backoff + jitter for 429s; `--validate-urls` + `last_404` health check for URL rot; round-trip→one-way normalization (ANA); freshness de-dupe; trust-weighted median with `sources_disagree_NN%`; source rotation → next target → Wayback on a block.
- **Lightweight adaptiveness (optional, low-risk):** a small politeness/source-rotation policy that learns the fastest non-429 delay per domain. Scheduling efficiency, **not evasion**; starts hardcoded, stays there until volume justifies more.
- **Output:** normalized `AwardQuote` with full provenance — identical contract to the API providers, so the verification core can't tell them apart.

---

## 7. Verification, graph & honesty (unchanged soul)

Per `(program, route/zone, cabin)` group:
1. **Authoritative short-circuit** — a usable row from an authoritative source (Capital One ratios, `partners.yaml`) wins outright.
2. **Independent agreement** — pool quotes from independent providers (e.g., aggregator vs. seats.aero when enabled); trust-weighted median. Spread >~10% → emit but flag `sources_disagree_NN%` + demote. *(Two mirrors of one chart ≠ independent.)*
3. **Single source** — emit at medium/low confidence with a `single_source` flag.
4. **Fallback only** — emit flagged `hardcoded_fallback`; never `recommended`.
5. **Nothing on the transfer side** — conclude `portal_only`: the portal floor (1.0¢ Venture / 1.25¢ Venture X) as the confirmed answer.

`conclude_winner`: no verified non-stale transfer path → `portal_only`; best transfer within 20% of portal → `comparable`; beats portal by ≥20% → `best` (or `tentative_best` if it carries a warning flag). Graph stays your NetworkX CPP-by-product model.

---

## 8. The Brain (Engine B) — quarantined for later

The bot-avoider is **isolated and optional**, built last, only for a source that exists *only* behind a WAF. It lives in `providers/brain/`, import-isolated; the working product never depends on it.

**Honest framing:** beating Akamai isn't an AI problem at the hard layer — TLS/JA4, the `_abck` sensor, and IP reputation are solved (or not) by `curl_cffi`/`nodriver`/`Camoufox`/proxies, not models. If ever built, the AI's only job is a contextual bandit that *orchestrates* those tools, rewarded by the real `is_usable()` selector-hit signal. Start with a hardcoded decision tree; learn later.

**Boundaries (enforced in `brain/README.md`, off-limits):** no `_abck` sensor reverse-engineering / turnkey Akamai bypass; no CAPTCHA-solving farms; no credentialed/account scraping; no high-volume hammering; no ignoring robots.txt/ToS on protected first-party sources (get real legal advice before hitting WAF'd first-party sources at scale); no malware-sourced proxies; no redistribution of scraped data against terms.

---

## 9. Memory, storage & the multi-user path (the Redis answer)

Three separable concerns, **all behind interfaces from Phase 0** so the implementation can change without touching callers:

| Concern | Interface | Phase 0–3 impl (single-user, local) | Phase 4 impl (multi-user) | Why it changes |
|---|---|---|---|---|
| Durable truth | `Repository` | **SQLite** file | Turso / Supabase (same interface) | hosting + concurrent writers |
| Hot cache | `Cache` | in-process dict + TTL | **Redis / Upstash** | shared across workers/users |
| Rate limiting | `RateLimiter` | in-process token bucket | **Redis** atomic counter | global quota shared across users |
| Coordination | `Lock` | no-op / `threading.Lock` | **Redis `SETNX`** | de-dupe concurrent scrapes |

**Why not Redis now / why Redis later.** Single-user has no concurrency, so Redis is pure overhead — SQLite + a dict cover everything. **Multi-user makes Redis genuinely load-bearing** for four reasons:

1. **Global quota is shared, not per-user.** Free tiers cap *your key* across *all* users; you need one atomic, cross-process counter — Redis's core job. SQLite counters degrade under concurrent multi-process writes.
2. **Shared hot cache, high hit rate.** Charts, ratios, fares, and award space are **user-independent**, so one user's lookup serves the rest. This makes multi-user far cheaper than it looks and keeps you under free quotas.
3. **Distributed locks.** Two users on the same route shouldn't both scrape — `SETNX` lets one fetch while others read cache.
4. **Background job queue.** Scrapes/refreshes run as workers off the request path.

**What stays user-scoped vs. shared.** Only *balances, card holdings, and preferences* carry a `user` dimension (in the `Repository`). All market data (charts/fares/award space) is shared and cached once for everyone — this is what keeps multi-user lean.

The rule: **interfaces exist from Phase 0; the Redis/Upstash + Turso/Supabase implementations land in Phase 4 (Multi-user).** One adapter swap, not a rewrite.

---

## 10. Observability (deferred, single-stack)

Skip dual Phoenix + Grafana until there's ML to observe. For Phases 0–2: structured logs + a run record in SQLite. When the aggregator's adaptive policy (or later the Brain's bandit) comes online, add **one** tracer (Arize Phoenix, OTel-native) whose spans double as the episode log, plus a **golden route set** (Demo A + Demo B + a handful more) run as CI evals so "no data without verification" becomes a failing test, not a comment.

---

## 11. Stack summary

| Layer | Choice | Free? | Cloud path |
|---|---|---|---|
| Schedules / fares APIs | Amadeus Self-Service (primary), Travelpayouts, AeroDataBox, aviationstack | ✓ tiers | same |
| Award availability | **Aggregator (default)**; seats.aero Partner API (optional) | ✓ / paid | same |
| Aggregator fetch | httpx, curl_cffi, Wayback/RSS/PDF | ✓ | same (server-side) |
| Protected fetch (Brain) | nodriver, Camoufox/Patchright — **quarantined** | ✓ | server-side only |
| Verification / graph | NetworkX + verify/ logic | ✓ | same |
| Durable store | SQLite → Turso / Supabase | ✓ | Turso / Supabase |
| Hot memory / quota / locks | in-proc → **Redis / Upstash (multi-user)** | ✓ | Upstash |
| Orchestrator | CLI → FastAPI | ✓ | Fly.io / Render / Railway |
| Frontend | `mileage-ui-mockup.html` → Vite + React | ✓ | Vercel / Cloudflare / Supabase |
| Auth (multi-user) | Supabase Auth (or Clerk) | ✓ tier | Supabase |
| Observability | logs → Phoenix when ML exists | ✓ | Phoenix Cloud |

---

## 12. Build roadmap — deliverables & demo at each stage

Both demos run end-to-end from Phase 0 onward; each phase makes them *more* trustworthy or *more* capable.

### Phase 0 — Working vertical slice (CLI, no web, no Brain)
**Build:** `domain/` core + `providers/curated.py` (ratios/charts) + `providers/amadeus.py` (cash fare to beat) + `verify/` + `graph/` + SQLite behind `Repository`; `Cache`/`RateLimiter`/`Lock` interfaces with in-process impls; `cli.py`.
**Deliverable:** `mileage quote --from LAX --to JFK --cabin economy --currency capital_one --miles 20000` prints a verified verdict with provenance and confidence.
**Demo:**
- *Demo A* runs fully → returns **`portal_only`/`comparable`** (honesty proven).
- *Demo B* runs against **curated charts only** (no live award space yet) → returns **`best` by chart math**, flagged `no_live_space`. Shows the value path, honestly caveated.

### Phase 1 — The Aggregator (Engine A)
**Build:** real scraper for L4 charts/ratios + **L3 award space** (default), with politeness/rotation + carried-over fixes; `knowledge/sources.yaml`.
**Deliverable:** the product works for routes/programs the free APIs don't cover; award space comes from real scraped data, normalized to `AwardQuote`.
**Demo:**
- *Demo B* upgraded → chart-only becomes **verified award space** (or an honest "no seat found on these dates"), and the `no_live_space` caveat clears.
- *Demo A* unchanged in verdict, now cross-checks curated vs. scraped chart.

### Phase 2 — Provider federation hardening
**Build:** multi-provider fallback ordering, quota guards, 2-day cache cadence, monthly URL/health check.
**Deliverable:** graceful degradation across providers; free tiers never exceeded; interactive use served from cache.
**Demo:** disable a provider (or exhaust its quota) mid-run → system falls back, verdict still computes with a `single_source`/degraded flag. **Both demos still pass.** This is the "checks each other but works if one fails" property, shown live.

### Phase 3 — UI + API (single-user)
**Build:** FastAPI orchestrator exposing `POST /redemptions`, `GET /status/{run_id}`, `GET /freshness`; wire `mileage-ui-mockup.html` into a Vite/React app; the 4-step stepper maps to the real pipeline.
**Deliverable:** the beige web app over the working pipeline, locally (`:5173` → `:8000`).
**Demo:** both demos run **in the browser** — watch the stepper go Route → Gathering → Cross-check → Redemptions; Demo B lights the single gold "best verified route" line, Demo A shows the calm "portal is your floor" verdict.

### Phase 4 — Multi-user + memory layer (Redis lands here) — ✅ SHIPPED
**Build:** swap `Cache`/`RateLimiter`/`Lock`/`QuotaGuard` to **Redis/Upstash** behind one `StoreBundle` (`store/stores.py`, `store/redis_impl.py`); add bearer auth (`api/auth.py`; Supabase Auth/Clerk are the production swap) and per-user balances/card holdings loaded from the `Repository`; background scrape workers + job queue (`store/jobs.py`). The API now holds **one shared registry**, and the registry populates the cache *inside* its de-dupe lock so a concurrent waiter reads cache instead of double-scraping.
**Deliverable:** a multi-user-ready service — shared market-data cache, **global quota counter shared across all users**, no double-scraping, results scoped to each user's cards. `MILEAGE_REDIS_URL` selects the Redis backend (graceful fallback to in-proc if unreachable); `Repository` stays SQLite behind the same interface (Turso/Supabase is the hosting swap, no caller changes).
**Demo (`mileage demo-multiuser`):** two users hit `LAX→IST` concurrently → **one scrape, both served from cache** (4 live fetches + 4 cache hits); the global quota counter is charged once; each user sees the verdict computed against *their own* balances (alice 30k C1 → `portal_only`; bob 90k C1 → `best` via Turkish). Covered by `tests/test_phase4.py`.

### Phase 5 — Observability + evals — ✅ SHIPPED
**Build:** Arize AX / OpenInference OTel tracing (`mileage/obs.py`) — every redemption run is a CHAIN span (`quote`) with a RETRIEVER/TOOL child per provider fetch, then `verify` + `optimize` spans, so the trace *is* the episode log. Tracing is optional + purely additive: with no `arize-otel`/`opentelemetry` deps or creds it is a no-op and the pipeline is unchanged (`pip install -e ".[observability]"` to enable; `ARIZE_SPACE_ID`+`ARIZE_API_KEY` → Arize AX, or `MILEAGE_TRACE_CONSOLE=1` → local console). The golden route set (Demo A + Demo B + honesty extras) + the anti-hallucination guards live in `mileage/evals.py`; `mileage eval` runs them offline/deterministically (live providers self-disable, the aggregator is pinned to its fixtures) and **exits non-zero on any failure**, so "no data without verification" is a failing build.
**Deliverable:** every run is traceable; the §2.1/§7 honesty rules — no unsourced datum, no out-of-bounds value, a flagged winner is `tentative_best` not `best`, and Capital One → United never enters the graph — are enforced as CI evals (`tests/test_phase5.py`).
**Demo (`mileage demo-observability`):** reports the tracing backend, runs the golden set (5/5 → BUILD OK), then feeds verification a **stale/garbage chart** (5-mile business seat, an unsourced row, a 2021 chart) → the garbage is dropped on bounds, the unsourced row on missing provenance, and the stale row is flagged + demoted; a clean control survives. If a hallucination ever slipped through, the eval **fails the build**; with tracing on, the `verify:anti-hallucination` span shows exactly where it was rejected.

### Phase 6 — The Brain (sandboxed, optional)
**Build:** only if a needed source exists *only* behind a WAF — strategy toolbox + hardcoded decision tree + block classifier, under §8 boundaries, import-isolated.
**Deliverable:** an isolated research module that can attempt a WAF'd source, falling back to the aggregator on failure.
**Demo:** point it at a single WAF'd public chart → it either returns verified rows or cleanly degrades to the aggregator; the working product is unaffected either way.

### Phase 7 — Go horizontal (the north star)
**Build:** add currencies (Amex MR, Chase UR, Citi TYP, Bilt), more programs/alliances, and limited-time transfer-bonus alerts.
**Deliverable:** the multi-card "Expedia for points."
**Demo:** "I hold 90k Amex MR + 40k Chase UR + 20k C1 — best business seat LAX→Europe next month, and should I wait for a transfer bonus?" → ranked cross-currency answer with an alert if a bonus is live.

---

## 13. Decisions — locked & still open

**Locked (this revision):**
- **Multi-user:** yes, future phase (Phase 4). Interfaces built in from Phase 0; Redis/Upstash + Turso/Supabase + auth land at Phase 4.
- **Award space:** **aggregator first**; seats.aero wired as an *optional* paid provider, off by default.
- **Demos:** keep **both** (A = honesty, B = value), run side by side every phase.

**Still open (don't block Phase 0):**
1. **Demo B's exact route/program** — pick one international premium redemption to optimize the showcase around (e.g. LAX→IST via Turkish, or SFO→NRT via ANA).
2. **Proxy budget & legal posture** for the eventual Brain (Phase 6) — only matters if a WAF'd first-party source becomes necessary.
3. **Hosting target** for Phase 4 (Supabase end-to-end vs. mix of Fly/Vercel/Upstash/Turso) — decide when you actually deploy.

Tell me to start **Phase 0** and I'll scaffold `domain/` (the source-agnostic `AwardQuote`/`FareQuote`/`User` models, `cpp.py`, `verdict.py`), the `Provider` interface + registry, the `Repository`/`Cache`/`RateLimiter`/`Lock` interfaces with in-process impls, a seeded `knowledge/ratios.yaml` + `charts.yaml`, and a `cli.py` that runs both demos end-to-end.
