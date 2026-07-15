# Mileage

**A points-to-flights optimizer that tells you the truth about whether transferring your credit card points is actually worth it.**

Redeeming credit card points for flights is a comparison problem hidden behind paywalls, stale blog posts, and airline sites that actively block scrapers. Mileage answers one question honestly, with evidence: *given a route, a cabin, and a points balance, should you transfer to a partner airline or just book the portal floor?* It's built as a federated data pipeline — live scraping, third-party flight/fare APIs, and a verification layer that will not name a winner without provenance — on top of a multi-user-ready backend with Redis-backed caching, shared quota management, and OpenTelemetry tracing.

Every verdict ships with its receipts: source, timestamp, trust weight, and a confidence score. "Just use your portal, transferring isn't worth it" is a valid — and frequent — answer.

---

## Highlights

- **A working, real scraper — not a stub.** The aggregator (`providers/aggregator/`) pulls live award charts from Star Alliance partner pages (Aeroplan, LifeMiles, Turkish, ANA, KrisFlyer, EVA) through a resilient fetch stack: `httpx` for plain pages, `curl_cffi` TLS/JA4 impersonation for stricter hosts, and Wayback Machine / RSS / PDF fallbacks when a page is unreachable. An adaptive per-domain throttle backs off on `429`s and rotates sources instead of hammering. 15 chart targets in `knowledge/sources.yaml` as of 2026-07-08; two (the EVA and KrisFlyer Star-Alliance 10xtravel pages) were added and reasoned through against the real parser this session but not yet confirmed by an actual live fetch — run `mileage sources --validate-urls --deep` to get a live read.
- **Provenance-first verification.** A number only enters the graph if it came from a selector that actually hit real content. Two sources that mirror the same published chart don't count as independent confirmation — cross-checking only counts across genuinely different sources. This is enforced by CI, not just documented: `mileage eval` feeds the verification layer a poisoned dataset (a garbage value, an unsourced datum, a stale chart) and asserts each one gets caught and demoted before it can become a `best`.
- **A second, LLM-assisted data intake — with hallucination guardrails baked in.** Beyond scraping known URLs, the aggregator can also ingest newsletters, creator blog posts, and video transcripts, running each through a local extractor that turns prose into structured chart rows. Every extracted number is checked against the source text verbatim — a `miles` value that doesn't literally appear in the document is dropped, no matter how confident the model is. Extracted data is flagged and demoted relative to directly-scraped data until an independent source confirms it.
- **Multi-user from the storage layer up, not bolted on.** Cache, rate-limiter, and lock are interfaces from day one, so the move from in-process dicts to a shared Redis backend was an adapter swap, not a rewrite. Two users hitting the same route concurrently trigger one live scrape, both served from cache, with a single atomic quota counter shared across every user — verified in `mileage demo-multiuser`.
- **Full-stack, not just a script.** FastAPI backend with bearer auth, a Vite/React frontend, SQLite persistence, OpenTelemetry tracing (Arize AX-compatible) so every run is replayable, and a golden-route regression suite that runs in CI and fails the build on any dishonest answer.
- **100 automated tests (96 passing offline, 4 skipped live-network-only checks), fully hermetic** — the suite pins a deterministic fixture mode so it never depends on (or can be blocked by) a live network call.

---

## How it works

```
route + cabin + points  →  provider registry  →  verification (provenance, trust, freshness)  →  graph + verdict
                            (scraper, flight APIs,                                                (portal_only /
                             curated award charts)                                                comparable / best)
```

The domain logic (`domain/`) never imports from any data source — every provider, scraper, and extractor plugs into one interface and can be deleted without touching the core. That separation is what makes the honesty guarantees possible: the verification layer treats a live scrape and a $-API response identically, and can't be talked into trusting one over the other except by evidence (source trust weight, freshness, independence).

```
mileage/
  domain/      # pure logic: models, transfer ratios, cents-per-point math, verdict rules
  providers/   # every data source behind one interface
    aggregator/  # the real scraper — fetch, parse, politeness/throttling, email + blog + transcript intake
  verify/      # provenance, trust, freshness, cross-checking, anti-hallucination bounds
  graph/       # route graph + ranking (NetworkX)
  store/       # SQLite persistence + swappable Cache/RateLimiter/Lock (in-process or Redis)
  api/         # FastAPI backend + bearer auth
  cli.py       # full pipeline, no web stack required
ui/            # Vite + React frontend
```

---

## Setup

Requires Python 3.10+.

```bash
git clone <this-repo>
cd "Complete Mini Project"
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

That installs the core (CLI, API, scraper with `httpx`, SQLite). Everything runs with **zero API keys** — without external keys the fare chain falls back to a curated, clearly-flagged estimate instead of guessing.

**Optional extras**, install any combination:

```bash
pip install -e ".[aggregator]"     # curl_cffi TLS impersonation, PDF/RSS parsing, brotli/zstd decoding
pip install -e ".[discovery]"      # blog + transcript ingestion (readability extraction, YouTube captions)
pip install -e ".[multiuser]"      # Redis-backed cache/quota/locks for the multi-user backend
pip install -e ".[observability]"  # ship OpenTelemetry traces to Arize AX
pip install -e ".[dev]"            # pytest
```

Each extra degrades gracefully when absent — e.g. no `curl_cffi` just turns off TLS impersonation rather than crashing; no Redis URL falls back to in-process caching automatically.

### Run it

```bash
# Both flagship demos side by side: the "honest floor" case and the "hidden value" case
python -m mileage.cli demo

# One route, from the command line
python -m mileage.cli quote --from LAX --to IST --cabin business \
    --currency capital_one --miles 90000 --card venture_x

# Machine-readable
python -m mileage.cli quote --from LAX --to IST --cabin business --miles 90000 --json
```

Sample output — the "hidden value" case, where transferring beats the portal by 638%, backed by a live confirmed seat:

```
VERDICT: best — TRANSFER WINS
Capital One -> turkish returns 9.22c/pt vs the 1.25c/pt portal floor (638% better).
Live award space: turkish 45,000mi (2 seats)
```

And the "honest floor" case, where the tool correctly tells you not to bother:

```
VERDICT: comparable — COMPARABLE — transfer roughly ties the portal
Capital One -> lifemiles (1.30c/pt) is within 20% of the portal floor (1.25c/pt).
```

### Run the full stack (API + web UI)

```bash
# Terminal 1
uvicorn mileage.api.app:app --reload --port 8000

# Terminal 2
cd ui && npm install && npm run dev
```

Open `http://localhost:5173`.

### Run the test suite / CI honesty gate

```bash
pip install -e ".[dev]"
pytest                              # 70 tests, offline and hermetic

python -m mileage.cli eval          # golden-route regression + extraction-accuracy gate
python -m mileage.cli demo-observability   # watch the anti-hallucination guard reject a poisoned dataset live
```

### Other useful commands

```bash
python -m mileage.cli providers                    # provider health, quota used/remaining
python -m mileage.cli sources --validate-urls       # scraper target health check (URL rot detection)
python -m mileage.cli demo-degrade                  # disable/exhaust a provider mid-run, watch graceful fallback
python -m mileage.cli demo-multiuser                # two concurrent users, one shared scrape, per-user verdicts
python -m mileage.cli discover --dry-run            # preview rows extracted from the newsletter/blog intake
```

---

## What's built so far

- End-to-end verified quote pipeline: route → live/curated data → cross-checked verdict, with full provenance
- A real scraper with resilient fetching, adaptive politeness, and multi-format parsing (HTML tables, JSON, RSS, PDF)
- Provider federation with quota guards, caching, and ordered fallbacks — no single source can crash a run
- FastAPI backend + React frontend running the identical pipeline as the CLI
- Multi-user backend: shared cache, global quota counter, per-user balances, bearer auth, Redis-swappable storage
- OpenTelemetry tracing and a CI-enforced golden-route regression suite that fails the build on any hallucinated or unsourced answer
- A secondary data-discovery pipeline (newsletters, blog posts, video transcripts) with a verbatim-grounding guard so extracted numbers can never be invented
- Four additional transferable currencies (Amex MR, Chase UR, Citi ThankYou, Bilt) wired into the same ratio graph as Capital One — see the dormant-features note below on their source confidence
- An eval harness for the discovery extractor (`mileage/extraction_eval.py`) plus a swappable `OllamaExtractor` skeleton — see "Built but dormant" below

## Built but dormant

Some things are fully coded and wired but don't do anything useful yet, either for lack of an API key or because they're intentionally unfinished. Worth knowing before assuming a feature is live:

- **Amadeus** (the primary cash-fare + schedules source) — `AMADEUS_CLIENT_ID`/`AMADEUS_CLIENT_SECRET` aren't set, so it reports `DOWN` and every quote's "price to beat" currently comes from the lower-trust `travelpayouts`/curated-fallback fare path instead of a live fare API.
- **seats.aero** (the optional paid live-award-space upgrade) — `SEATS_AERO_API_KEY` isn't set, so live award availability is 100% the `starnet_award_space.json` fixture; there is no real live-seat source in this deployment today.
- **URL rediscovery** (`mileage sources --rediscover`) — fully built (rot detection, search-then-validate-before-adopt), but dormant: no `SERPAPI_API_KEY`/`BING_SEARCH_API_KEY` and `MILEAGE_URL_REDISCOVERY` isn't set, so a rotted source is detected but never auto-replaced.
- **Duffel and AeroDataBox** — named in the plan as fallback providers, never scaffolded (no `providers/duffel.py` or equivalent exists).
- **The local LLM extractor** (Qwen2.5 via Ollama, §6.2/§6.3) — a real `OllamaExtractor` + GBNF grammar + eval harness now exist (`providers/aggregator/extract/local_extractor.py`, `extract/grammar.gbnf`, `mileage/extraction_eval.py`), but none of it has been exercised against a running model — every discovery intake still defaults to the keyless `DeterministicExtractor` unless `MILEAGE_EXTRACTOR_BACKEND=ollama` is set AND Ollama is actually running. See `Cursor-LLM-Extractor-Task.md` for what's left.
- **Chase/Amex/Citi/Bilt ratios** — real and sourced (issuer support pages / Award Travel Finder / The Points Guy, all fetched and dated in `knowledge/ratios.yaml`), but at lower trust than the Capital One block: the official issuer transfer-partner pages are JS-rendered and couldn't be fetched directly to cross-check against secondary aggregators. Treat these four as single-sourced until re-verified against the issuer's own page.
- **Arize tracing** — `ARIZE_SPACE_ID`/`ARIZE_API_KEY` are set, but the `arize-otel`/`openinference` packages need the `observability` extra actually installed on whatever machine runs this for tracing to do anything (`pip install -e ".[observability]"`).
- **aviationstack** — not dormant so much as deliberately a permanent stub (`ProviderHealth.DOWN` always); the plan calls it a weak-enough schedules fallback not worth building.
- **The Brain (Engine B)** — Phase 6, intentionally quarantined and unbuilt; the working product never depends on it.

## What's next

- **Live award-space scraping as the default, not a placeholder.** Award availability currently flows through a stand-in fixture; wiring a real live-inventory source (or the seats.aero partner API) is the top priority so "is there actually a seat" is answered from live data every time, not fixture data.
- **A zone-matrix PDF parser.** The current parser reads wide-table PDFs; several official airline award charts are laid out as zone matrices and need a dedicated extraction path to become a primary source instead of a fallback.
- **Going horizontal across more cards — data added 2026-07-08, needs re-verification.** Amex Membership Rewards, Chase Ultimate Rewards, Citi ThankYou, and Bilt are now wired into `knowledge/ratios.yaml` alongside Capital One (confirmed this was purely a data/config change, no core rewrite — see `curated.py`/`config.py`), but sourced from secondary aggregators rather than each issuer's own page; re-verify against the official pages before trusting them at more than single-source confidence.
- **Expanding the discovery intake.** Email newsletter ingestion is live; sweeping creator blogs and video transcripts for additional chart data is built but still opt-in. A local-LLM extractor skeleton (`OllamaExtractor`, GBNF grammar, eval harness) now exists but is unverified against a real running model — see `Cursor-LLM-Extractor-Task.md` for the remaining integration and fixture-collection work before it can replace the deterministic default.
- **Limited-time transfer-bonus alerts** and true multi-card ranking — "I hold points across three programs, what's the best seat I can book and should I wait for a bonus" — the eventual "Expedia for points" north star.

---

## Design principles

1. **No hallucinations.** A number enters the graph only from a source that produced a verifiable value.
2. **Source-agnostic core.** The domain logic consumes a normalized quote, never a scraper- or API-shaped row — any source can be swapped or removed without touching the core.
3. **Cross-checking requires independence.** Two sources echoing the same published chart is not confirmation.
4. **Graceful degradation.** A useful, honest answer even if a provider — or a whole data layer — returns nothing.
5. **Honest conclusions.** "Just use your portal" is a valid, frequent answer — the tool is willing to tell you not to bother.
6. **Provenance and freshness are first-class.** Every datum carries its source, timestamp, trust weight, and age-decayed confidence.
