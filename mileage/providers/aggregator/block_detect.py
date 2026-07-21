"""Bypass layer 1 — classify *what* blocked a fetch (Engine A).

Cheap, deterministic heuristics over status / headers / body. This does NOT
bypass anything; it only labels the failure so ops and (later) Phase 6 Brain
can choose the next action.

Policy mapping (callers decide; this module only classifies):

  rate_limit_429     → politeness / backoff
  tls_or_fingerprint → curl_cffi impersonation
  short_shell / soft → Wayback if the chart is archiveable
  waf_* / captcha / js_challenge (hard) → flag for Brain; do not bypass in A
  undecoded          → install brotli/zstd extras
  network            → egress / DNS / proxy hygiene
"""

from __future__ import annotations

from typing import Mapping, Optional

# Canonical taxonomy (string constants keep JSON / YAML serialization simple).
BLOCK_NONE = "none"
BLOCK_RATE_LIMIT = "rate_limit_429"
BLOCK_HTTP_403 = "http_403"
BLOCK_TLS = "tls_or_fingerprint"
BLOCK_JS_CHALLENGE = "js_challenge"
BLOCK_CAPTCHA = "captcha"
BLOCK_CF = "waf_cloudflare"
BLOCK_AKAMAI = "waf_akamai"
BLOCK_DATADOME = "waf_datadome"
BLOCK_SHORT_SHELL = "short_shell"
BLOCK_UNDECODED = "undecoded"
BLOCK_NETWORK = "network"
BLOCK_UNKNOWN = "unknown"

BLOCK_TYPES = frozenset(
    {
        BLOCK_NONE,
        BLOCK_RATE_LIMIT,
        BLOCK_HTTP_403,
        BLOCK_TLS,
        BLOCK_JS_CHALLENGE,
        BLOCK_CAPTCHA,
        BLOCK_CF,
        BLOCK_AKAMAI,
        BLOCK_DATADOME,
        BLOCK_SHORT_SHELL,
        BLOCK_UNDECODED,
        BLOCK_NETWORK,
        BLOCK_UNKNOWN,
    }
)

# Body / title fingerprints (lowercase).
_CF_BODY = (
    "cdn-cgi/challenge",
    "cf-browser-verification",
    "attention required! | cloudflare",
    "just a moment...",
    "checking your browser before accessing",
    "cf-turnstile",
    "challenge-platform",
)
_CAPTCHA_BODY = (
    "g-recaptcha",
    "hcaptcha",
    "captcha-delivery",
    "px-captcha",
    "geo.captcha-delivery.com",
)
_AKAMAI_BODY = ("access denied", "_abck", "akamai")
_DATADOME_BODY = ("datadome", "dd_cookie", "geo.captcha-delivery")

_MIN_SHELL_BYTES = 200


def _header(headers: Optional[Mapping[str, str]], name: str) -> str:
    if not headers:
        return ""
    # Case-insensitive lookup
    lower = {str(k).lower(): str(v) for k, v in headers.items()}
    return lower.get(name.lower(), "")


def _looks_undecoded(text: str) -> bool:
    if not text:
        return False
    sample = text[:2000]
    printable = sum(1 for c in sample if c.isprintable() or c in "\r\n\t ")
    return (printable / len(sample)) < 0.85


def classify_block(
    *,
    status: int = 0,
    headers: Optional[Mapping[str, str]] = None,
    body: str = "",
    error: Optional[BaseException] = None,
    min_shell_bytes: int = _MIN_SHELL_BYTES,
) -> tuple[str, list[str]]:
    """Return ``(block_type, signals)`` for a fetch attempt.

    ``signals`` are short human-readable reasons that fired (for diagnostics).
    """
    signals: list[str] = []
    body_l = (body or "")[:8000].lower()
    server = _header(headers, "server").lower()
    cf_ray = _header(headers, "cf-ray")
    set_cookie = _header(headers, "set-cookie").lower()

    if error is not None:
        signals.append(f"error:{type(error).__name__}")
        err_s = str(error).lower()
        # TLS / handshake failures often surface as SSLError / ConnectError.
        if any(
            tok in type(error).__name__.lower() or tok in err_s
            for tok in ("ssl", "tls", "certificate", "handshake")
        ):
            signals.append("tls_error")
            return BLOCK_TLS, signals
        return BLOCK_NETWORK, signals

    if status == 429:
        signals.append("status:429")
        return BLOCK_RATE_LIMIT, signals

    # Header-based WAF identity (prefer over generic 403).
    if cf_ray or "cloudflare" in server:
        signals.append("header:cloudflare")
        if any(m in body_l for m in _CF_BODY) or status in (403, 503):
            if any(m in body_l for m in ("turnstile", "cf-turnstile", "challenge")):
                signals.append("body:cf_challenge")
                return BLOCK_CF, signals
            if status in (403, 503) or any(m in body_l for m in _CF_BODY):
                signals.append("body_or_status:cf")
                return BLOCK_CF, signals

    if "akamai" in server or "akamai" in set_cookie or "_abck" in set_cookie:
        signals.append("header:akamai")
        return BLOCK_AKAMAI, signals

    if "datadome" in server or "datadome" in set_cookie or "datadome" in body_l:
        signals.append("header_or_body:datadome")
        return BLOCK_DATADOME, signals

    # Body fingerprints on any status (including sneaky 200 challenge pages).
    if any(m in body_l for m in _CF_BODY):
        signals.append("body:cloudflare_challenge")
        return BLOCK_CF, signals

    if any(m in body_l for m in _CAPTCHA_BODY):
        signals.append("body:captcha")
        return BLOCK_CAPTCHA, signals

    if "cdn-cgi" in body_l and "challenge" in body_l:
        signals.append("body:js_challenge")
        return BLOCK_JS_CHALLENGE, signals

    if _looks_undecoded(body):
        signals.append("body:undecoded")
        return BLOCK_UNDECODED, signals

    if status == 403:
        signals.append("status:403")
        # Soft 403 with Akamai-ish body copy
        if any(m in body_l for m in _AKAMAI_BODY):
            signals.append("body:akamai_ish")
            return BLOCK_AKAMAI, signals
        return BLOCK_HTTP_403, signals

    if status in (500, 502, 503, 504):
        signals.append(f"status:{status}")
        return BLOCK_UNKNOWN, signals

    if status in (200, 0) and body and len(body) < min_shell_bytes:
        signals.append(f"short_body:{len(body)}")
        # Challenge crumbs on a tiny 200
        if any(m in body_l for m in (*_CF_BODY, "challenge", "captcha")):
            signals.append("short_challenge_crumb")
            return BLOCK_JS_CHALLENGE, signals
        return BLOCK_SHORT_SHELL, signals

    if status in (200, 0) and body:
        return BLOCK_NONE, signals

    if status == 0 and not body:
        signals.append("empty_network")
        return BLOCK_NETWORK, signals

    signals.append(f"status:{status}")
    return BLOCK_UNKNOWN, signals
