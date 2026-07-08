"""Follow blog and YouTube links found inside newsletter email bodies (§6.1).

When a newsletter links to a full article or video instead of embedding chart
numbers inline, this module fetches those targets and runs the same grounded
extractor used everywhere else in discovery intake.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Callable, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .creators import readable_text
from .transcripts import default_caption_fetcher

if TYPE_CHECKING:
    from .email_source import EmailDocument

log = logging.getLogger("mileage.aggregator.ingest.email_links")

CaptionFetcher = Callable[[str], Optional[str]]

_YT_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
)
_SKIP_HOST_FRAGMENTS = (
    "unsubscribe", "list-manage", "mailchi.mp", "click.email",
    "facebook.com", "twitter.com", "x.com", "instagram.com", "linkedin.com",
    "pinterest.com", "tiktok.com", "fonts.googleapis",
)
_SKIP_PATH_FRAGMENTS = ("/unsubscribe", "/preferences", "/optout", "/login")
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")


class _HrefParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.hrefs.append(href.strip())


def _unwrap_redirect(url: str) -> str:
    """Unwrap common newsletter redirect wrappers (Google, etc.)."""
    parsed = urlparse(url)
    if parsed.netloc.endswith("google.com") and parsed.path == "/url":
        target = parse_qs(parsed.query).get("q", [None])[0]
        if target:
            return unquote(target)
    return url


def extract_links(body: str) -> list[str]:
    """Pull candidate http(s) links from HTML hrefs and plain-text URLs."""
    seen: set[str] = set()
    ordered: list[str] = []

    def _add(raw: str) -> None:
        url = _unwrap_redirect(raw.strip())
        if not url.startswith(("http://", "https://", "file://")):
            return
        if url in seen:
            return
        seen.add(url)
        ordered.append(url)

    if "<" in body and "href=" in body.lower():
        parser = _HrefParser()
        try:
            parser.feed(body)
        except Exception:
            pass
        for href in parser.hrefs:
            _add(href)

    for match in re.finditer(r"https?://[^\s<>\"']+", body):
        _add(match.group(0).rstrip(".,;)\"'"))

    return ordered


def youtube_video_id(url: str) -> Optional[str]:
    """Return a YouTube video id when ``url`` points at a watch/shorts/youtu.be link."""
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host in {"youtu.be", "www.youtu.be"}:
        vid = parsed.path.lstrip("/").split("/")[0]
        return vid or None
    if host not in _YT_HOSTS:
        return None
    if parsed.path == "/watch":
        return parse_qs(parsed.query).get("v", [None])[0]
    parts = parsed.path.strip("/").split("/")
    if len(parts) >= 2 and parts[0] in {"shorts", "embed", "live"}:
        return parts[1] or None
    return None


def is_followable_blog_url(url: str) -> bool:
    """True for article-like http(s)/file URLs that are not YouTube or noise."""
    if youtube_video_id(url):
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https", "file"}:
        return False
    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if any(frag in host for frag in _SKIP_HOST_FRAGMENTS):
        return False
    if any(frag in path for frag in _SKIP_PATH_FRAGMENTS):
        return False
    if path.endswith(_IMAGE_SUFFIXES):
        return False
    return bool(host or parsed.scheme == "file")


def _cache_get(cache: Any, key: str) -> Any:
    return cache.get(key) if cache is not None else None


def _cache_set(cache: Any, key: str, ttl: float) -> None:
    if cache is not None:
        cache.set(key, "1", ttl)


def _rows_from_extract(
    rows,
    *,
    source_name: str,
    source_url: str,
    updated_at: Optional[str],
    trust: float = 0.28,
) -> list[dict]:
    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "program": r.program,
                "region_a": r.region_a,
                "region_b": r.region_b,
                "cabin": r.cabin,
                "miles": r.miles,
                "roundtrip": r.roundtrip,
                "source_name": source_name,
                "source_url": source_url,
                "source_updated_at": updated_at,
                "trust": trust,
            }
        )
    return out


def follow_links_in_document(
    doc: "EmailDocument",
    *,
    fetcher,
    extractor,
    cache: Any = None,
    caption_fetcher: Optional[CaptionFetcher] = None,
    limit: int = 5,
    dedupe_ttl: float = 30 * 86400.0,
) -> tuple[list[dict], int]:
    """Fetch blog posts / YouTube videos linked from one email body.

    Returns ``(rows, links_followed)``.
    """
    if caption_fetcher is None:
        caption_fetcher = default_caption_fetcher

    rows: list[dict] = []
    followed = 0
    for url in extract_links(doc.body):
        if followed >= limit:
            break
        vid = youtube_video_id(url)
        if vid:
            cache_key = f"email_link:yt:{vid}"
            if _cache_get(cache, cache_key):
                continue
            transcript = caption_fetcher(vid)
            _cache_set(cache, cache_key, dedupe_ttl)
            followed += 1
            if not transcript:
                log.info("email link yt %s: no transcript", vid)
                continue
            source_name = f"email_link:yt:{doc.source_name}"
            extracted = extractor.extract(transcript, source_hint=source_name)
            rows.extend(
                _rows_from_extract(
                    extracted,
                    source_name=source_name,
                    source_url=f"https://www.youtube.com/watch?v={vid}",
                    updated_at=doc.received_at,
                )
            )
            continue

        if not is_followable_blog_url(url):
            continue
        cache_key = f"email_link:blog:{url}"
        if _cache_get(cache, cache_key):
            continue
        page = fetcher.get(url)
        _cache_set(cache, cache_key, dedupe_ttl)
        followed += 1
        if page is None or not page.ok:
            log.info("email link blog unavailable: %s", url)
            continue
        body = readable_text(page.text)
        source_name = f"email_link:blog:{doc.source_name}"
        extracted = extractor.extract(body, source_hint=source_name)
        rows.extend(
            _rows_from_extract(
                extracted,
                source_name=source_name,
                source_url=url,
                updated_at=doc.received_at,
            )
        )
    return rows, followed
