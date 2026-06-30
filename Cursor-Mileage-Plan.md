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
    aggregator/            # ENGINE A — real scraper + BOTH intake modes (§6, §6.1)
      __init__.py
      fetch.py             # httpx + curl_cffi; Wayback / RSS / PDF fallbacks (shared by every intake)
      politeness.py        # adaptive throttle + source rotation (simple first)
      parse.py             # bytes -> RawChartRow[] / RawAwardRow[] (selector-hit only)
      sources.py provider.py  # INTAKE (a) deterministic: sources.yaml -> _build_charts -> AwardQuote[]
      ingest/              # INTAKE (b) discovery — a normal sub-module of Engine A (§6.1, Phase 8)
        __init__.py        #   NOT import-isolated like brain/; reuses Fetcher + jobs + Redis + Arize
        email_source.py    # Gmail IMAP (app password) poll -> unread HTML bodies as documents
        creators.py        # creators.yaml blog RSS -> Fetcher.get(post) -> readability body
        transcripts.py     # youtube channel RSS -> youtube-transcript-api captions as documents
        devaluation.py     # subject/title "{program} devaluation" -> bump that program's charts stale
      extract/             # the LOCAL extractor every intake mode shares (§6.2) — no Anthropic
        __init__.py
        base.py            # LLMExtractor interface (swappable backend; local Qwen now)
        local_extractor.py # Qwen2.5-Instruct via Ollama/llama.cpp + constrained decoding
        grammar.gbnf       # GBNF schema: [{program,from,to,cabin,miles,roundtrip}] — output can't escape it
        gliner_tagger.py   # GLiNER span tagger: verbatim entities, cannot hallucinate
        grounding.py       # verbatim-number guard: drop any `miles` not literally in the source text
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
- `aggregator/ingest/` + `aggregator/extract/` are **normal sub-modules of Engine A**, not a separate engine and **not** import-isolated like `brain/`. They may import the aggregator's own `fetch`/`parse`/`provider` and the shared `store`/`obs` infra, but the `domain/`/`verify/` core still never imports *them*. Everything they emit is a plain `AwardQuote` the core can't distinguish from a deterministic scrape.
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

**Curated `sources.yaml` (this revision):** Turkish, ANA, and KrisFlyer now have explicit AwardTravelFinder (ATF) chart targets on the same non-WAF domain/parser already used for Aeroplan + LifeMiles, plus 10xtravel.com fallbacks and a KrisFlyer official-PDF target (pdfplumber, degrades to empty without it). All are `html_table_wide`/`pdf`, server-rendered, no WAF. L3 candidates (Roame, AwardFares) were evaluated and *not* added — Roame has no documented JSON endpoint (test with `curl_cffi` first; if it needs sensor data it's the Brain's, §8), and AwardFares' full data is account-gated (credentialed scraping is an §8 boundary). seats.aero remains the clean optional paid L3 path.

**The aggregator has two intake modes feeding one pipeline.** Both end in the *same* `RawChartRow[] → _build_charts() → AwardQuote[]` path through `verify/` and `graph/`, so the core cannot tell them apart:

- **(a) Deterministic** — the known URLs in `sources.yaml` parsed by the structural parsers above (today's behavior; `parse.py`/`provider.py`).
- **(b) Discovery / ingest (§6.1)** — email + creator blogs + creator video transcripts, turned into structured rows by a **local** open-source extractor. This is *not* a separate engine; it is `aggregator/ingest/` + `aggregator/extract/`.

---

## 6.1 The aggregator's discovery intake mode — email + creator blogs + transcripts

This is **intake mode (b) of the aggregator (Engine A)**, not a separate engine. The deterministic intake (§6) needs a human to add a URL to `sources.yaml`. The discovery intake removes that bottleneck: it pulls documents from three standing feeds — the agent's **mailbox**, creator **blogs**, and creator **video transcripts** — turns each into structured rows with a **local** extractor (§6.2), and emits the **same `AwardQuote`** through the **same `_build_charts() → verify → graph`** path. It lives in `aggregator/ingest/` + `aggregator/extract/` and, like everything in `providers/`, can be deleted without touching the core.

**Honest framing — this is NOT the Brain (§8), and NOT import-isolated like it either.** Discovery only touches *public, server-rendered* content (blog posts, public RSS/captions) and *your own opt-in inbox* (newsletters you subscribed to). It does **no** WAF evasion, **no** sensor forging, **no** credentialed scraping of protected first-party sites — those are §8 boundaries. Unlike `brain/`, discovery is a normal sub-module of the aggregator: it freely reuses the aggregator's `Fetcher`/`parse`/`_build_charts` and the shared `store`/`obs` infra. The one rule it shares with every provider: the `domain/`/`verify/` core never imports *it*.

**Three intakes, one extractor, one pipeline.** Email, blog body, and transcript are all just *documents*. Each flows: `document → LLMExtractor (§6.2) → RawChartRow[] → AggregatorProvider._build_charts() → AwardQuote[]`, tagged `flags=["llm_extracted"]` with a provenance `source_name` naming the intake:

| Intake | Module | Discovery → fetch → document | Provenance `source_name` |
|---|---|---|---|
| Email | `ingest/email_source.py` | poll inbox (IMAP) → unread mail → HTML body | `email:{sender}` |
| Blog | `ingest/creators.py` | `creators.yaml.blog_rss` → `Fetcher.get(post_url)` → readability body | `blog:{name}` |
| Transcript | `ingest/transcripts.py` | channel RSS → `youtube-transcript-api(video_id)` captions | `yt:{name}` |

The creator list is **`knowledge/creators.yaml`**: per creator a `blog_rss`, a `youtube.channel_id`, and a `trust` weight. The two `channel_id: TODO` entries (`lets_get_to_the_points`, `award_travel_101`) were resolved **once** by loading the `@handle` page and reading the canonical `UC…` id (never guessed) — `UCaE0KM4BEXBR1969_Urs6mw` and `UCOBfnZ9yioR331pId7FyrNA`; `resolve_channel_id()` does this for any future handle. Every feed URL is confirmed with `mileage sources --validate-urls --force` before it is relied on. Daily Drop is email-first (no reliable RSS) → it arrives as `email:dailydrop`.

**Email is a standing scraping feed (`occulosequor@gmail.com`).**
- **Any received email is automatically ingested.** `ingest/email_source.py` polls the inbox on a schedule via the existing **`store/jobs.py`** queue (daily is fine; the Redis-list swap is the multi-worker upgrade, same interface), pulls **unread** mail, takes the **HTML body as a document**, and runs it through the local extractor like any other page. No human action after the one-time subscribe.
- **Auth: IMAP App Password only.** `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` in `.env`, read from the environment, never in code. **No Gmail API, no OAuth, no Cloud Pub/Sub** — polling is sufficient and far less to build/secure. (The old `GMAIL_OAUTH_*` keys are dropped from the discovery path.)
- **Devaluation fast-path (`ingest/devaluation.py`):** a subject matching `"{program} devaluation"` / `"award chart change"` immediately bumps that program's charts to `stale` in the store, rather than waiting for the next scheduled run to notice. (Mechanism in §6.2 — it lives in `store/`, so `domain/`/`verify/` are untouched.)

**Blogs and transcripts — scrape the creators the mailbox follows.**
- **Blogs:** poll each `blog_rss`, fetch new post URLs through the **existing `Fetcher.get()`** (unchanged: same politeness, Wayback/PDF fallbacks, `file://` fixtures), readability-extract the article body, extract.
- **Transcripts:** discover new videos via the channel RSS feed (`https://www.youtube.com/feeds/videos.xml?channel_id=<UC…>`), pull captions with **no API key** — `youtube-transcript-api` (preferred) or `yt-dlp --write-auto-sub --skip-download` as fallback — and treat the transcript text as a document.

**Anti-hallucination — discovered rows are second-class until independently confirmed (§5/§7).** Every `llm_extracted` row is subject to the guards in §6.2 (constrained decoding + verbatim-number grounding), then enters the **same `verify/crosscheck.py`** as everything else: it cross-checks against curated `knowledge/charts.yaml`, agreement elevates confidence, disagreement adds `sources_disagree_NN%` and demotes. Because `crosscheck` keys independence on `source_name`, a blog and a transcript that merely echo the same post are **not** independent. A winner resting on an `llm_extracted` row can only ever be `tentative_best`, never `best`, until a genuinely independent source confirms it.

**Reuses existing infra — no new services:**
- **Redis (§9):** `Cache` for URL/email/video de-dupe (don't re-extract the same document within TTL); `RateLimiter` throttles outbound fetches; `Lock` (`SETNX`) so two runs don't extract the same post.
- **Jobs (`store/jobs.py`):** the inbox poll + blog/transcript sweeps are submitted as background jobs off the request path.
- **Arize (§10):** a discovery run is a CHAIN span (`discover`); each `LLMExtractor` call is an **LLM span** with the raw document in and the JSON rows out — exactly the extraction use-case OTel tracing is for (extraction accuracy per source, where the grounding guard caught a hallucinated number).

**Honest caveat:** LLM extraction of charts from prose is good, not perfect — posts/transcripts describe *sweet spots* more than full zone matrices, so headline numbers come through but edge-case zones may be missed. Constrained decoding + the verbatim-number guard + the `llm_extracted` flag + cross-check against curated YAML is the safety net: it cannot invent a number, and an unconfirmed number never becomes a `best`.

---

## 6.2 The local extractor (`aggregator/extract/`) — model, constrained decoding, grounding

**No Anthropic. No cloud key in the discovery path.** The extractor runs locally behind a small `LLMExtractor` interface so the backend is swappable (local Qwen now; a different local model, or a hosted one, is a config swap — nothing hardwired). Search keys (`BING_SEARCH_API_KEY` / `SERPAPI_API_KEY`) stay **optional**; `ANTHROPIC_API_KEY` is removed from this path.

```python
# aggregator/extract/base.py
class LLMExtractor(Protocol):
    def extract(self, document: str, *, source_hint: str = "") -> list[RawChartRow]:
        """Prose/HTML/transcript -> schema-valid, number-grounded chart rows."""
```

**Model choice — `Qwen2.5-7B-Instruct` (3B-Instruct for a CPU/12 GB-light build).** Justification:
- **Local, open-weight, commercially usable (Apache-2.0)** — satisfies the "no cloud key" hard requirement and keeps every document on-device (the mailbox is private mail).
- **Right size for structured extraction.** This is span-copying, not reasoning; a 7B instruct model is comfortably enough and **QLoRA-trains + serves on a single 12 GB consumer GPU** (§6.3). 3B is the CPU-friendly fallback with the same toolchain.
- **First-class constrained-decoding support** in both serving paths we'd use (llama.cpp GBNF; Outlines/XGrammar on vLLM).
- **Alternatives considered:** *Llama-3.1-8B-Instruct* — fine, slightly heavier, similar story; *Phi-3.5-mini* — great on CPU but weaker on long messy HTML; *gemma-2-9b* — capable but larger and license is more restrictive; hosted GPT/Claude — **rejected**, reintroduces the cloud key we are removing. Qwen2.5 is the best size/license/tooling fit; the `LLMExtractor` interface means any of these is a drop-in if that changes.

**Serving:** **Ollama** for the easy local path (`ollama run qwen2.5:7b-instruct`, GBNF grammar via the `format`/grammar option), or **llama.cpp** directly when we want the GGUF + GBNF grammar pinned. vLLM + Outlines/XGrammar is the throughput option if discovery volume grows. All three sit behind the same `LLMExtractor`.

**Constrained decoding is mandatory — not retry-on-failure.** The decoder is constrained to the exact schema so output *physically cannot* escape it:

```gbnf
# aggregator/extract/grammar.gbnf  (sketch)
root    ::= "[" (row ("," row)*)? "]"
row     ::= "{" "\"program\":" str "," "\"from\":" str "," "\"to\":" str ","
            "\"cabin\":" cabin "," "\"miles\":" int "," "\"roundtrip\":" bool "}"
cabin   ::= "\"economy\"" | "\"premium_economy\"" | "\"business\"" | "\"first\""
int     ::= [0-9]+
bool    ::= "true" | "false"
str     ::= "\"" ([^"\\] | "\\" .)* "\""
```

The schema mirrors `RawChartRow` exactly (`program, from→region_a, to→region_b, cabin, miles, roundtrip`), so the extractor's output drops straight into the existing `_build_charts()` with no new parsing surface. `cabin` is constrained to the four canonical values the parser already accepts.

**Two anti-hallucination guards on top of constrained decoding (§5):**
1. **Verbatim-number grounding (`extract/grounding.py`) — the hard guard.** Reject any row whose `miles` integer does **not** appear literally in the source text (comma/spacing-insensitive). Numbers are the one thing we cannot afford to invent; a schema-valid row with a fabricated number is the dangerous failure mode, and this kills it deterministically before it ever becomes an `AwardQuote`.
2. **GLiNER span tagger (`extract/gliner_tagger.py`) — complementary.** GLiNER extracts entity spans *verbatim* from the text (program names, cabins, city/zone names) and **cannot hallucinate** them. Use it to (a) pre-tag candidate program/zone/cabin spans to focus the extractor, and (b) cross-validate the LLM's `program`/`cabin`/region fields against spans that actually occur in the document; a field with no supporting span is dropped. The model is also prompted to **omit any row it is unsure of rather than guess.**

**Devaluation → stale mechanism (no `domain/`/`verify/` changes).** A `store/` method records a per-program `marked_stale_at` (the `program_staleness` table in `SQLiteRepository`); when the aggregator emits chart quotes it consults `repo.stale_programs()` and, for a flagged program, attaches the `stale` flag (and caps `source_updated_at` ~200 days back, before the freshness cutoff). `verify/crosscheck.py` already carries `q.flags` through and already demotes anything flagged `stale` — so proactive staleness is purely additive on the `store` + emission side. The detector (`ingest/devaluation.py`) is shared by all intakes: email subjects and blog/transcript titles funnel through `detect_devaluation`, and `mark_devaluations_stale(repo, …)` persists the hits. `discovered_charts.json` keeps a `stale_programs` list as a fallback for runs without a repo.

## 6.2a Region canonicalization — the fix that makes scraped charts resolve (§A)

Before this, scraping real ATF / 10xtravel chart pages produced parsed rows that **never became route quotes**: `parse.py` stored region cells as raw text (`"north america"`, `"atlantic"`, `"0–4,000 mi"`) while `domain/charts.py` matched a band only when its region pair *exactly equaled* the route's canonical tokens (`north_america` / `europe` / …). Spaces ≠ underscores, and distance bands had no token at all → `lookup_award_miles` always returned `None`. Fixtures only resolved because they were hand-authored in snake_case.

- **`providers/aggregator/regions.py`** adds `canonicalize_region(label) -> token | None` (human zone label → canonical token; `None` for anything unmapped → the row is **dropped and counted**, never guessed), plus `canonicalize_zone_pair` (an Aeroplan `from` cell that names a *pair*, e.g. "Between North America and Atlantic") and `parse_distance_band` ("0–4,000 mi" → `(0, 4000)`).
- **`parse.py`** applies canonicalization in the long/wide/JSON chart parsers, storing canonical tokens and dropping (with a `stats["dropped"]` counter) rows whose zone can't be mapped. `RawChartRow` gains optional `distance_min/_max`.
- **`charts.yaml`** expands `region_map` from 12 to ~70 award-relevant airports and adds an `airports:` `[lat, lon]` table.
- **Distance bands (Aeroplan, §A.4):** `domain/charts.py` gains a `great_circle_miles` helper and a distance-band code path — a band carrying a `distance: [lo, hi]` range matches only when the route's great-circle distance falls inside it (`airport_coords` passed in by the provider; `domain/` still imports nothing from `providers/`). A latent bug is also fixed: a geography-matching band that lacked the requested cabin used to `return None`, hiding later bands; it now `continue`s.

## 6.2b Validator hardening (§G) and URL rediscovery (§F)

- **`--validate-urls --deep` (§G).** `validate_targets` previously flagged only 404/410; an unreachable host (`status 0`) and a 200-but-empty page both reported `ok`. Deep mode now fetches a reachable target's body and runs its structural parser, requiring ≥1 canonicalizable row. `Target.status_label()` distinguishes **`ok` / `unreachable` / `rotted` / `selector_miss`**; `source_health` persists a `consecutive_failures` counter and `selector_misses` (a transient `status 0` still does not *permanently* disable a source, but is no longer printed as `ok`).
- **URL rediscovery (`providers/aggregator/rediscover.py`, §F).** Deterministic scraping stays the default/cheap path. A source is *rotted* when `last_404` OR `consecutive_failures ≥ N` (default 3) OR `selector_misses ≥ M` (default 2). Only when ≥`MILEAGE_REDISCOVERY_MIN_ROTTED` (default 1) sources are rotted — and only behind `MILEAGE_URL_REDISCOVERY=1` plus a search key — does a pluggable `WebSearch` (SerpAPI primary, Bing secondary; `NoopSearch` otherwise) **propose** candidate URLs. The LLM/search only proposes URLs; it never extracts numbers. Every candidate must pass `validate_candidate` (real fetch, not 404, **and** the parser must hit ≥1 row) before it is appended to `sources.yaml` with low trust and a `discovered_url` note. Runs off the `store/jobs.py` queue, rate-limited, and cached per rotted source (no re-search within TTL) — expected volume: ≤1 search per rotted source per TTL, never on the request path. Exposed as `mileage sources --rediscover`.

## 6.3 Fine-tuning & evaluation (propose, do not run yet)

**Fine-tuning — QLoRA via Unsloth on `Qwen2.5-7B-Instruct`.** 4-bit base + trained low-rank adapters; fits the 12 GB GPU and serves through the same Ollama/llama.cpp path (merge adapters → GGUF, or load adapters at runtime). LoRA, not full fine-tune, so the artifact is a few hundred MB and the base stays swappable.
- **Dataset = `(source_text → JSON rows)` pairs from our own corpus.** Seed from the ATF / 10xtravel chart pages we already fetch plus a sample of creator posts/transcripts. Bootstrap a few hundred examples; **hand-correct every number**; hold out a test split. Optionally distill labels *once* from a stronger model to seed, **then human-verify** — no unverified label enters training, the same no-hallucination contract the runtime enforces.
- **Why fine-tune at all:** the stock 7B already does the job with constrained decoding; QLoRA buys higher recall on messy blog/transcript layouts and fewer omitted rows, without changing the safety guarantees (constrained decoding + grounding still gate the output).

**Eval — an offline extraction-accuracy harness wired like `mileage/evals.py`.**
- **Metrics:** row-level precision/recall against a labeled fixture set, and **exact-match on the `miles` integer** (the number that matters), plus a hallucination counter = rows the grounding guard rejected. Runs deterministically/offline (fixtures via the existing `_OfflineFetcher` pattern) and **exits non-zero on regression**, so extraction quality is a CI gate, not a vibe.
- **Arize LLM spans:** raw document in, JSON rows out, with the grounding-guard verdict on each row — so we can watch extraction accuracy per source over time and *see exactly where* a hallucinated number got caught.

**Dependency list (all local / keyless for the core path; add to a `discovery` extra):**
- `youtube-transcript-api` (captions; `yt-dlp` optional fallback)
- `feedparser` (blog + channel RSS; already an optional aggregator accelerator)
- a readability extractor (`trafilatura` or `readability-lxml`) for blog/email bodies
- `gliner` (verbatim entity spans)
- a local LLM server: **Ollama** (simplest) or `llama-cpp-python` (GGUF + GBNF); `outlines`/`xgrammar` + `vllm` only if we move to the GPU-throughput path
- fine-tuning (dev-only, not runtime): `unsloth`, `peft`, `trl`, `bitsandbytes`
- **stdlib** `imaplib` + `email` for the mailbox — **no new Gmail dependency**
- **Optional, not required:** `BING_SEARCH_API_KEY` / `SERPAPI_API_KEY` for active search. **Removed:** `ANTHROPIC_API_KEY`, `GMAIL_OAUTH_*`.

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
| Discovery intake (§6.1) | IMAP (`imaplib`), `feedparser`, `youtube-transcript-api`, readability | ✓ keyless | same (server-side) |
| Local extractor (§6.2) | Qwen2.5-Instruct via Ollama/llama.cpp + GBNF constrained decoding + GLiNER | ✓ local | same (self-hosted / GPU) |
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

**Also shipped in this phase — the email discovery intake (§6.1, aggregator mode b).** `providers/aggregator/ingest/email_source.py` polls the mailbox (`occulosequor@gmail.com`) over IMAP App Password (UNSEEN + PEEK, idempotent; `.eml` fixtures when offline/unconfigured), runs each body through the keyless deterministic extractor (`extract/deterministic.py` + verbatim-number grounding in `extract/grounding.py`), and `mileage discover` writes number-grounded rows to `knowledge/discovered_charts.json`, which `AggregatorProvider` resolves as `llm_extracted` (→ `tentative_best`-only) quotes. The region-canonicalization fix (§6.2a) is what makes those rows actually resolve to route quotes. `mileage discover` defaults to this email-only path (fast, proven); `--all` opts into the blog/transcript sweep (Phase 8). Devaluation headlines mark a program `stale` (§6.2). **This is the part of discovery that counts toward "Phases 0–5 done."**

### Phase 6 — The Brain (sandboxed, optional)
**Build:** only if a needed source exists *only* behind a WAF — strategy toolbox + hardcoded decision tree + block classifier, under §8 boundaries, import-isolated.
**Deliverable:** an isolated research module that can attempt a WAF'd source, falling back to the aggregator on failure.
**Demo:** point it at a single WAF'd public chart → it either returns verified rows or cleanly degrades to the aggregator; the working product is unaffected either way.

### Phase 8 — Discovery intake: creator blogs + transcripts (§6.1/§6.2)
> The **email** intake + the **local extractor** + the region-canonicalization fix shipped in Phase 5 (above). Phase 8 is the *creator-feed expansion* on top of that proven base — opt-in via `mileage discover --all` until the feeds are validated and relied on.

**Build:** `providers/aggregator/ingest/` — `creators.py` (blog RSS → `Fetcher.get` → readability body, stdlib fallback), `transcripts.py` (channel RSS → `youtube-transcript-api`/`yt-dlp` captions), `orchestrate.py` (run all intakes off the `store/jobs.py` queue under one `discover` span), and the shared `devaluation.py` (titles → bump program charts `stale` via the `store/` `program_staleness` table). Reads `knowledge/creators.yaml`. Reuses the existing `Fetcher`, the `store/jobs.py` queue, Redis (`Cache`/`RateLimiter`/`Lock`), and Arize (a `discover` CHAIN span + per-extraction LLM spans). **No Anthropic key, no Gmail API/OAuth, no new services.** Nothing in `domain/`/`verify/` changes — discovered rows are plain `llm_extracted` `AwardQuote`s that enter the same `_build_charts → verify → graph` pipeline and cross-check against curated YAML. The aspirational local `LLMExtractor` upgrade (Qwen2.5 + GBNF constrained decoding + GLiNER, §6.2/§6.3) remains future work; today's extractor is the keyless deterministic one.
**Deliverable:** `sources.yaml` stops being the only L4 intake: the aggregator ingests subscribed newsletters, new creator blog posts, and new video transcripts automatically, emitting normalized, provenance-tagged (`email:`/`blog:`/`yt:`), constrained-decoded, number-grounded, cross-checked `AwardQuote`s. Devaluation emails bump affected charts to `stale` proactively. An offline extraction-accuracy eval (§6.3) gates the extractor in CI.
**Demo:** run `mileage discover --all` → it polls the inbox, fetches new blog posts and pulls video captions, runs each through the deterministic extractor (the grounding guard rejects any number not in the source), cross-checks against curated charts (agreement elevates confidence, conflict flags `sources_disagree`), and emits `tentative_best`-only rows. A planted "Turkish devaluation" email flips Turkish's charts to `stale`.

### Phase 8b — Finish intake, fix the live-data gap, add URL rediscovery (§6.2a/§6.2b)
**Build:** the root-cause **region canonicalization** layer (§6.2a — `regions.py`, `parse.py` canonicalization + dropped-row counting, expanded `charts.yaml` region_map + airport coords, distance-band resolver with great-circle in `domain/charts.py`); the remaining intakes (`ingest/creators.py` blogs, `ingest/transcripts.py` transcripts with the two `channel_id` TODOs resolved from their handles, shared `ingest/devaluation.py`, `ingest/orchestrate.py` running all four off the jobs queue under a `discover` span); **validator hardening** (`--validate-urls --deep` with `ok`/`unreachable`/`rotted`/`selector_miss` taxonomy, persisted rot counters); and **URL rediscovery** (`rediscover.py`, behind `MILEAGE_URL_REDISCOVERY=1` + a search key, validate-before-adopt). Store gains `program_staleness`, rot counters, and WAL.
**Deliverable:** scraped real-label chart pages now resolve to route quotes (mappable rows resolve, unmapped rows are dropped + counted), blogs/transcripts are ingested with `blog:`/`yt:` provenance, devaluation headlines proactively demote a program, the validator surfaces real rot, and rotted sources can be re-pointed automatically with mandatory validation. New `tests/test_phase8b.py` (10 tests) + an extraction precision/recall/exact-miles eval gate, all green offline under `MILEAGE_OFFLINE=1`.
**Demo:** `mileage sources --validate-urls --deep` shows true `ok`/`selector_miss` states; `mileage discover` shows live IMAP + new blog/transcript rows; a real ATF URL produces an end-to-end route quote.

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
- **L4 chart sources (this revision):** Turkish / ANA / KrisFlyer added to `sources.yaml` against ATF (same parser as Aeroplan/LifeMiles) + 10xtravel fallbacks + KrisFlyer official PDF. Roame/AwardFares evaluated and **not** added (no public endpoint / account-gated).
- **Discovery intake mode (§6.1, NOT a separate engine):** email + creator-blog + creator-transcript ingestion is **intake mode (b) of the aggregator (Engine A)** — `aggregator/ingest/` + `aggregator/extract/`, feeding the same `_build_charts → verify → graph` pipeline as `sources.yaml`. The mailbox `occulosequor@gmail.com` is a standing feed: **any received mail is auto-ingested** via a scheduled IMAP poll on the existing jobs queue (**App Password only — no Gmail API/OAuth/Pub/Sub**). Blogs/transcripts come from `knowledge/creators.yaml` (RSS + `youtube-transcript-api`, no API key). The extractor is **local and open-source (Qwen2.5-Instruct + mandatory constrained decoding + a verbatim-number grounding guard + GLiNER)** — **`ANTHROPIC_API_KEY` is removed from this path.** Public/opt-in content only; it is **not** the Brain (no Akamai, no credentialed scraping) and **not** import-isolated like `brain/`.

**Recommended next steps (priority order):**
1. **Done — Turkish/ANA/KrisFlyer added to `sources.yaml`** pointing at ATF; unblocks Demo B with real chart data once live.
2. **Run `mileage sources --validate-urls --force`** against the new HTTP/PDF targets to confirm ATF pages still serve and the table structure matches the `html_table_wide` parser (the KrisFlyer PDF filename especially — it's a best-effort URL pending confirmation).
3. **(Optional) Add a curated KrisFlyer baseline** to `knowledge/charts.yaml` so scraped KrisFlyer rows have an independent cross-check — *only with sourced numbers* (no hallucinated charts; that's the §2.1 contract).
4. **Build the discovery intake (Phase 8):** local extractor + IMAP/RSS/transcript intakes under `aggregator/ingest/` + `aggregator/extract/` — the most durable fix for keeping charts fresh. Resolve the `channel_id: TODO` entries in `creators.yaml` first and `--validate-urls` every feed.
5. **Decide seats.aero vs. Roame for L3** — the discovery intake covers L4 well; L3 live space still needs a dedicated source.

**Still open (don't block current work):**
1. **Proxy budget & legal posture** for the eventual Brain (Phase 6) — only matters if a WAF'd first-party source becomes necessary.
2. **Hosting target** for Phase 4 (Supabase end-to-end vs. mix of Fly/Vercel/Upstash/Turso) — decide when you actually deploy.
3. **Local extractor serving path** for the discovery intake — Ollama (simplest) vs. llama.cpp GGUF+GBNF vs. vLLM+Outlines (throughput) — and whether to ship the stock Qwen2.5 or the QLoRA-tuned adapter (§6.3) first. *(Gmail auth is settled: IMAP App Password only.)*

Tell me to start **Phase 0** and I'll scaffold `domain/` (the source-agnostic `AwardQuote`/`FareQuote`/`User` models, `cpp.py`, `verdict.py`), the `Provider` interface + registry, the `Repository`/`Cache`/`RateLimiter`/`Lock` interfaces with in-process impls, a seeded `knowledge/ratios.yaml` + `charts.yaml`, and a `cli.py` that runs both demos end-to-end.
