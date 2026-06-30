# Task for Cursor — finish discovery intake, fix the live-data gaps, add LLM URL-rediscovery

> **Mode: PLAN FIRST.** Do not write implementation code yet. First read this
> brief end-to-end, reconcile it against the current code, update the relevant
> sections of `Cursor-Mileage-Plan.md`, and present a design + file layout +
> test plan for approval. Build only after I confirm.
>
> **Hard rules (unchanged from `Cursor-Discovery-Task.md`):** `domain/` and
> `verify/` never import from `providers/`. Reuse the existing `Fetcher`,
> `politeness`, `store/jobs.py` queue, Redis `Cache`/`Lock`/`RateLimiter`, and
> Arize spans — **no new services.** Discovery stays a normal sub-module of the
> aggregator (Engine A), not a separate engine, not import-isolated like
> `brain/`. Keep chart *extraction* deterministic (the existing
> `extract/deterministic.py` + verbatim-number grounding) — **do not** add a
> local LLM to the extraction path. The only new LLM use is URL rediscovery
> (§D), and only when sources rot.

---

## Context — current state (verified 2026-06-30)

- Phases 0–5 built; offline suite green (**49/49**, `MILEAGE_OFFLINE=1` via
  `tests/conftest.py`).
- Phase 8 email intake built: `providers/aggregator/ingest/email_source.py`
  (IMAP App-Password poll of `occulosequor@gmail.com`, `.eml` fixture fallback),
  `extract/deterministic.py` (keyless extractor) + `extract/grounding.py`
  (verbatim-number guard), `mileage discover` writes
  `knowledge/discovered_charts.json`, consumed by `AggregatorProvider` as
  `llm_extracted` quotes. Email confirmed connecting live from the terminal.
- **Not built:** `ingest/creators.py` (blogs), `ingest/transcripts.py`
  (YouTube), `ingest/devaluation.py`. The Brain remains a stub.

This task: build the three missing intakes, fix the live-data gaps that stop
scraped charts from becoming route quotes, harden the issues found in review,
and add cost-bounded LLM URL rediscovery.

---

## A. Root-cause fix — why live URL scraping yields no route quotes (do this FIRST)

**Symptom:** the email/fixture path produces verdicts, but scraping real ATF /
10xtravel chart pages produces parsed rows that never turn into route quotes.

**Root cause (confirmed in code, not network):** there is no region-label
canonicalization layer between the parser and the chart lookup.

- `parse.py` emits chart rows with `region_a/region_b = cells[...].strip().lower()`
  — i.e. the *raw* table text: `"north america"`, `"atlantic"`,
  `"within north america"`, `"europe 1"`, or distance bands like `"0–500 miles"`.
- `domain/charts.py::lookup_award_miles` resolves a route by mapping its airports
  to canonical region tokens via `charts.yaml::region_map`
  (`{north_america, europe, north_asia}`), then matches a band only when
  `_bands_match` finds the band's region pair **exactly equal** to the route's
  canonical pair (`sorted lower-case compare`).
- `"north america"` (space) ≠ `"north_america"` (underscore), `"atlantic"` and
  distance bands have no canonical token at all → `_bands_match` always fails →
  `ChartHit` is `None` → no quote.
- The fixtures resolve **only** because they are hand-authored with the canonical
  snake_case tokens. Real pages will never match. This is the bug.

**Fix — add a region normalizer (parser-side, not in `domain/`/`verify/`):**

1. New `providers/aggregator/regions.py` (or extend `parse.py`): a
   `canonicalize_region(label: str) -> str | None` that maps human zone labels to
   the canonical tokens used in `region_map`/charts.yaml. Cover at least:
   `north america | usa | continental us | within north america → north_america`;
   `europe | atlantic | eu → europe`; `north asia | japan | korea →
   north_asia`; plus the obvious aliases each ATF/10x page uses. Return `None`
   for anything unmapped (drop the row rather than guess — same no-hallucination
   contract).
2. Apply it where rows are built (`parse.py` html/json/wide parsers): store the
   canonical token in `region_a/region_b`, and **drop** rows whose region can't be
   canonicalized (count them — see eval below).
3. Expand `charts.yaml::region_map` beyond the current 12 airports to a realistic
   set (top ~50 award-relevant airports across NA / Europe / North Asia / SE Asia
   / etc.), still sourced (no invented geography).
4. **Distance-banded charts (Aeroplan):** the region-pair band model cannot
   represent distance bands. Either (a) add a distance-band resolver
   (`great-circle miles between airports → band`) as a separate code path in
   `domain/charts.py` keyed by a `band_type: distance` chart shape, or (b)
   explicitly mark Aeroplan chart scraping as unsupported for now and rely on the
   curated Aeroplan band + discovery rows. State which in your plan.

**Acceptance:** add a fixture that mirrors a *real* ATF table's raw labels (spaces,
title-case, an unmapped zone). After the fix, the mappable rows resolve to a route
quote; the unmapped row is dropped and counted, not silently mismatched. Without
the fix this fixture must fail — prove the bug first, then the fix.

---

## B. Build `ingest/creators.py` — blog intake

Read `knowledge/creators.yaml`. For each creator with a `blog_rss`:

- Poll the feed (`feedparser`), diff new post URLs against the `Cache` (de-dupe by
  URL within TTL; `Lock`/`SETNX` so two runs don't extract the same post).
- Fetch each new post via the **existing `Fetcher.get()`** (unchanged politeness /
  Wayback / `file://` fixture support), readability-extract the body
  (`trafilatura` preferred, `readability-lxml` fallback — match whatever
  `email_source.py` already uses for HTML→text).
- Run the body through the existing `extract/deterministic.py` →
  `RawChartRow[]` → `_build_charts` → `AwardQuote[]`, flag `["llm_extracted"]`,
  provenance `source_name="blog:{name}"`.
- Confirm every `blog_rss` with `mileage sources --validate-urls --force` and only
  rely on feeds that return a real feed body (not an HTML 200 error page) — see §C.

## C. Build `ingest/transcripts.py` — YouTube transcript intake

For each creator with a `youtube.channel_id`:

- Discover new videos via the channel RSS feed
  `https://www.youtube.com/feeds/videos.xml?channel_id=<UC…>` (`feedparser`).
- Pull captions with **no API key**: `youtube-transcript-api` (preferred),
  `yt-dlp --write-auto-sub --skip-download` as fallback. De-dupe by video id via
  `Cache`.
- Treat the transcript text as a document → same deterministic extractor →
  `AwardQuote[]`, flag `["llm_extracted"]`, provenance `source_name="yt:{name}"`.
- **Resolve the `channel_id: TODO` entries once** (`lets_get_to_the_points`,
  `award_travel_101`): load `youtube.com/<handle>`, read the canonical `UC…` id
  from the page, write it into `creators.yaml`. **Never guess a UC string.** If a
  handle can't be resolved, leave it `TODO` and skip it (don't fabricate).

## D. Build `ingest/devaluation.py` — proactive stale fast-path

- A subject/title matching `"{program} devaluation"` / `"award chart change"`
  (case-insensitive, program from the known program list) immediately bumps that
  program's charts to `stale` in the store, instead of waiting for the next run.
- Mechanism lives in `store/` (a `marked_stale_at` per program, as
  `Cursor-Mileage-Plan.md` §6.2 already specifies). `AggregatorProvider`
  consults it on emit and attaches the `stale` flag + caps `source_updated_at`
  before the freshness cutoff. **No `domain/`/`verify/` changes** —
  `verify/crosscheck.py` already demotes `stale`.
- Wire it into the email intake (subjects) and the blog/transcript intakes
  (titles). Add a test with a planted "Turkish devaluation" email that flips
  Turkish charts to `stale` and demotes a Turkish row from `best` to
  `tentative_best`/lower.

## E. Wire all intakes into `mileage discover`

`mileage discover` must run all four intakes (email + blogs + transcripts +
devaluation) off the `store/jobs.py` queue, emit a `discover` CHAIN span with a
child per extraction (Arize, additive/no-op without creds), and write the merged
rows to `discovered_charts.json`. Each emitted row carries its `email:`/`blog:`/
`yt:` provenance and the `llm_extracted` flag, and can only ever produce
`tentative_best` until an **independent** source confirms it (cross-check vs.
`charts.yaml`; a blog and a transcript echoing the same post are NOT independent —
key independence on `source_name`).

---

## F. LLM URL rediscovery — deterministic by default, LLM only on rot

Goal: deterministic scraping of known-good URLs is the default and the cheap path.
An LLM web search is invoked **only** when a source rots, never on the hot path.

1. **Rot detection (deterministic, no LLM):** track per-source health already in
   `store` (`last_404`, `last_status`, `last_checked`). Add a consecutive-failure
   counter and a "selector-miss" signal (fetched 200 but the parser produced 0
   rows — see §C/§G). A source is *rotted* when `last_404` is true OR it has
   N consecutive failures OR M consecutive selector-misses (make N/M config).
2. **Trigger:** only when rotted-source count crosses a threshold (e.g. ≥1 hard-404
   target, or a configurable ratio) does the rediscovery job run. Gate it behind a
   flag (`MILEAGE_URL_REDISCOVERY=1`) and the optional search keys already stubbed
   (`BING_SEARCH_API_KEY` / `SERPAPI_API_KEY`); with no key it's a no-op.
3. **LLM step (bounded):** for each rotted source, an LLM + web search proposes
   candidate replacement URLs for that program/chart (e.g. "Turkish Miles&Smiles
   partner award chart"). The LLM **only proposes URLs** — it never extracts chart
   numbers (extraction stays deterministic + grounded).
4. **Mandatory validation before adoption:** every proposed URL must pass
   `validate_targets` (real fetch, not 404) **and** a content check — the
   structural parser must actually hit (≥1 canonicalizable row, §A/§G). Only then
   is it written into `sources.yaml` (with a low initial trust + a
   `discovered_url` provenance note). A proposed URL that doesn't parse is
   discarded, never adopted on faith.
5. **Cost control:** rediscovery runs off the jobs queue, rate-limited, cached
   (don't re-search the same rotted source within TTL), and is never on the
   request path. Document the expected call volume.

State in your plan which search backend you'll use and the exact rot thresholds.

---

## G. Harden `--validate-urls` (it currently can't catch rot)

Current behavior: `validate_targets` flags only HTTP 404/410 as rot; an
unreachable host returns `status=0` and is reported `[ok]`, and a page that loads
but stopped serving the expected table also reports `[ok]`. So real rot is
invisible.

- Add a **content-validation mode** (`--validate-urls --deep` or always-on for
  non-fixture targets): after a 200, run the target's structural parser and
  require ≥1 canonicalizable row (a "selector hit"). 200-but-zero-rows =
  `selector_miss`, surfaced distinctly and fed into §F rot detection.
- In the display, distinguish **`ok` / `unreachable (status 0)` / `rotted (404)` /
  `selector_miss`** instead of collapsing the last three into `ok`. Keep the
  existing rule that a transient `status=0` does not *permanently* disable a
  source — but it must no longer be printed as `ok`.

---

## H. Bug fixes & hygiene (concrete, low-risk)

1. **`.env` secret hygiene (do this regardless of the rest).** The committed-on-
   disk `.env` currently contains, in cleartext, the Gmail App Password, the
   Upstash Redis URL **with token**, the Arize API key, **and the account login
   password written in a plain comment** (`#normal: …`). Actions:
   - Remove the password-in-comment line entirely.
   - I (the human) will **rotate** the Gmail App Password, the account password,
     the Upstash token, and the Arize key — flag this in your plan as a required
     manual step; Cursor should not handle live secrets.
   - Confirm `.env` is gitignored (it is) and add a `.env.example` diff if any new
     keys are introduced.
2. **`MILEAGE_REDIS_URL` line.** The value is populated (a real Upstash URL), so
   every run attempts Redis; if the `multiuser` extra isn't installed it logs a
   fallback warning each run. Decide intent: either `pip install -e ".[multiuser]"`
   and keep it, or comment the line out for single-user. (The leading space after
   `=` is cosmetic — dotenv strips it — but tidy it.) Same cosmetic leading-space
   on `ARIZE_SPACE_ID`.
3. **SQLite "disk I/O error" — likely NOT a real bug for you.** It only reproduces
   when `mileage.db` lives on a FUSE/synced mount (it surfaced in the review
   sandbox). On a normal local path on your Mac it's fine. Low-priority defensive
   options: open SQLite in WAL mode and wrap `commit()` with a clearer error that
   names `MILEAGE_DB`. Don't over-engineer.

---

## I. Tests & evals (CI gates, offline/deterministic)

- New `tests/test_phase8b.py`: region-canonicalization (§A, incl. the
  bug-proving real-label fixture), blog intake, transcript intake (mock the
  caption fetch with a fixture), devaluation fast-path, and `--validate-urls`
  content-mode (`ok`/`unreachable`/`rotted`/`selector_miss`).
- Extend the extraction-accuracy eval (`mileage/evals.py` style): row
  precision/recall + exact-match on `miles` + a dropped-row counter (unmapped
  regions, failed grounding). Must **exit non-zero on regression**.
- URL rediscovery (§F): test the rot-trigger logic and the
  validate-before-adopt gate **offline** (mock the search backend; never hit the
  network in tests). The whole suite stays hermetic under `MILEAGE_OFFLINE=1`.

## J. Deliverables

1. Updated `Cursor-Mileage-Plan.md` (§6.1/§6.2/Phase 8): region normalizer,
   blog+transcript+devaluation intakes, the §F URL-rediscovery design, the §G
   validator hardening.
2. New code under `providers/aggregator/`: `regions.py` (or `parse.py`
   extension), `ingest/creators.py`, `ingest/transcripts.py`,
   `ingest/devaluation.py`, the rediscovery job, the validator content-mode.
3. Resolved `channel_id` values in `creators.yaml` (real, read from handles).
4. `tests/test_phase8b.py` + extended evals, all green offline.
5. A short "live verification checklist" I can run on my Mac (network present):
   `mileage discover` shows live IMAP + new blog/transcript rows;
   `mileage sources --validate-urls --deep` shows real `ok`/`selector_miss`
   states; one real ATF URL now produces a route quote end-to-end.

> Present the plan + file layout + test plan for approval before writing code.
