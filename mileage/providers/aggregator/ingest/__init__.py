"""The aggregator's discovery intake (§6.1) — intake mode (b).

NOT a separate engine and NOT import-isolated like `brain/`: a normal
sub-module of Engine A. It pulls documents from standing feeds (the mailbox
today; creator blogs/transcripts are the same shape later), runs each through
the local extractor (`../extract`), and persists number-grounded `RawChartRow`s
to `knowledge/discovered_charts.json`. The `AggregatorProvider` then resolves
those rows for a route through the SAME `_build_charts -> verify -> graph` path
as scraped URLs — flagged `llm_extracted`, so a winner resting on one is only
ever `tentative_best` until an independent source confirms it.

`domain/` and `verify/` never import this module — same rule as every provider.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from ..parse import RawChartRow

log = logging.getLogger("mileage.aggregator.ingest")

# One discovered row as the provider consumes it.
DiscoveredRow = Tuple[RawChartRow, str, Optional[str], Optional[str], float]


def discovered_path(knowledge_dir: Path) -> Path:
    return Path(knowledge_dir) / "discovered_charts.json"


def _load_doc(path: Path) -> dict:
    if not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")) or {}
    except Exception as exc:  # corrupt file must not crash a quote
        log.warning("discovered_charts.json unreadable (%s); ignoring", exc)
        return {}


def load_discovered_rows(path: Path) -> List[DiscoveredRow]:
    """Read persisted discovery rows as (RawChartRow, source, url, updated, trust)."""
    doc = _load_doc(path)
    out: List[DiscoveredRow] = []
    for r in doc.get("rows", []):
        try:
            row = RawChartRow(
                program=str(r["program"]).strip().lower(),
                region_a=str(r["region_a"]).strip().lower(),
                region_b=str(r["region_b"]).strip().lower(),
                cabin=str(r["cabin"]).strip().lower(),
                miles=int(r["miles"]),
                roundtrip=bool(r.get("roundtrip", False)),
                updated_at=r.get("source_updated_at"),
            )
        except (KeyError, ValueError, TypeError):
            continue
        out.append(
            (
                row,
                str(r.get("source_name") or "discovery"),
                r.get("source_url"),
                r.get("source_updated_at"),
                float(r.get("trust") or 0.3),
            )
        )
    return out


def load_stale_programs(path: Path) -> set[str]:
    """Programs a devaluation email marked stale (proactive freshness, §6.2)."""
    doc = _load_doc(path)
    return {str(p).strip().lower() for p in doc.get("stale_programs", []) if p}


def write_discovered(
    path: Path,
    rows: List[dict],
    stale_programs: set[str],
) -> None:
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stale_programs": sorted(stale_programs),
        "rows": rows,
    }
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


from .devaluation import (  # noqa: E402
    detect_devaluation,
    mark_devaluations_stale,
)
from .email_source import (  # noqa: E402  (re-export after helpers are defined)
    EmailDocument,
    fetch_email_documents,
    run_discovery,
)
from .creators import (  # noqa: E402
    Creator,
    IntakeResult,
    load_creators,
    poll_blog,
    run_blog_intake,
)
from .transcripts import (  # noqa: E402
    poll_channel,
    resolve_channel_id,
    run_transcript_intake,
)
from .orchestrate import DiscoverResult, run_all_intakes  # noqa: E402

__all__ = [
    "Creator",
    "DiscoveredRow",
    "DiscoverResult",
    "EmailDocument",
    "IntakeResult",
    "detect_devaluation",
    "discovered_path",
    "fetch_email_documents",
    "load_creators",
    "load_discovered_rows",
    "load_stale_programs",
    "mark_devaluations_stale",
    "poll_blog",
    "poll_channel",
    "resolve_channel_id",
    "run_all_intakes",
    "run_blog_intake",
    "run_discovery",
    "run_transcript_intake",
    "write_discovered",
]
