# Mileage — Architecture & Build Plan

*A points-to-flights redemption optimizer built from two independent data engines that check each other's work, degrade gracefully when one fails, remember what they find, and present results through a calm web interface.*

---

## 0. What Mileage is

Mileage answers one question honestly: **"I hold X Capital One miles and want to fly O→D in cabin C — what is the single best way to convert my points into that seat, in cents per mile, and can I trust the number?"**

It is the consolidation of your existing work (`AggregateScraper`, the `mini-scraper` graph model, and the `lifemiles-lab` findings) into one product with a clear shape:

- **Two data engines** that gather award/redemption data from different classes of source.
- **A verification core** that reconciles their answers and refuses to guess.
- **A graph optimizer** (your proven CPP-by-product model) that ranks redemptions.
- **A memory layer** that persists everything found, with provenance and freshness.
- **A web frontend** (the "Mileage" UI) that runs locally now and is built to move to hosting later.

The load-bearing domain fact carries over unchanged: **Capital One does not transfer directly to United MileagePlus**, so value is found by routing through Star Alliance partners (LifeMiles, Turkish, KrisFlyer, Aeroplan, ANA) and compounding CPP by product across each hop.

---

## 1. System topology

```
                              ┌──────────────────────────────┐
                              │         MILEAGE UI           │
                              │  (beige web app, localhost   │
                              │   now → hosted later)        │
                              └───────────────┬──────────────┘
                                              │  HTTP/JSON
                              ┌───────────────▼──────────────┐
                              │        ORCHESTRATOR API       │
                              │  (FastAPI: route query →      │
                              │   run engines → verify →      │
                              │   graph → conclude)           │
                              └──┬───────────────────────┬────┘
                                 │                       │
          ┌──────────────────────▼─────┐   ┌─────────────▼───────────────────┐
          │   ENGINE A — AGGREGATOR     │   │  ENGINE B — PROTECTED SOURCE    │
          │   less-protected sites      │   │  ("the Brain")                  │
          │   • httpx / curl_cffi       │   │  • strategy toolbox             │
          │   • RSS / Wayback / PDF      │   │    (curl_cffi, nodriver,        │
          │   • adaptive RATE-LIMIT      │   │     Camoufox/Patchright, proxy) │
          │     + source-rotation policy │   │  • adaptive STRATEGY policy     │
          │   • aggregator + 1st sources │   │  • block classifier             │
          └──────────────┬──────────────┘   └──────────────┬──────────────────┘
                         │   ScrapedRow[]                   │   ScrapedRow[]
                         └──────────────┬───────────────────┘
                                        │
                          ┌─────────────▼──────────────┐
                          │     VERIFICATION CORE       │
                          │  cross-check · trust ·      │
                          │  freshness · bounds ·       │
                          │  CONSENSUS / DISAGREEMENT   │
                          └─────────────┬──────────────┘
                                        │  verified edges
                          ┌─────────────▼──────────────┐
                          │     GRAPH + OPTIMIZER       │
                          │  NetworkX DiGraph ·         │
                          │  CPP-by-product · rank ·    │
                          │  conclude_winner (≥20%)     │
                          └─────────────┬──────────────┘
                                        │
                ┌───────────────────────▼───────────────────────┐
                │                MEMORY / STORAGE                │
                │  SQLite (durable source of truth, local)       │
                │  Redis (hot cache, leaderboard, locks,         │
                │         rate buckets, engine memory, episodes) │
                │  → Upstash / Supabase / Turso when off-laptop  │
                └────────────────────────────────────────────────┘
                                        │
                          ┌─────────────▼──────────────┐
                          │   OBSERVABILITY (optional)  │
                          │  OTel spans → Phoenix        │
                          │  evals = anti-hallucination  │
                          │  + Grafana metrics           │
                          └─────────────────────────────┘
```

Two engines feed one verification core. The core never trusts a single number on its own. Everything found is written to memory with its source, age, and confidence. The frontend talks only to the orchestrator.

---

## 2. Design principles (the contract)

These are non-negotiable and inherited from your existing thesis:

1. **No hallucinations.** Data enters the graph only from a source with an actual selector hit. A row with no selector match, or flagged hallucinated, is never usable.
2. **Cross-check.** Two independent sources must agree before a number is trusted; an authoritative source (Capital One for transfer ratios) wins outright.
3. **Graceful degradation.** The product produces a useful, honest answer **even if one engine returns nothing** — it just lowers confidence and says so.
4. **Live precedence.** Live availability (seats.aero) overrides static zone charts when present.
5. **Honest conclusions.** Never name a winner unless verified data exists on both the portal and the transfer path. "Portal is your floor" is a valid, frequent answer.
6. **Provenance and freshness are first-class.** Every datum carries source, timestamp, trust weight, and age-decayed confidence.
7. **Local-first, cloud-ready.** Runs entirely on a laptop today; no design decision blocks a later move to hosting.

---

## 3. The two data engines

You asked for the aggregator and the protected-source reader to be **separate**, with the harder AI reserved for the Akamai problem and a lighter adaptive layer for the aggregator. That separation is the right call — they have different failure modes and different ethics, so they should be different modules behind a common contract.

### 3.1 The common contract

Both engines emit the same `ScrapedRow` your `AggregateScraper` already defines: `source_name/url`, `scraped_at`, `from_program`, `to_program`, optional `transfer_ratio`, zones + `economy/business/first_miles`, `raw_cell_text`, `selector_matched`, `confidence`, `flags`, `source_updated_at`, `source_trust`, `source_count`, `miles_range_low/high`. The verification core doesn't care which engine produced a row — only what's in it. This is what makes "works even if one fails" possible: the core consumes a flat list of rows from zero, one, or two engines identically.

### 3.2 Engine A — the Aggregator (less-protected sites)

**Scope:** secondary sources (awardtravelfinder, 10xtravel, blog charts, RSS award feeds, PDF charts) and first sources that *aren't* behind a heavy WAF. Your lab already proved this is where the reliable data actually lives.

**Fetch stack:** `httpx` for plain pages, `curl_cffi` (TLS/JA4 impersonation) for sites that only check headers/TLS, plus your existing Wayback / RSS / PDF fallbacks. No browser needed for the core.

**The previously-found problems, and where they're handled:**

| Problem | Fix (carried over / formalized) |
|---|---|
| Rate limiters (429) | Adaptive token-bucket per domain in Redis + exponential backoff + jitter |
| URL rot / 404s | `--validate-urls`, per-target `last_404`, monthly health check |
| Round-trip vs one-way charts (ANA) | `get_ow_miles()` normalization + `rt_to_ow_normalized` flag |
| Freshness double-counting | `_dedupe_freshness_entries()` (one entry per program/source/scraped_at) |
| Source disagreement | trust-weighted median + `sources_disagree_NN%` flag |
| Bot-blocked secondary site | source rotation → next target in the ordered chain → Wayback |

**The "AI for the aggregator" (you asked, and yes it helps — kept deliberately simple):** the aggregator does *not* need an evasion brain. What it benefits from is a **lightweight adaptive throttling + source-selection policy** — a small multi-armed bandit whose only job is to learn, per domain, the *fastest delay that doesn't trigger a 429* and the *source order most likely to return fresh data*. Actions are `{politeness delay tier, source order, with/without proxy}`; reward is `success ÷ (latency × blocks)`. This is a scheduling optimization, not evasion — it makes a well-behaved scraper *efficient*, not *sneaky*. It shares the bandit machinery of Engine B but runs in a completely different risk class.

### 3.3 Engine B — the Protected-Source Reader ("the Brain")

**Scope:** first-party sources fronted by Akamai Bot Manager (the LifeMiles class of problem). This is the module you want an AI trained for.

**The honest framing first.** "Train an AI to beat Akamai" sounds like a model problem; it mostly isn't. In 2026 the blocking layers are TLS/JA4 fingerprinting, HTTP/2 fingerprinting, the `sensor.js` JavaScript challenge feeding the `_abck` trust-score cookie, and IP reputation. The tools that actually move the needle are not AI: `curl_cffi` (TLS impersonation), `nodriver` (automation-protocol evasion — the layer where every Playwright fork fails), `Camoufox`/`Patchright` (JS fingerprint), and reputable residential/mobile proxies (IP reputation). **The AI's job is not to crack the challenge — it's to orchestrate these tools intelligently and learn what works where.**

**Architecture of the Brain — an adaptive orchestration layer, not a cracker:**

- **Action space:** a discrete toolbox of fetch strategies — `{curl_cffi+impersonate, nodriver+residential-proxy, Camoufox, Patchright, wayback-fallback, source-swap-to-aggregator}`.
- **State / context:** target domain, which detection layer it gates on, recent block history for that domain, proxy reputation, time of day.
- **Reward:** `+1` for a 200 carrying *real priced rows* (validated by your existing `is_usable()` / selector-hit check — you already have a ground-truth success signal that can't be faked), `0` for a block or empty page, minus cost and latency.
- **Policy:** start with a **hardcoded decision tree** (below); once episodes accumulate, replace it with a **contextual bandit** (Thompson sampling, then LinUCB if you want cross-domain generalization).
- **Block classifier:** extend `is_bot_blocked()` into a small classifier returning `{success, hard-block, JS-challenge, rate-limit, empty}` — this is the bandit's feedback signal.

**The hardcoded policy to start with** (the manual "brain" you run before any learning):

```
Does the data appear in the raw HTML?
├── Yes → curl_cffi (impersonate="chrome") + residential proxy
│         Still blocked? → it checks JS too → go to a browser.
└── No (needs JavaScript) →
    ├── Blocks on the very first load → fingerprint gate → Camoufox / Patchright
    ├── Blocks only after N requests → IP/behavior gate → add proxies, warm the
    │                                  session, randomize pacing
    └── Blocked when automated but fine when clicked by hand
        → automation-protocol detection → switch to nodriver
```

**How it "trains":** every fetch attempt is logged as an episode `(state, action, reward)` to Redis (hot) and Phoenix (traced). After enough episodes, the bandit learns the same tree the experts wrote — but tuned to *your* targets, *your* proxies, *your* time windows. This is exactly your `lifemiles-lab` pattern generalized: the lab ran all five strategies and scored them; the Brain is the lab plus a policy that *chooses* a strategy instead of running all of them.

**Critical recommendation:** for *award charts specifically*, your own lab already proved Engine B is usually unnecessary — the aggregator gets the same data faster and cleaner. Treat the Brain as (a) a research/learning module and (b) a last resort for a source that genuinely exists *only* behind a WAF. Build Engines A → verification → graph → UI first and ship a working product **without ever fighting Akamai**; add the Brain last, sandboxed. (See §10 for what's off-limits inside it.)

---

## 4. Cross-check & graceful degradation

This is the heart of "the two check each other but it works if one fails." The verification core (your `cross_check.py` / `trust.py` / `freshness.py` / `bounds.py`, generalized to two engines) runs this logic per `(program, zones, cabin)` group:

1. **Authoritative short-circuit.** If a usable row from an authoritative source (`capitalone.com`, `partners.yaml`) is present, it wins — no vote needed.
2. **Both engines returned.** Compute a **trust-weighted median** across all rows (Engine A and Engine B rows are pooled, weighted by source trust and age). If the spread exceeds ~10%, emit the edge but flag `sources_disagree_NN%` and demote to low confidence. Agreement → high confidence.
3. **Only one engine returned.** Emit the edge at `medium`/`low` confidence with a `single_engine` flag. The verdict still computes; it just carries a visible caveat.
4. **Neither returned, but a fallback row exists.** Emit flagged `hardcoded_fallback`; never `recommended`.
5. **Nothing at all for the transfer side.** The conclusion engine returns `portal_only` — the portal floor (1.0¢ Venture / 1.25¢ Venture X) as the confirmed answer.

The **conclusion engine** (`conclude_winner`) then applies the honesty rules:
- No verified, non-stale transfer path → `portal_only`.
- Best transfer within 20% of portal → `comparable` ("weigh availability and fees").
- Best transfer beats portal by ≥20% → `best`, or `tentative_best` if the winner carries any warning flag.

Because the core consumes a flat row list, the failure of either engine never crashes the pipeline — it only shifts confidence and the verdict label. That is the graceful degradation you asked for, made structural rather than bolted-on.

---

## 5. Memory & storage

You asked the engines to "use memory to save the data they find." The design is **local-first with a clean upgrade path**, two tiers:

**Tier 1 — durable source of truth: SQLite (local).** Your existing `db/store.py` schema — edges, runs, alerts — stays authoritative. Everything verified is written here. It's a single file, zero infrastructure, perfect for localhost.

**Tier 2 — hot working memory: Redis.** Sits *in front of* SQLite, never replaces it:

| Use | Redis structure | Notes |
|---|---|---|
| HTTP response cache | string + TTL | TTL = your freshness window (7d charts); this *is* your cache scheduling |
| CPP leaderboard | sorted set (ZSET) | score = CPP, member = path key; O(log n) ranked reads |
| In-flight dedupe | `SETNX` lock | two route queries don't re-scrape the same chart |
| Per-domain rate limiting | counter + TTL | the aggregator's polite throttle, shared across workers |
| Engine memory (the Brain) | hash | per-domain strategy success rates, proxy reputation |
| Episode log (training) | stream | `(state, action, reward)` for the bandits |

**Going beyond local later** (you asked to keep this open):

- **Redis off-laptop → Upstash** (serverless Redis, free tier, HTTP API) — drop-in when you leave localhost; no server to run.
- **Durable store off-laptop → Supabase** (hosted Postgres + auth + storage, free tier) **or Turso** (hosted libSQL — closest to your SQLite, minimal migration). Supabase additionally gives you auth and can host the frontend, which matters if Mileage ever becomes multi-user.
- The only code rule to keep this painless: **access storage through a thin repository interface** (`get_edge`, `put_edge`, `cache_get/set`) so swapping SQLite→Turso or local-Redis→Upstash is one adapter change, not a rewrite.

**On "Sai":** I'm reading that as **Supabase** — it's the natural fit for "more than just local" because it bundles hosted database, auth, storage, and frontend hosting behind one free tier. If you meant something else (for instance **seats.aero**, which is a *data source* for live availability, not infrastructure — it already belongs in Engine A behind an API key), say so and I'll slot it correctly.

---

## 6. Observability & the training loop

This is where **Arize Phoenix** earns its place — but only because Mileage now has ML in it (the two bandits and, optionally, an LLM parser). Phoenix is free, open-source, and OpenTelemetry-native, which dovetails with your Dynatrace/OTel experience.

Two distinct jobs:

1. **Tracing the pipeline.** Instrument every fetch attempt, parse, cross-check, and ranked path as an OTel span. In Phoenix this becomes a searchable trace of "what happened" on every run — and, not coincidentally, the **episode log the bandits learn from**. The observability data *is* the training data.
2. **Evals as automated anti-hallucination.** Build a **golden route set** (a handful of routes whose correct miles you know from `fallback_rates.json`). Run your `cross_check` / `bounds` rules — and any LLM parser — as Phoenix evaluators against that set in CI. Your "no data enters the graph without verification" principle stops being a code comment and becomes a test that fails loudly when a parser drifts.

For pure operational metrics — block rate over time, data-age distribution, CPP drift per program — a **Prometheus + Grafana** (or Grafana Cloud free) pair is lighter than Phoenix and arguably more useful for the non-ML parts. Use both: Phoenix for the ML/parse story, Grafana for the ops story.

---

## 7. Orchestrator & frontend connection

**Orchestrator: FastAPI.** One service exposes the pipeline to the UI:

- `POST /redemptions` — `{origin, dest, cabin, miles, cash, fees}` → runs engines → verifies → ranks → returns the leaderboard + verdict.
- `GET /status/{run_id}` — live progress so the UI can show the two engines working (steps 2–3).
- `GET /freshness`, `GET /alerts` — diagnostics surfaced in the UI.
- Honors `offline` (cache-only) and `refresh` flags, and respects per-type cache windows.

**Frontend connection.** The React app calls the FastAPI service. Locally that's `localhost:5173` (Vite) → `localhost:8000` (FastAPI). When you move off-laptop, the same app deploys to **Vercel / Cloudflare Pages / Supabase hosting** (all free tiers) and points at a hosted orchestrator — no frontend rewrite, just an API base-URL change. Keep that base URL in one env var.

---

## 8. The Mileage UI

The accompanying file `mileage-ui-mockup.html` realizes the Passage aesthetic you referenced, reskinned for this project. Design tokens:

- **Palette:** ivory ground `#F5F2E8`, panel `#EFEBDD`, espresso ink `#2E2117`, taupe-brown sub-text `#6B5A48`, soft line `#DCD5C4`. One restrained accent — oxidized gold `#A8822C` — used in exactly one place: the "best verified route" highlight. Boldness spent once, as the design discipline demands.
- **Type:** **Fraunces** (high-contrast old-style serif) for the wordmark and verdict numbers; **Inter** for UI labels and body. The italic Fraunces eyebrow ("Plan a redemption") echoes Passage's "Translate to."
- **Stepper tabs**, mapped to the real pipeline (this is a true sequence, so numbering is honest, not decorative):
  1. **Route** — enter origin, destination, cabin, Capital One miles, and the cash fare to beat.
  2. **Gathering** — both engines run; the UI shows aggregator vs. protected-source progress.
  3. **Cross-check** — consensus, freshness, and disagreement surfaced before any verdict.
  4. **Redemptions** — the ranked CPP leaderboard and the honest verdict (`best` / `comparable` / `portal_only`).
- **Signature:** the route-as-journey framing — Mileage is itself a *passage* from points to a seat — carried by the serif wordmark and the single gold verdict line.

The mockup is responsive, keyboard-focusable, and respects reduced motion. Open it directly in a browser to see the landing state; it's a faithful starting point for the real Vite/React build.

---

## 9. Stack summary

| Layer | Choice | Free? | Local | Cloud path |
|---|---|---|---|---|
| Aggregator fetch | httpx, curl_cffi | ✓ | ✓ | same |
| Protected fetch (Brain) | nodriver, Camoufox/Patchright, curl_cffi | ✓ | ✓ | server-side only |
| Fallbacks | Wayback, RSS (feedparser), PDF (pdfplumber) | ✓ | ✓ | same |
| Verification / graph | NetworkX + your verify/ logic | ✓ | ✓ | same |
| Durable store | SQLite → Turso / Supabase | ✓ | ✓ | Turso / Supabase |
| Hot memory | Redis → Upstash | ✓ | ✓ (Docker) | Upstash |
| Orchestrator | FastAPI | ✓ | ✓ | Fly.io / Render / Railway free-ish |
| Frontend | Vite + React | ✓ | ✓ | Vercel / Cloudflare Pages / Supabase |
| ML observability | Arize Phoenix (OTel) | ✓ | ✓ | Phoenix Cloud / self-host |
| Ops metrics | Prometheus + Grafana | ✓ | ✓ | Grafana Cloud |
| Live availability | seats.aero API | free tier | ✓ | same |

---

## 10. Possible / Possible-but-not-allowed / Not possible

You asked for an explicit map of the boundaries. Here it is.

### ✅ Possible — and fine to build

- Scraping **public** award charts and transfer ratios from aggregators, blogs, RSS, PDFs, and Wayback snapshots.
- **TLS/JA4 impersonation** (`curl_cffi`) and realistic headers — matching a normal browser's fingerprint.
- **Respectful rate-limit handling**: backoff, jitter, the adaptive politeness bandit, source rotation.
- **Automation-protocol-clean browsers** (`nodriver`) and fingerprint-clean browsers (`Camoufox`) for JS-rendered public pages.
- The **orchestration bandit**, the **block classifier**, the **golden-set evals**, the **memory layer**, and the whole **UI / API / cloud-ready** architecture.
- **Reputable** residential/mobile proxies if you genuinely need IP diversity.

### ⚠️ Possible but not allowed / not advisable — listed so you know what to avoid

- **Reverse-engineering and regenerating Akamai's `_abck` sensor payloads, or shipping a turnkey "Akamai bypass."** This is the part Akamai patches weekly; it's where DIY becomes a full-time arms race, and it's the most ToS-hostile thing you could build. I won't write a sensor generator, and you should be wary of any module whose explicit purpose is to forge challenge responses.
- **CAPTCHA-solving farms / human-solver APIs** to defeat challenges — against essentially every site's terms and ethically fraught.
- **Logging into accounts, using credentials, or scraping gated award *space* / personal or account data.** Stay on public, unauthenticated chart data.
- **High-volume hammering** that degrades a source's service — that crosses from scraping into something that looks like an attack, and it's counterproductive (you get banned).
- **Ignoring `robots.txt` or explicit ToS prohibitions** on protected first-party sites. Circumventing access controls can breach ToS (civil) and, depending on jurisdiction, raise computer-misuse exposure. I'm not a lawyer — if you intend to hit WAF'd first-party sources at any scale, get real legal advice on your jurisdiction and those specific terms.
- **Sketchy proxy networks.** Some cheap "residential" pools are built on malware-compromised devices; using them makes you complicit. If you use proxies, use providers that document consented sourcing.
- **Redistributing or selling scraped data** in violation of a source's terms.

### ⛔ Not possible (or not reliably)

- A model that **permanently "solves" Akamai.** It's a moving target updated continuously; no stable bypass exists, so don't architect around one as if it's a fixed dependency.
- **Beating fingerprint + sensor + IP reputation with "just AI"** and no real browser or proxies. The AI orchestrates primitives; it does not substitute for them.
- **Doing the protected scraping from the browser / a static host.** Browsers can't spoof TLS/JA3 and shouldn't hold proxy credentials — the Brain must run server-side. A purely static-hosted frontend can drive the *aggregator path*, but not Engine B.
- **Guaranteeing a winner** when neither engine got verified data. By design, the honest answer there is "portal is your floor," and that's a feature, not a gap.
- **100% uptime against a WAF from a single datacenter IP.** Not achievable; plan for fallback to the aggregator, always.

---

## 11. Build roadmap

Phased so you have a working, honest product early and never block on the hard part.

- **Phase 0 — Working local product (no Brain).** Consolidate Engine A + verification + graph behind FastAPI; add Redis cache in front of SQLite via the repository interface; build the Vite/React UI from the mockup. *Deliverable: enter a route, get a verified verdict, fully local.*
- **Phase 1 — Observability + trust.** OTel spans → Phoenix; golden route set; cross-check/bounds as CI evals; Grafana for block-rate/freshness. *Deliverable: every run is traceable and the anti-hallucination rules are enforced automatically.*
- **Phase 2 — Aggregator politeness bandit.** The low-risk adaptive throttling/source-rotation policy for Engine A. *Deliverable: fewer 429s, fresher data, no evasion.*
- **Phase 3 — The Brain (sandboxed, optional).** The strategy toolbox + hardcoded decision tree + block classifier + bandit, on the explicit understanding of §10. Only for sources that exist *only* behind a WAF. *Deliverable: a research module, isolated, with the boundaries above enforced in code.*
- **Phase 4 — Off-laptop.** Swap adapters: Redis→Upstash, SQLite→Turso/Supabase, deploy orchestrator + frontend. *Deliverable: Mileage runs hosted with no architectural rewrite.*

---

## 12. What I still need from you

To turn this plan into code, the answers that most change the shape:

1. **Single-user or multi-user?** Drives whether Redis/queues/auth are warranted now or later.
2. **How does it run** — on demand, cron, or a long-lived service? Drives local-Redis vs. Upstash.
3. **Do you actually need any WAF'd first-party source,** or do aggregators cover your routes? If they do, Phase 3 may never be worth building.
4. **Legal posture / appetite** for the protected path, and any proxy budget.
5. **Did "Sai" mean Supabase** (assumed here) or something else?

Tell me the first three and I can start writing Phase 0 against your existing `AggregateScraper` structure — the repository interface, the FastAPI orchestrator, and the Redis cache layer — and wire the mockup into a real Vite/React front end.
