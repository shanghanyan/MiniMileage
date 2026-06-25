# Engine B — "the Brain" (QUARANTINED)

This package is **import-isolated**. The working product never depends on it.
It is built **last** (Phase 6), and only if a needed source exists *only* behind
a WAF. It is empty in Phase 0 on purpose.

## Hard rules

- `domain/` and `verify/` **must never import** from `providers/brain/`.
  Dependencies point inward only (see `Cursor-Mileage-Plan.md` §4).
- The working pipeline must run, pass, and ship with this package deleted.

## Off-limits (enforced here, see §8)

The following are **not** to be built in this module:

- No `_abck` sensor reverse-engineering / turnkey Akamai bypass / sensor
  payload generation.
- No CAPTCHA-solving farms or human-solver APIs.
- No credentialed/account scraping; stay on public, unauthenticated data.
- No high-volume hammering that degrades a source.
- No ignoring `robots.txt` / ToS on protected first-party sources. Get real
  legal advice before hitting WAF'd first-party sources at scale.
- No malware-sourced ("sketchy residential") proxies.
- No redistribution of scraped data against a source's terms.

## Honest framing

Beating a modern WAF is **not** primarily an AI problem — TLS/JA4, the `_abck`
sensor, and IP reputation are addressed (or not) by tools like `curl_cffi`,
`nodriver`, `Camoufox`/`Patchright`, and reputable proxies, not by models. If
ever built, the AI's only job here is a contextual bandit that *orchestrates*
those tools, rewarded by the real `is_usable()` selector-hit signal. Start with
a hardcoded decision tree; learn later. Always fall back to the aggregator.
