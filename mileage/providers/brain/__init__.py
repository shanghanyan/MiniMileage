"""ENGINE B — "the Brain" — QUARANTINED. §8

Import-isolated by design: the working product NEVER depends on this package.
`domain/` and `verify/` must never import from here. See README.md for the
hard boundaries (no _abck sensor reverse-engineering, no CAPTCHA farms, no
credentialed/account scraping, no high-volume hammering, no ToS/robots.txt
circumvention on protected first-party sources).

Built last (Phase 6), and only if a needed source exists ONLY behind a WAF.
Empty in Phase 0 on purpose.
"""
