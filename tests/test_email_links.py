"""Email link following — blog posts and YouTube videos linked from newsletters."""

from __future__ import annotations

from pathlib import Path

from mileage.config import Config
from mileage.providers.aggregator.extract import DeterministicExtractor
from mileage.providers.aggregator.fetch import Fetcher
from mileage.providers.aggregator.ingest.email_links import (
    extract_links,
    follow_links_in_document,
    youtube_video_id,
)
from mileage.providers.aggregator.ingest.email_source import EmailDocument, run_discovery

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "mileage" / "knowledge"
_FIXTURES = _KNOWLEDGE / "fixtures"


def test_extract_links_from_html() -> None:
    body = (_FIXTURES / "email_with_links.eml").read_text(encoding="utf-8")
    links = extract_links(body)
    assert any("linked_blog_post.html" in u for u in links)
    assert any(youtube_video_id(u) for u in links)


def test_youtube_video_id_parsing() -> None:
    assert youtube_video_id("https://www.youtube.com/watch?v=abc123_XYZ-") == "abc123_XYZ-"
    assert youtube_video_id("https://youtu.be/abc123_XYZ-") == "abc123_XYZ-"
    assert youtube_video_id("https://example.com/post") is None


def test_email_link_follows_blog_fixture_offline() -> None:
    blog_uri = (_FIXTURES / "linked_blog_post.html").as_uri()
    doc = EmailDocument(
        sender="hello@dailydrop.com",
        subject="link test",
        body=f'<a href="{blog_uri}">read more</a>',
    )
    fetcher = Fetcher(base_dir=_KNOWLEDGE, offline=True)
    extractor = DeterministicExtractor()

    rows, n = follow_links_in_document(
        doc, fetcher=fetcher, extractor=extractor, caption_fetcher=lambda _vid: None,
    )
    assert n == 1
    assert any(r["program"] == "turkish" and r["miles"] == 52000 for r in rows)
    assert all(r["source_name"].startswith("email_link:blog:") for r in rows)


def test_email_link_follows_youtube_with_mock_captions() -> None:
    doc = EmailDocument(
        sender="hello@dailydrop.com",
        subject="video link",
        body="https://www.youtube.com/watch?v=abc123_XYZ-",
    )
    fetcher = Fetcher(base_dir=_KNOWLEDGE, offline=True)
    extractor = DeterministicExtractor()

    def _captions(vid: str) -> str:
        assert vid == "abc123_XYZ-"
        return "Aeroplan business class to Europe is 60,000 miles one-way."

    rows, n = follow_links_in_document(
        doc,
        fetcher=fetcher,
        extractor=extractor,
        caption_fetcher=_captions,
    )
    assert n == 1
    assert any(r["program"] == "aeroplan" and r["miles"] == 60000 for r in rows)
    assert rows[0]["source_url"].startswith("https://www.youtube.com/watch")


def test_run_discovery_follows_links_when_fetcher_provided() -> None:
    config = Config(offline=True, knowledge_dir=_KNOWLEDGE)
    fetcher = Fetcher(base_dir=_KNOWLEDGE, offline=True)

    def _captions(vid: str) -> str:
        return "Turkish business class to Europe is 55,000 miles."

    result = run_discovery(
        config,
        fetcher=fetcher,
        caption_fetcher=_captions,
        fixture_dir=_FIXTURES,
        limit=5,
        link_limit=5,
    )
    assert result.email_links_followed >= 1
    link_rows = [r for r in result.rows if r["source_name"].startswith("email_link:")]
    assert link_rows, "expected rows from linked blog or YouTube in fixtures"
