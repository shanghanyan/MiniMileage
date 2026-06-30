"""Blog intake for the discovery mode (§6.1) — `knowledge/creators.yaml`.

For each creator with a `blog_rss`, poll the feed, diff new post URLs against the
shared `Cache` (de-dupe by URL within TTL; a `Lock`/SETNX so two runs don't
extract the same post), fetch each new post via the EXISTING `Fetcher.get()`
(unchanged politeness / Wayback / `file://` fixture support), readability-extract
the body, and run it through the SAME deterministic extractor as email — emitting
`llm_extracted` rows with provenance `source_name="blog:{name}"`.

Like the rest of the discovery intake, this is a normal sub-module of Engine A.
It degrades gracefully: `feedparser`/`trafilatura`/`readability-lxml` are optional
accelerators — without them it falls back to a stdlib feed parser and the
stdlib HTML→text used everywhere else, so the pipeline (and its tests) run with
no extra installs and never touch the network in offline mode.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Tuple
from xml.etree import ElementTree

import yaml

from ..parse import RawChartRow
from .devaluation import detect_devaluation

if TYPE_CHECKING:  # avoid a runtime import cycle with the package __init__
    from ...config import Config

log = logging.getLogger("mileage.aggregator.ingest.creators")

# De-dupe TTL: once a post URL is extracted, skip it for this long.
DEFAULT_DEDUPE_TTL = 30 * 86400.0


@dataclass
class Creator:
    name: str
    blog_rss: Optional[str] = None
    youtube_handle: Optional[str] = None
    channel_id: Optional[str] = None
    trust: float = 0.45

    @property
    def has_blog(self) -> bool:
        return bool(self.blog_rss)

    @property
    def has_youtube(self) -> bool:
        return bool(self.channel_id) and str(self.channel_id).startswith("UC")


@dataclass
class IntakeResult:
    rows: List[dict] = field(default_factory=list)
    stale_programs: set = field(default_factory=set)
    seen: int = 0          # feed entries seen
    new: int = 0           # entries not already in the cache
    used_fixtures: bool = False


def load_creators(path: Path) -> List[Creator]:
    """Load knowledge/creators.yaml into Creator records."""
    path = Path(path)
    if not path.exists():
        log.info("creators.yaml not found at %s", path)
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    creators: List[Creator] = []
    for raw in data.get("creators", []):
        yt = raw.get("youtube") or {}
        cid = yt.get("channel_id") if isinstance(yt, dict) else None
        if cid in (None, "TODO", ""):
            cid = None
        creators.append(
            Creator(
                name=str(raw.get("name") or "creator"),
                blog_rss=raw.get("blog_rss") or None,
                youtube_handle=(yt.get("handle") if isinstance(yt, dict) else None),
                channel_id=cid,
                trust=float(raw.get("trust", 0.45)),
            )
        )
    return creators


# --------------------------------------------------------------------------- #
# Feed parsing (feedparser preferred, stdlib fallback)
# --------------------------------------------------------------------------- #
def parse_feed_entries(text: str) -> List[Tuple[str, str, Optional[str]]]:
    """Return [(url, title, published)] for RSS or Atom. Empty on junk/HTML.

    A real feed body is required — an HTML 200 error page yields no entries, so
    feeds that don't actually return a feed are naturally ignored (§B/§C).
    """
    try:
        import feedparser  # type: ignore

        parsed = feedparser.parse(text)
        out: List[Tuple[str, str, Optional[str]]] = []
        for e in parsed.entries:
            link = getattr(e, "link", "") or ""
            if link:
                out.append(
                    (link, getattr(e, "title", "") or "", getattr(e, "published", None))
                )
        if out or parsed.entries:
            return out
    except Exception as exc:  # feedparser missing or failed -> stdlib fallback
        log.debug("feedparser unavailable/failed (%s); using stdlib feed parser", exc)
    return _parse_feed_stdlib(text)


def _parse_feed_stdlib(text: str) -> List[Tuple[str, str, Optional[str]]]:
    out: List[Tuple[str, str, Optional[str]]] = []
    try:
        root = ElementTree.fromstring(text)
    except Exception as exc:
        log.info("feed parse error: %s", exc)
        return out

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    # RSS 2.0: channel/item/{link,title,pubDate}
    for item in root.iter():
        if _local(item.tag) != "item":
            continue
        link = title = pub = None
        for child in item:
            lt = _local(child.tag)
            if lt == "link":
                link = (child.text or "").strip()
            elif lt == "title":
                title = (child.text or "").strip()
            elif lt in ("pubDate", "date"):
                pub = (child.text or "").strip()
        if link:
            out.append((link, title or "", pub))
    if out:
        return out

    # Atom: entry/{link[href],title,published}
    for entry in root.iter():
        if _local(entry.tag) != "entry":
            continue
        link = title = pub = None
        for child in entry:
            lt = _local(child.tag)
            if lt == "link":
                link = (child.attrib.get("href") or child.text or "").strip()
            elif lt == "title":
                title = (child.text or "").strip()
            elif lt in ("published", "updated"):
                pub = pub or (child.text or "").strip()
        if link:
            out.append((link, title or "", pub))
    return out


# --------------------------------------------------------------------------- #
# Readability (trafilatura -> readability-lxml -> stdlib html_to_text)
# --------------------------------------------------------------------------- #
def readable_text(html: str) -> str:
    """Extract the article body. Falls back to the stdlib HTML→text everywhere."""
    try:
        import trafilatura  # type: ignore

        extracted = trafilatura.extract(html)
        if extracted:
            return extracted
    except Exception:
        pass
    try:
        from readability import Document  # type: ignore

        summary_html = Document(html).summary()
        from ..extract.deterministic import html_to_text

        return html_to_text(summary_html)
    except Exception:
        pass
    from ..extract.deterministic import html_to_text

    return html_to_text(html)


# --------------------------------------------------------------------------- #
# The blog intake
# --------------------------------------------------------------------------- #
def _cache_get(cache: Any, key: str) -> Any:
    return cache.get(key) if cache is not None else None


def _cache_set(cache: Any, key: str, ttl: float) -> None:
    if cache is not None:
        cache.set(key, "1", ttl)


def poll_blog(
    creator: Creator,
    *,
    fetcher,
    extractor,
    cache: Any = None,
    lock: Any = None,
    limit: int = 10,
    dedupe_ttl: float = DEFAULT_DEDUPE_TTL,
) -> IntakeResult:
    """Poll one creator's blog feed and extract grounded rows from new posts."""
    result = IntakeResult()
    if not creator.has_blog:
        return result
    feed = fetcher.get(creator.blog_rss)
    if feed is None or not feed.ok:
        log.info("blog %s: feed unavailable (%s)", creator.name, creator.blog_rss)
        return result
    entries = parse_feed_entries(feed.text)
    result.seen = len(entries)
    for url, title, published in entries[:limit]:
        cache_key = f"blog:{creator.name}:{url}"
        if _cache_get(cache, cache_key):
            continue  # already extracted within TTL
        # Lock so two concurrent runs don't both extract the same post.
        if lock is not None:
            with lock.acquire(cache_key) as got:
                if not got:
                    continue
                if _cache_get(cache, cache_key):
                    continue
                self_processed = _process_post(
                    creator, url, title, published, fetcher=fetcher, extractor=extractor
                )
        else:
            self_processed = _process_post(
                creator, url, title, published, fetcher=fetcher, extractor=extractor
            )
        _cache_set(cache, cache_key, dedupe_ttl)
        result.new += 1
        program = detect_devaluation(title)
        if program:
            result.stale_programs.add(program)
        result.rows.extend(self_processed)
    return result


def _process_post(
    creator: Creator,
    url: str,
    title: str,
    published: Optional[str],
    *,
    fetcher,
    extractor,
) -> List[dict]:
    post = fetcher.get(url)
    if post is None or not post.ok:
        log.info("blog %s: post unavailable (%s)", creator.name, url)
        return []
    body = readable_text(post.text)
    source_name = f"blog:{creator.name}"
    rows: List[RawChartRow] = extractor.extract(body, source_hint=source_name)
    out: List[dict] = []
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
                "source_url": url,
                "source_updated_at": published,
                "trust": min(creator.trust, 0.3),  # llm_extracted -> second class
            }
        )
    return out


def run_blog_intake(
    config: "Config",
    *,
    fetcher,
    extractor=None,
    cache: Any = None,
    lock: Any = None,
    creators: Optional[List[Creator]] = None,
    limit: int = 10,
) -> IntakeResult:
    """Run the blog intake across every creator with a blog_rss."""
    if extractor is None:
        from ..extract import DeterministicExtractor

        extractor = DeterministicExtractor()
    if creators is None:
        creators = load_creators(config.knowledge_dir / "creators.yaml")

    merged = IntakeResult()
    for creator in creators:
        if not creator.has_blog:
            continue
        r = poll_blog(
            creator, fetcher=fetcher, extractor=extractor,
            cache=cache, lock=lock, limit=limit,
        )
        merged.rows.extend(r.rows)
        merged.stale_programs |= r.stale_programs
        merged.seen += r.seen
        merged.new += r.new
    return merged
