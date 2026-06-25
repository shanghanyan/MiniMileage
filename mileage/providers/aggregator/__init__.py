"""ENGINE A — the aggregator (real scraper). §6

DEFAULT Layer 3 award space + Layer 4 charts source, FROM PHASE 1. In Phase 0
this package is a placeholder: the curated provider supplies charts/ratios and a
fallback fare so the vertical slice runs without any scraping.

When implemented (Phase 1) it emits the SAME normalized `AwardQuote` contract as
every other provider, so the verification core cannot tell a scrape from an API
call. Fetch stack: httpx + curl_cffi, with Wayback / RSS / PDF fallbacks. No
browser, no sensor-forging — that is the Brain (Engine B), quarantined (§8).
"""
