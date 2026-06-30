"""Discovery orchestration (§6.1/§E) — run all four intakes as one sweep.

`mileage discover` runs email + blogs + transcripts + devaluation off the
existing `store/jobs.py` queue, emits a `discover` CHAIN span with a child per
intake (Arize, additive/no-op without creds), marks devaluation-flagged programs
`stale` in the store, and returns the merged rows for the caller to persist to
`discovered_charts.json`.

Each emitted row carries its `email:`/`blog:`/`yt:` provenance and the
`llm_extracted` flag, and can only ever produce `tentative_best` until an
INDEPENDENT source confirms it — `verify/crosscheck.py` keys independence on
`source_name`, so a blog and a transcript echoing the same post are not
independent.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional

from .... import obs
from .creators import Creator, load_creators, run_blog_intake
from .devaluation import mark_devaluations_stale
from .email_source import run_discovery as run_email_discovery
from .transcripts import run_transcript_intake

if TYPE_CHECKING:
    from ...config import Config

log = logging.getLogger("mileage.aggregator.ingest.orchestrate")


@dataclass
class DiscoverResult:
    rows: List[dict] = field(default_factory=list)
    stale_programs: set = field(default_factory=set)
    email_docs: int = 0
    blog_new: int = 0
    transcript_new: int = 0
    used_fixtures: bool = False
    marked_stale: set = field(default_factory=set)

    @property
    def by_intake(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self.rows:
            kind = str(r.get("source_name", "")).split(":", 1)[0] or "other"
            counts[kind] = counts.get(kind, 0) + 1
        return counts


def run_all_intakes(
    config: "Config",
    *,
    repo: Any = None,
    fetcher=None,
    cache: Any = None,
    lock: Any = None,
    jobs: Any = None,
    caption_fetcher=None,
    fixture_dir: Optional[Path] = None,
    creators: Optional[List[Creator]] = None,
    limit: int = 10,
) -> DiscoverResult:
    """Run email + blog + transcript intakes; mark devaluations stale.

    Blog/transcript sweeps are submitted to `jobs` (off the request path) when a
    queue is provided; otherwise they run inline. Email uses IMAP directly (or
    .eml fixtures offline).
    """
    if fetcher is None:
        from ..fetch import Fetcher
        from ..politeness import PolitenessPolicy

        fetcher = Fetcher(
            politeness=PolitenessPolicy(),
            base_dir=config.knowledge_dir,
            offline=config.offline,
        )
    if creators is None:
        creators = load_creators(config.knowledge_dir / "creators.yaml")

    result = DiscoverResult()
    lock_guard = threading.Lock()

    with obs.span("discover", obs.KIND_CHAIN, input_value="email+blogs+transcripts") as chain:
        # 1) Email (IMAP or .eml fixtures).
        with obs.span("discover:email", obs.KIND_CHAIN) as s:
            email = run_email_discovery(config, fixture_dir=fixture_dir, limit=limit)
            result.rows.extend(email.rows)
            result.stale_programs |= set(email.stale_programs)
            result.email_docs = len(email.documents)
            result.used_fixtures = email.used_fixtures
            obs.set_output(s, f"{len(email.rows)} rows from {result.email_docs} emails")

        # 2) Blogs + 3) transcripts — concurrent off the jobs queue when present.
        def _blogs() -> None:
            with obs.span("discover:blogs", obs.KIND_CHAIN) as s:
                r = run_blog_intake(
                    config, fetcher=fetcher, cache=cache, lock=lock,
                    creators=creators, limit=limit,
                )
                with lock_guard:
                    result.rows.extend(r.rows)
                    result.stale_programs |= r.stale_programs
                    result.blog_new += r.new
                obs.set_output(s, f"{len(r.rows)} rows from {r.new} new posts")

        def _transcripts() -> None:
            with obs.span("discover:transcripts", obs.KIND_CHAIN) as s:
                r = run_transcript_intake(
                    config, fetcher=fetcher, cache=cache,
                    caption_fetcher=caption_fetcher, creators=creators, limit=limit,
                )
                with lock_guard:
                    result.rows.extend(r.rows)
                    result.stale_programs |= r.stale_programs
                    result.transcript_new += r.new
                obs.set_output(s, f"{len(r.rows)} rows from {r.new} new videos")

        if jobs is not None:
            jobs.submit(_blogs, name="discover:blogs")
            jobs.submit(_transcripts, name="discover:transcripts")
            jobs.join(timeout=120.0)
        else:
            _blogs()
            _transcripts()

        # 4) Devaluation fast-path: persist stale programs to the store (§D).
        result.marked_stale = mark_devaluations_stale(
            repo, result.stale_programs, reason="discovery"
        )
        obs.set_output(
            chain,
            f"{len(result.rows)} rows; stale={sorted(result.stale_programs)}",
        )
    return result
