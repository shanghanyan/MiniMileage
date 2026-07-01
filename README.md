# Mileage

**A points-to-flights optimizer that tells you the truth about whether transferring your credit card points is actually worth it.**

Redeeming credit card points for flights is a comparison problem hidden behind paywalls, stale blog posts, and airline sites that actively block scrapers. Mileage answers one question honestly, with evidence: *given a route, a cabin, and a points balance, should you transfer to a partner airline or just book the portal floor?* It's built as a federated data pipeline — live scraping, third-party flight/fare APIs, and a verification layer that will not name a winner without provenance — on top of a multi-user-ready backend with Redis-backed caching, shared quota management, and OpenTelemetry tracing.

Every verdict ships with its receipts: source, timestamp, trust weight, and a confidence score. "Just use your portal, transferring isn't worth it" is a valid — and frequent — answer.

---

## Highlights

- **A working, real scraper — not a stub.** The aggregator (`providers/aggregator/`) pulls live award charts from Star Alliance partner pages (Aeroplan, LifeMiles, Turkish, ANA, KrisFlyer) through a resilient fetch stack: `httpx` for plain pages, `curl_cffi` TLS/JA4 impersonation for stricter hosts, and Wayback Machine / RSS / PDF fallbacks when a page is unreachable. An adaptive per-domain throttle backs off on `429`s and rotates sources instead of hammering.
- **Provenance-first verification.** A number only enters the graph if it came from a selector that actually hit real content. Two sources that mirror the same published chart don't count as independent confirmation — cross-checking only counts across genuinely different sources. This is enforced by CI, not just documented: `mileage eval` feeds the verification layer a poisoned dataset (a garbage value, an unsourced datum, a stale chart) and asserts each one gets caught and demoted before it can become a `best`.
- **A second, LLM-assisted data intake — with hallucination guardrails baked in.** Beyond scraping known URLs, the aggregator can also ingest newsletters, creator blog posts, and video transcripts, running each through a local extractor that turns prose into structured chart rows. Every extracted number is checked against the source text verbatim — a `miles` value that doesn't literally appear in the document is dropped, no matter how confident the model is. Extracted data is flagged and demoted relative to directly-scraped data until an independent source confirms it.
- **Multi-user from the storage layer up, not bolted on.** Cache, rate-limiter, and lock are interfaces from day one, so the move from in-process dicts to a shared Redis backend was an adapter swap, not a rewrite. Two users hitting the same route concurrently trigger one live scrape, both served from cache, with a single atomic quota counter shared across every user — verified in `mileage demo-multiuser`.
- **Full-stack, not just a script.** FastAPI backend with bearer auth, a Vite/React frontend, SQLite persistence, OpenTelemetry tracing (Arize AX-compatible) so every run is replayable, and a golden-route regression suite that runs in CI and fails the build on any dishonest answer.
- **70 automated tests, all green, fully offline and hermetic** — the suite pins a deterministic fixture mode so it never depends on (or can be blocked by) a live network call.

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

## What's next

- **Live award-space scraping as the default, not a placeholder.** Award availability currently flows through a stand-in fixture; wiring a real live-inventory source (or the seats.aero partner API) is the top priority so "is there actually a seat" is answered from live data every time, not fixture data.
- **A zone-matrix PDF parser.** The current parser reads wide-table PDFs; several official airline award charts are laid out as zone matrices and need a dedicated extraction path to become a primary source instead of a fallback.
- **Going horizontal across more cards.** Today the transfer graph is Capital One-only; adding Amex Membership Rewards, Chase Ultimate Rewards, Citi ThankYou, and Bilt is largely a data/config change against the existing pipeline — new transfer-ratio and partner-chart entries, no core rewrite.
- **Expanding the discovery intake.** Email newsletter ingestion is live; sweeping creator blogs and video transcripts for additional chart data is built but still opt-in, and upgrading the local extractor to a small open-weight LLM (with the same verbatim-grounding guard) is the natural next step for recall on messier sources.
- **Limited-time transfer-bonus alerts** and true multi-card ranking — "I hold points across three programs, what's the best seat I can book and should I wait for a bonus" — the eventual "Expedia for points" north star.

---

## Design principles

1. **No hallucinations.** A number enters the graph only from a source that produced a verifiable value.
2. **Source-agnostic core.** The domain logic consumes a normalized quote, never a scraper- or API-shaped row — any source can be swapped or removed without touching the core.
3. **Cross-checking requires independence.** Two sources echoing the same published chart is not confirmation.
4. **Graceful degradation.** A useful, honest answer even if a provider — or a whole data layer — returns nothing.
5. **Honest conclusions.** "Just use your portal" is a valid, frequent answer — the tool is willing to tell you not to bother.
6. **Provenance and freshness are first-class.** Every datum carries its source, timestamp, trust weight, and age-decayed confidence.
