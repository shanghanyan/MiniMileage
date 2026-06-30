"""YouTube transcript intake for the discovery mode (§6.1) — captions, no key.

For each creator with a `youtube.channel_id`, discover new videos via the channel
RSS feed `https://www.youtube.com/feeds/videos.xml?channel_id=<UC…>`, pull
captions with NO API key (`youtube-transcript-api` preferred, `yt-dlp
--write-auto-sub --skip-download` fallback), de-dupe by video id via the shared
`Cache`, and run the transcript text through the SAME deterministic extractor —
emitting `llm_extracted` rows with provenance `source_name="yt:{name}"`.

The channel-feed fetch goes through the existing `Fetcher.get()` so `file://`
fixtures exercise the exact same path offline. The caption fetch is injected
(`caption_fetcher`) so tests mock it with a fixture transcript and never touch
the network.

`channel_id: TODO` entries are resolved ONCE from the handle via
`resolve_channel_id` (reading the canonical `UC…` id off the page) — never
guessed. A handle that can't be resolved is left `TODO` and skipped.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple
from xml.etree import ElementTree

from ..parse import RawChartRow
from .creators import Creator, IntakeResult, load_creators
from .devaluation import detect_devaluation

if TYPE_CHECKING:  # avoid a runtime import cycle with the package __init__
    from ...config import Config

log = logging.getLogger("mileage.aggregator.ingest.transcripts")

YT_FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"

CaptionFetcher = Callable[[str], Optional[str]]


def channel_feed_url(channel_id: str) -> str:
    return YT_FEED_URL.format(cid=channel_id)


def parse_channel_feed(text: str) -> List[Tuple[str, str, Optional[str]]]:
    """Return [(video_id, title, published)] from a YouTube channel Atom feed."""
    out: List[Tuple[str, str, Optional[str]]] = []
    try:
        root = ElementTree.fromstring(text)
    except Exception as exc:
        log.info("yt feed parse error: %s", exc)
        return out

    def _local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    for entry in root.iter():
        if _local(entry.tag) != "entry":
            continue
        vid = title = pub = None
        for child in entry:
            lt = _local(child.tag)
            if lt == "videoId":
                vid = (child.text or "").strip()
            elif lt == "title" and title is None:
                title = (child.text or "").strip()
            elif lt in ("published", "updated") and pub is None:
                pub = (child.text or "").strip()
        if vid:
            out.append((vid, title or "", pub))
    return out


# --------------------------------------------------------------------------- #
# Caption fetching (no API key): youtube-transcript-api -> yt-dlp
# --------------------------------------------------------------------------- #
def default_caption_fetcher(video_id: str) -> Optional[str]:
    """Fetch auto/manual captions for a video id with no API key."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi  # type: ignore

        chunks = YouTubeTranscriptApi.get_transcript(video_id)
        return " ".join(c.get("text", "") for c in chunks).strip() or None
    except Exception as exc:
        log.debug("youtube-transcript-api failed for %s (%s); trying yt-dlp", video_id, exc)
    return _yt_dlp_captions(video_id)


def _yt_dlp_captions(video_id: str) -> Optional[str]:  # pragma: no cover - network
    try:
        import subprocess
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(
                [
                    "yt-dlp", "--write-auto-sub", "--skip-download",
                    "--sub-lang", "en", "--sub-format", "vtt",
                    "-o", str(Path(tmp) / "%(id)s.%(ext)s"),
                    f"https://www.youtube.com/watch?v={video_id}",
                ],
                check=False, capture_output=True, timeout=60,
            )
            for vtt in Path(tmp).glob("*.vtt"):
                return _vtt_to_text(vtt.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        log.info("yt-dlp caption fetch failed for %s: %s", video_id, exc)
    return None


def _vtt_to_text(vtt: str) -> str:
    lines: List[str] = []
    for line in vtt.splitlines():
        s = line.strip()
        if not s or s == "WEBVTT" or "-->" in s or s.isdigit():
            continue
        lines.append(re.sub(r"<[^>]+>", "", s))
    return " ".join(lines)


# --------------------------------------------------------------------------- #
# channel_id resolution from a handle (network — used once, never guessed)
# --------------------------------------------------------------------------- #
def resolve_channel_id(handle: str, *, fetcher) -> Optional[str]:
    """Read the canonical UC… id off youtube.com/<handle>. None if unresolved."""
    handle = handle.lstrip("@")
    page = fetcher.get(f"https://www.youtube.com/@{handle}")
    if page is None or not page.ok:
        return None
    for pattern in (
        r'"channelId":"(UC[0-9A-Za-z_-]{22})"',
        r'channel/(UC[0-9A-Za-z_-]{22})',
        r'"externalId":"(UC[0-9A-Za-z_-]{22})"',
    ):
        m = re.search(pattern, page.text)
        if m:
            return m.group(1)
    return None


# --------------------------------------------------------------------------- #
# The transcript intake
# --------------------------------------------------------------------------- #
def poll_channel(
    creator: Creator,
    *,
    fetcher,
    extractor,
    caption_fetcher: CaptionFetcher,
    cache: Any = None,
    limit: int = 10,
    dedupe_ttl: float = 30 * 86400.0,
) -> IntakeResult:
    """Poll one creator's channel feed and extract rows from new transcripts."""
    result = IntakeResult()
    if not creator.has_youtube:
        return result
    feed = fetcher.get(channel_feed_url(creator.channel_id))
    if feed is None or not feed.ok:
        log.info("yt %s: channel feed unavailable", creator.name)
        return result
    videos = parse_channel_feed(feed.text)
    result.seen = len(videos)
    source_name = f"yt:{creator.name}"
    for video_id, title, published in videos[:limit]:
        cache_key = f"yt:{creator.name}:{video_id}"
        if cache is not None and cache.get(cache_key):
            continue
        transcript = caption_fetcher(video_id)
        if cache is not None:
            cache.set(cache_key, "1", dedupe_ttl)
        result.new += 1
        program = detect_devaluation(title)
        if program:
            result.stale_programs.add(program)
        if not transcript:
            continue
        rows: List[RawChartRow] = extractor.extract(transcript, source_hint=source_name)
        for r in rows:
            result.rows.append(
                {
                    "program": r.program,
                    "region_a": r.region_a,
                    "region_b": r.region_b,
                    "cabin": r.cabin,
                    "miles": r.miles,
                    "roundtrip": r.roundtrip,
                    "source_name": source_name,
                    "source_url": f"https://www.youtube.com/watch?v={video_id}",
                    "source_updated_at": published,
                    "trust": min(creator.trust, 0.3),
                }
            )
    return result


def run_transcript_intake(
    config: "Config",
    *,
    fetcher,
    extractor=None,
    caption_fetcher: Optional[CaptionFetcher] = None,
    cache: Any = None,
    creators: Optional[List[Creator]] = None,
    limit: int = 10,
) -> IntakeResult:
    """Run the transcript intake across every creator with a channel_id."""
    if extractor is None:
        from ..extract import DeterministicExtractor

        extractor = DeterministicExtractor()
    if caption_fetcher is None:
        caption_fetcher = default_caption_fetcher
    if creators is None:
        creators = load_creators(config.knowledge_dir / "creators.yaml")

    merged = IntakeResult()
    for creator in creators:
        if not creator.has_youtube:
            continue
        r = poll_channel(
            creator, fetcher=fetcher, extractor=extractor,
            caption_fetcher=caption_fetcher, cache=cache, limit=limit,
        )
        merged.rows.extend(r.rows)
        merged.stale_programs |= r.stale_programs
        merged.seen += r.seen
        merged.new += r.new
    return merged
