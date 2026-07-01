"""Aggregator fetch stack — Engine A (§6).

The real fetch layer behind the aggregator. One `Fetcher.get(url)` returns a
normalized `FetchResult` regardless of how the bytes were obtained, with a
documented fallback chain:

    httpx (plain pages)
      └─ curl_cffi (TLS/JA4 impersonation) ── optional, only if installed
           └─ Wayback Machine snapshot ───── on 403 / 429 / 5xx / network error
                └─ give up (caller rotates to the next source)

`file://` URLs are served from disk so the *exact same* parse/normalize path is
exercised offline and deterministically (the public targets in
`knowledge/sources.yaml` are stand-ins; swapping a `file://` fixture for a real
`https://` page is a config change, not a code change).

No browser, no sensor-forging — that is the Brain (Engine B), quarantined (§8).
"""

from __future__ import annotations

import logging
import os
import random
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from .politeness import PolitenessPolicy

log = logging.getLogger("mileage.aggregator.fetch")

# Realistic desktop-browser User-Agent strings, rotated per request (§6.5). The
# old single self-identifying UA ("MileageAggregator/1.0 …") announced the
# scraper to every target, which lets a site serve different content or a hard
# 403 on sight. Rotating through current Chrome/Safari/Firefox strings removes
# that trivial fingerprint. TLS/JA4-level evasion is still curl_cffi's job (and
# the heavy anti-bot work is the Brain's, §8) — this only fixes the UA tell.
_BROWSER_UAS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

def _accept_encoding() -> str:
    """Advertise ONLY the content-codings we can actually decode.

    httpx decodes `gzip`/`deflate` via stdlib zlib, but needs the optional
    `brotli`/`brotlicffi` (for `br`) and `zstandard` (for `zstd`) packages to
    decompress those. Hardcoding `br` in Accept-Encoding when no Brotli decoder
    is installed is a silent trap: the server honours it and returns a Brotli
    body, httpx can't inflate it, and hands the *raw compressed bytes* back as
    `resp.text`. That looks like a healthy `200 OK` with a >200-byte body but no
    `<table>` — i.e. it masquerades as a parser "selector miss" when it's really
    an undecoded response. Servers like AwardTravelFinder default to `br`, so
    this alone silently zeroed out every ATF HTML chart. Building the header
    from installed decoders keeps the scraper correct even when the optional
    `[aggregator]` extras are absent (it just asks for gzip), and still sends a
    realistic `br`/`zstd` header once they are installed.
    """
    encs = ["gzip", "deflate"]
    if _has_module("brotli") or _has_module("brotlicffi"):
        encs.append("br")
    if _has_module("zstandard"):
        encs.append("zstd")
    return ", ".join(encs)


def _has_module(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


_BASE_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/json;q=0.8,*/*;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": _accept_encoding(),
}


def _headers() -> dict:
    """A fresh request header set with a rotated, realistic browser UA (§6.5)."""
    return {"User-Agent": random.choice(_BROWSER_UAS), **_BASE_HEADERS}


# Back-compat alias for any direct importer; live requests call `_headers()` so
# the User-Agent actually rotates per request.
_DEFAULT_HEADERS = _headers()

# Statuses that mean "blocked / transient" -> try the next link in the chain.
_BLOCK_STATUSES = {403, 429, 500, 502, 503, 504}

# curl_cffi is an OPTIONAL dependency (TLS/JA4 impersonation). Absence is fine.
try:  # pragma: no cover - exercised only when the extra is installed
    from curl_cffi import requests as _curl_requests  # type: ignore

    _HAS_CURL_CFFI = True
except Exception:  # pragma: no cover
    _curl_requests = None
    _HAS_CURL_CFFI = False


@dataclass
class FetchResult:
    """Normalized output of any fetch path (live HTTP, impersonated, archived)."""

    url: str                 # the URL we were asked for
    text: str                # decoded body
    status: int              # HTTP-ish status (200, or 0 for file://)
    final_url: str           # where the bytes actually came from (e.g. Wayback)
    content_type: str = ""
    via: str = "httpx"       # which path produced it: httpx | curl_cffi | wayback | file
    flags: list[str] = field(default_factory=list)
    # Undecoded body. Required for binary formats (PDF) where `text` is a lossy
    # decode and pdfplumber needs the original bytes. None for legacy callers.
    raw: Optional[bytes] = None

    @property
    def ok(self) -> bool:
        return bool(self.text) and (self.status == 200 or self.status == 0)


class Fetcher:
    """Resilient fetcher with a documented, source-rotating fallback chain."""

    # Internet Archive availability API; overridable for tests/self-hosting.
    DEFAULT_WAYBACK_API = "http://archive.org/wayback/available?url="

    def __init__(
        self,
        *,
        politeness: Optional[PolitenessPolicy] = None,
        base_dir: Optional[Path] = None,
        timeout: float = 10.0,
        max_429_retries: int = 2,
        use_wayback: bool = True,
        impersonate: bool = False,
        wayback_api: Optional[str] = None,
        offline: Optional[bool] = None,
        trust_env: Optional[bool] = None,
    ) -> None:
        self.politeness = politeness or PolitenessPolicy()
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self.timeout = timeout
        self.max_429_retries = max_429_retries
        self.use_wayback = use_wayback
        self.wayback_api = wayback_api or self.DEFAULT_WAYBACK_API
        # Only impersonate when explicitly asked AND the optional dep is present.
        self.impersonate = impersonate and _HAS_CURL_CFFI
        # Proxy hygiene (§6): by DEFAULT we do NOT trust the process environment,
        # so httpx ignores ALL_PROXY / HTTP(S)_PROXY / .netrc. A stray
        # `ALL_PROXY=socks5h://…` (or a sandbox's filtering HTTP proxy) would
        # otherwise route — and silently break (`ProxyError`, or a missing
        # `socksio` crash) — every request before it reaches the target. The
        # scraper should not be funneled through a proxy that filters it. An
        # operator who genuinely needs a proxy can opt back in with
        # `trust_env=True` or `MILEAGE_TRUST_ENV=1`.
        if trust_env is None:
            trust_env = os.getenv("MILEAGE_TRUST_ENV", "") in ("1", "true", "yes")
        self.trust_env = trust_env
        # Offline mode: only `file://` fixtures resolve; live HTTP + Wayback are
        # short-circuited to None. This makes the test suite and `mileage eval`
        # deterministic and network-free — the exact same parse path runs, just
        # from disk — and guarantees a run can never hang on a blocked network.
        # An explicit offline=True/False always wins; when left unset (None) we
        # read MILEAGE_OFFLINE from the environment. That keeps a default-built
        # Fetcher hermetic under the test env while letting a test that needs the
        # real HTTP path opt in with offline=False.
        if offline is None:
            offline = os.getenv("MILEAGE_OFFLINE", "") not in ("", "0", "false")
        self.offline = offline

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def get(self, url: str) -> Optional[FetchResult]:
        """Fetch `url`, walking the fallback chain. None means total failure."""
        scheme = urllib.parse.urlparse(url).scheme
        if scheme == "file" or scheme == "":
            return self._get_file(url)

        if self.offline:
            # Network-free: live targets resolve to nothing; only fixtures load.
            log.debug("offline mode: skipping live fetch for %s", url)
            return None

        domain = urllib.parse.urlparse(url).netloc

        # 1) httpx, with adaptive throttle + bounded 429 backoff.
        result = self._get_http(url, domain)
        if result is not None and result.ok:
            return result

        # 2) curl_cffi impersonation (header/TLS-only blocks) — optional.
        if self.impersonate:
            imp = self._get_curl_cffi(url, domain)
            if imp is not None and imp.ok:
                return imp

        # 3) Wayback Machine snapshot — last public, robots-respecting resort.
        if self.use_wayback:
            self.politeness.record_block(domain)
            snap = self._get_wayback(url)
            if snap is not None and snap.ok:
                return snap

        log.info("fetch failed for %s (chain exhausted)", url)
        return None

    def head_ok(self, url: str) -> tuple[bool, int]:
        """Lightweight liveness probe for `--validate-urls` (returns ok, status)."""
        scheme = urllib.parse.urlparse(url).scheme
        if scheme in ("file", ""):
            ok = self._resolve_file(url).exists()
            return ok, (200 if ok else 404)
        if self.offline:
            # Can't probe a live URL with no network; report unknown (status 0),
            # which the caller must NOT treat as a permanent 404 (URL rot).
            return False, 0
        try:
            resp = httpx.head(
                url, headers=_headers(), timeout=self.timeout,
                follow_redirects=True, trust_env=self.trust_env,
            )
            if resp.status_code == 405:  # HEAD not allowed -> tiny GET
                resp = httpx.get(
                    url, headers=_headers(), timeout=self.timeout,
                    follow_redirects=True, trust_env=self.trust_env,
                )
            return resp.status_code < 400, resp.status_code
        except Exception as exc:
            log.info("head_ok failed for %s: %s", url, exc)
            return False, 0

    # ------------------------------------------------------------------ #
    # Fetch paths
    # ------------------------------------------------------------------ #
    def _get_http(self, url: str, domain: str) -> Optional[FetchResult]:
        attempts = self.max_429_retries + 1
        for attempt in range(attempts):
            self.politeness.before_request(domain)
            try:
                resp = httpx.get(
                    url,
                    headers=_headers(),
                    timeout=self.timeout,
                    follow_redirects=True,
                    trust_env=self.trust_env,
                )
            except Exception as exc:
                self.politeness.on_response(domain, 0)
                log.info("httpx error for %s: %s", url, exc)
                return None

            self.politeness.on_response(domain, resp.status_code)
            if resp.status_code == 429 and attempt < attempts - 1:
                log.info("429 from %s; backing off (attempt %d)", domain, attempt + 1)
                continue  # politeness already widened the delay
            if resp.status_code in _BLOCK_STATUSES:
                return None
            if resp.status_code != 200:
                return None
            return FetchResult(
                url=url,
                text=resp.text,
                status=resp.status_code,
                final_url=str(resp.url),
                content_type=resp.headers.get("content-type", ""),
                via="httpx",
                raw=resp.content,
            )
        return None

    def _get_curl_cffi(self, url: str, domain: str) -> Optional[FetchResult]:  # pragma: no cover
        if not _HAS_CURL_CFFI:
            return None
        self.politeness.before_request(domain)
        try:
            resp = _curl_requests.get(
                url, impersonate="chrome", timeout=self.timeout
            )
        except Exception as exc:
            log.info("curl_cffi error for %s: %s", url, exc)
            return None
        self.politeness.on_response(domain, resp.status_code)
        if resp.status_code != 200:
            return None
        return FetchResult(
            url=url,
            text=resp.text,
            status=resp.status_code,
            final_url=url,
            content_type=resp.headers.get("content-type", ""),
            via="curl_cffi",
            flags=["impersonated"],
            raw=resp.content,
        )

    def _get_wayback(self, url: str) -> Optional[FetchResult]:
        """Resolve the closest Internet Archive snapshot and fetch it."""
        api = self.wayback_api + urllib.parse.quote(url)
        try:
            meta = httpx.get(api, timeout=self.timeout, trust_env=self.trust_env).json()
        except Exception as exc:
            log.info("wayback lookup failed for %s: %s", url, exc)
            return None
        snap = (
            meta.get("archived_snapshots", {})
            .get("closest", {})
        )
        if not snap.get("available") or not snap.get("url"):
            return None
        snap_url = snap["url"]
        try:
            resp = httpx.get(
                snap_url, headers=_headers(), timeout=self.timeout,
                follow_redirects=True, trust_env=self.trust_env,
            )
            resp.raise_for_status()
        except Exception as exc:
            log.info("wayback fetch failed for %s: %s", snap_url, exc)
            return None
        return FetchResult(
            url=url,
            text=resp.text,
            status=200,
            final_url=snap_url,
            content_type=resp.headers.get("content-type", ""),
            via="wayback",
            flags=["from_wayback"],
            raw=resp.content,
        )

    # ------------------------------------------------------------------ #
    # file:// (local fixtures stand in for public sources)
    # ------------------------------------------------------------------ #
    def _resolve_file(self, url: str) -> Path:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "file":
            # Percent-decode so paths with spaces (e.g. ".../Complete Mini
            # Project/...", or any as_uri() output) resolve correctly.
            netloc = urllib.parse.unquote(parsed.netloc)
            ppath = urllib.parse.unquote(parsed.path)
            if netloc:
                # file://relative/path -> netloc='relative', path='/path'
                path = Path(netloc + ppath)
            else:
                # file:///abs/path -> netloc='', path='/abs/path'
                path = Path(ppath)
        else:
            path = Path(url)
        if not path.is_absolute():
            path = self.base_dir / path
        return path

    def _get_file(self, url: str) -> Optional[FetchResult]:
        path = self._resolve_file(url)
        if not path.exists():
            log.info("file fixture not found: %s", path)
            return None
        data = path.read_bytes()
        # Decode defensively: a binary fixture (e.g. a .pdf) must not crash the
        # text path. PDF parsing uses `raw`; `text` stays non-empty so `ok` holds.
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        ctype = {
            ".json": "application/json",
            ".html": "text/html",
            ".rss": "application/rss+xml",
            ".xml": "application/xml",
            ".pdf": "application/pdf",
        }.get(path.suffix.lower(), "text/plain")
        return FetchResult(
            url=url, text=text, status=0, final_url=str(path),
            content_type=ctype, via="file", raw=data,
        )
