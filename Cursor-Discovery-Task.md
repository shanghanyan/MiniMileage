# Task for Cursor — fold discovery into the aggregator + local extractor

> **Mode: PLAN FIRST.** Do not write implementation code yet. First revise
> `Cursor-Mileage-Plan.md` per this brief and present a design + file layout for
> approval. Then build only after I confirm.

A creator intake stub already exists at **`mileage/knowledge/creators.yaml`** —
blog RSS feeds + YouTube channels, with several channel IDs filled in and a few
marked `TODO` (resolve once from the @handle; never guess a `UC…` string). Use it
as the source list for the work below.

---

## 1. Reframe — this is NOT a separate "Engine C"

In the current plan, `providers/aggregator/discovery/` is labelled "Engine C / the
Discovery Agent," implying it's an architectural peer of Engine A (aggregator) and
Engine B (the Brain). It is not. It lives **inside** the aggregator package and
emits the same `AwardQuote` through the same `_build_charts → verify → graph`
pipeline. Unlike `brain/` (genuinely import-isolated and quarantined), discovery is
a normal sub-module of Engine A.

Rewrite §6.1 and Phase 8 of `Cursor-Mileage-Plan.md` so the aggregator has **two
intake modes feeding one pipeline**:

- **(a) deterministic** — known URLs in `sources.yaml` (today's behavior).
- **(b) discovery/ingest** — email + creator blogs + creator video transcripts,
  parsed by a **local** extractor.

Drop the "Engine C" name. Keep the rule that `domain/` and `verify/` never import
from the aggregator. Reuse the existing `Fetcher`, politeness, `store/jobs.py`
queue, Redis `Cache`/`Lock`/`RateLimiter`, and Arize spans — **no new services.**

## 2. Email is a scraping input (`occulosequor@gmail.com`)

- Treat the mailbox as a standing feed. **Any received email is automatically
  ingested**: poll the inbox on a schedule (existing `store/jobs.py` queue; daily
  is fine), pull unread mail, take the HTML body as a document, run it through the
  local extractor like any other page. Provenance `source_name="email:{sender}"`.
- Auth: **IMAP App Password only** (`GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` in
  `.env`). Do **not** build Gmail API / OAuth / Cloud Pub/Sub — polling is enough.
- A subject matching `"{program} devaluation"` / `"award chart change"` bumps that
  program's charts to `stale` in the store immediately.

## 3. Also scrape the creators the mailbox follows — blogs AND transcripts

Read the creator list from `mileage/knowledge/creators.yaml`. For each creator:

- **Blogs:** poll `blog_rss`, fetch new posts via the existing `Fetcher`,
  readability-extract the body. Provenance `source_name="blog:{name}"`.
- **Video transcripts:** discover new videos via the channel RSS feed
  (`https://www.youtube.com/feeds/videos.xml?channel_id=<UC…>`), pull captions with
  **no API key** — `youtube-transcript-api` (preferred) or
  `yt-dlp --write-auto-sub --skip-download`. Treat the transcript as a document.
  Provenance `source_name="yt:{name}"`.
- All three intakes (email, blog, transcript) flow through the **same** extractor →
  `RawChartRow[]` → `_build_charts` → `AwardQuote[]`, flagged `["llm_extracted"]`.
- Resolve the `channel_id: TODO` entries once (load the @handle page, read the
  canonical id) and confirm every feed with `mileage sources --validate-urls --force`.

## 4. Replace the Anthropic extractor with a LOCAL open-source one

No Anthropic API key in the discovery path. The extractor runs locally.
Recommended stack (justify or override in your plan):

- **Model:** `Qwen2.5-7B-Instruct` (or `Qwen2.5-3B-Instruct` for a lighter /
  CPU-friendlier build), served via **Ollama** or **llama.cpp**. 7B QLoRA-trains
  and runs on a single 12 GB consumer GPU.
- **Constrained decoding is mandatory**, not retry-on-failure: force the exact
  schema `[{program, from, to, cabin, miles, roundtrip}]` with a **GBNF grammar**
  (llama.cpp) or **Outlines / XGrammar** (vLLM). Output cannot escape the schema.
- **GLiNER** as a complementary span tagger for entities (program names, cabins,
  city/zone names): it extracts spans verbatim and cannot hallucinate — use it to
  pre-tag candidates and/or cross-validate the LLM's fields.
- Build an `LLMExtractor` interface so the backend is swappable (local Qwen now,
  nothing hardwired). Search keys (`BING`/`SERPAPI`) stay optional; **remove
  `ANTHROPIC_API_KEY` from the discovery path.**

## 5. Anti-hallucination plan (hard requirement)

- Constrained decoding guarantees parsable, schema-valid output.
- **Verbatim-number grounding guard:** reject any row whose `miles` integer does
  not appear literally in the source text. Numbers are the thing we cannot afford
  to invent.
- Keep the cross-check vs. curated `knowledge/charts.yaml`: agreement elevates
  confidence; disagreement flags `sources_disagree_NN%` and demotes. A row built on
  `llm_extracted` can only ever produce `tentative_best`, never `best`, until an
  **independent** source confirms it.
- Prompt the model to omit any row it's unsure of rather than guess.

## 6. Fine-tuning plan (propose, don't run yet)

- **Method:** QLoRA via **Unsloth** on `Qwen2.5-7B-Instruct` (4-bit base, train
  low-rank adapters; fits 12 GB).
- **Dataset:** `(source_text → JSON rows)` pairs built from our own corpus — the
  ATF / 10xtravel chart pages plus a sample of creator posts and transcripts.
  Bootstrap a few hundred examples; hand-correct the numbers; hold out a test
  split. Optionally distill labels once from a stronger model, then human-verify
  (no unverified labels enter training — same no-hallucination contract).
- **Eval:** an offline extraction-accuracy harness (row precision/recall,
  exact-match on `miles`) wired as a CI eval like `mileage/evals.py`, plus Arize
  LLM spans (raw doc in, JSON out) to watch accuracy and where hallucinations get
  caught.

## 7. Deliverables

- Revised `Cursor-Mileage-Plan.md` (§6.1 + Phase 8 reframed as aggregator intake
  modes; local extractor; training plan).
- Proposed file layout under `providers/aggregator/`, e.g.
  `ingest/email_source.py`, `ingest/creators.py`, `ingest/transcripts.py`,
  `extract/local_extractor.py` (+ grammar), `extract/grounding.py`.
- Model-selection justification, constrained-decoding choice, dataset/training/eval
  plan, and the dependency list.

Reuse the existing `Fetcher`, jobs queue, Redis, and Arize. Do **not** modify
`domain/` or `verify/`. Present the plan for approval before writing code.
