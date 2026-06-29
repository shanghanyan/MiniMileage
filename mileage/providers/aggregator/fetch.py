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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from .politeness import PolitenessPolicy

log = logging.getLogger("mileage.aggregator.fetch")

_DEFAULT_HEADERS = {
    "User-Agent": (
        "MileageAggregator/1.0 (+https://example.org/mileage; polite scraper)"
    ),
    "Accept": "text/html,application/json,application/xml;q=0.9,*/*;q=0.8",
}

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
    ) -> None:
        self.politeness = politeness or PolitenessPolicy()
        self.base_dir = Path(base_dir) if base_dir else Path.cwd()
        self.timeout = timeout
        self.max_429_retries = max_429_retries
        self.use_wayback = use_wayback
        self.wayback_api = wayback_api or self.DEFAULT_WAYBACK_API
        # Only impersonate when explicitly asked AND the optional dep is present.
        self.impersonate = impersonate and _HAS_CURL_CFFI

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def get(self, url: str) -> Optional[FetchResult]:
        """Fetch `url`, walking the fallback chain. None means total failure."""
        scheme = urllib.parse.urlparse(url).scheme
        if scheme == "file" or scheme == "":
            return self._get_file(url)

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
        try:
            resp = httpx.head(
                url, headers=_DEFAULT_HEADERS, timeout=self.timeout,
                follow_redirects=True,
            )
            if resp.status_code == 405:  # HEAD not allowed -> tiny GET
                resp = httpx.get(
                    url, headers=_DEFAULT_HEADERS, timeout=self.timeout,
                    follow_redirects=True,
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
                    headers=_DEFAULT_HEADERS,
                    timeout=self.timeout,
                    follow_redirects=True,
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
        )

    def _get_wayback(self, url: str) -> Optional[FetchResult]:
        """Resolve the closest Internet Archive snapshot and fetch it."""
        api = self.wayback_api + urllib.parse.quote(url)
        try:
            meta = httpx.get(api, timeout=self.timeout).json()
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
                snap_url, headers=_DEFAULT_HEADERS, timeout=self.timeout,
                follow_redirects=True,
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
        )

    # ------------------------------------------------------------------ #
    # file:// (local fixtures stand in for public sources)
    # ------------------------------------------------------------------ #
    def _resolve_file(self, url: str) -> Path:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "file":
            if parsed.netloc:
                # file://relative/path -> netloc='relative', path='/path'
                path = Path(parsed.netloc + parsed.path)
            else:
                # file:///abs/path -> netloc='', path='/abs/path'
                path = Path(parsed.path)
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
        text = path.read_text(encoding="utf-8")
        ctype = {
            ".json": "application/json",
            ".html": "text/html",
            ".rss": "application/rss+xml",
            ".xml": "application/xml",
            ".pdf": "application/pdf",
        }.get(path.suffix.lower(), "text/plain")
        return FetchResult(
            url=url, text=text, status=0, final_url=str(path),
            content_type=ctype, via="file",
        )
