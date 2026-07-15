#!/usr/bin/env python3
"""Dump real intake documents as fixture-candidate JSON for hand-labeling (§B,
Cursor-LLM-Extractor-Task.md). Pulls documents through the SAME code paths
production/discovery uses — `fetch_email_documents()` for the mailbox,
`Fetcher.get()` + `readable_text()` for blogs — so what you label is exactly
what `LLMExtractor.extract()` sees, not an approximation of it.

Writes one skeleton JSON file per document to
`tests/fixtures/extraction_real/`, matching the schema in
`tests/fixtures/extraction/schema.json`, with `document`/`source_hint` filled
in and `expected_rows: []` left for you to fill in by hand.

Usage:
    python scripts/dump_fixture_candidates.py --emails 15
    python scripts/dump_fixture_candidates.py --blogs frequent_miler ten_x_travel --per-blog 3
    python scripts/dump_fixture_candidates.py --emails 10 --blogs thrifty_traveler

Needs real credentials/network — run on your own machine (GMAIL_ADDRESS /
GMAIL_APP_PASSWORD in .env for --emails; both flags need live HTTP), not
inside the Cowork sandbox.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "tests" / "fixtures" / "extraction_real"


def _slug(text: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return s[:maxlen] or "untitled"


def _write_skeleton(path: Path, *, source_hint: str, note: str, document: str) -> None:
    path.write_text(
        json.dumps(
            {
                "source_hint": source_hint,
                "synthetic": False,
                "difficulty": "clean",  # set to "messy" if it's a multi-item/ambiguous post
                "notes": note + " — FILL IN expected_rows before scoring",
                "document": document,
                "expected_rows": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def dump_emails(limit: int) -> list[Path]:
    from mileage.config import Config
    from mileage.providers.aggregator.ingest.email_source import fetch_email_documents

    config = Config.from_env()
    docs, used_fixtures = fetch_email_documents(config, limit=limit)
    if used_fixtures:
        print(
            "WARNING: no GMAIL_ADDRESS/GMAIL_APP_PASSWORD (or MILEAGE_OFFLINE=1 set) "
            "— these are the .eml fixtures under knowledge/fixtures/, not your real "
            "inbox. Unset MILEAGE_OFFLINE and confirm .env has real Gmail creds."
        )
    written: list[Path] = []
    for i, doc in enumerate(docs, 1):
        # doc.body is exactly what run_discovery() hands to extractor.extract() —
        # dumped verbatim (raw HTML or plain text) so the fixture matches
        # production input exactly. Read it in Gmail/a browser to label
        # comfortably; the numbers are still present as plain digits in the HTML.
        path = OUT_DIR / f"real_email_{i:02d}_{_slug(doc.subject)}.json"
        _write_skeleton(
            path,
            source_hint=doc.source_name,
            note=f"subject: {doc.subject!r}",
            document=doc.body,
        )
        written.append(path)
    return written


def dump_blogs(creator_names: list[str], per_blog: int) -> list[Path]:
    from mileage.config import Config
    from mileage.providers.aggregator.fetch import Fetcher
    from mileage.providers.aggregator.politeness import PolitenessPolicy
    from mileage.providers.aggregator.ingest.creators import (
        load_creators,
        parse_feed_entries,
        readable_text,
    )

    config = Config.from_env()
    creators = {
        c.name: c for c in load_creators(config.knowledge_dir / "creators.yaml")
    }
    fetcher = Fetcher(
        politeness=PolitenessPolicy(), base_dir=config.knowledge_dir, offline=False
    )

    written: list[Path] = []
    for name in creator_names:
        creator = creators.get(name)
        if creator is None or not creator.blog_rss:
            print(f"skip {name}: not in creators.yaml or has no blog_rss")
            continue
        feed = fetcher.get(creator.blog_rss)
        if feed is None or not feed.ok:
            print(f"skip {name}: feed fetch failed ({creator.blog_rss})")
            continue
        entries = parse_feed_entries(feed.text)[:per_blog]
        if not entries:
            print(f"skip {name}: feed parsed but had 0 entries — check the URL by hand")
            continue
        for i, (url, title, _pub) in enumerate(entries, 1):
            page = fetcher.get(url)
            if page is None or not page.ok:
                print(f"  skip post {url}: fetch failed")
                continue
            body_text = readable_text(page.text)  # same call run_blog_intake() makes
            path = OUT_DIR / f"real_blog_{name}_{i:02d}_{_slug(title)}.json"
            _write_skeleton(
                path,
                source_hint=f"blog:{name}",
                note=f"post: {title!r} ({url})",
                document=body_text,
            )
            written.append(path)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--emails", type=int, default=0, help="how many unread emails to pull"
    )
    parser.add_argument(
        "--blogs", nargs="*", default=[], help="creator names from creators.yaml"
    )
    parser.add_argument("--per-blog", type=int, default=3, help="posts per blog")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if args.emails:
        written += dump_emails(args.emails)
    if args.blogs:
        written += dump_blogs(args.blogs, args.per_blog)

    if not written:
        print("Nothing written — pass --emails N and/or --blogs NAME [NAME ...]")
        return 1

    print(f"\nWrote {len(written)} fixture-candidate skeleton(s) to {OUT_DIR}/")
    for p in written:
        print(f"  {p.name}")
    print(
        "\nNext: open each file, read 'document' (or the original email/post "
        "side-by-side if the HTML is hard to read), fill in 'expected_rows' by "
        "hand, set 'difficulty' to clean/messy, and DELETE any file with no real "
        "award-chart content (booking confirmations, pure devaluation chatter, "
        "unrelated posts) rather than leaving it as an empty-rows fixture — unless "
        "you're deliberately keeping it as a negative-control precision test."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
