"""Persist scheduled scrape snapshots — Redis Cloud when configured, file fallback (§6).

The daily scrape CLI writes a combined discovery + chart-scrape report here so
cron can run once per day without keeping the API server up. The debug UI reads
``GET /scrape/daily`` to show the last persisted run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("mileage.aggregator.scrape_store")

LATEST_KEY = "scrape:daily:latest"
HISTORY_TTL = 8 * 86400.0  # keep ~1 week of dated snapshots
FILE_NAME = "daily_scrape.json"


def _history_key(iso_date: str) -> str:
    return f"scrape:daily:history:{iso_date}"


def save_daily_snapshot(
    payload: dict,
    *,
    knowledge_dir: Path,
    cache: Any = None,
    backend: str = "inproc",
) -> str:
    """Persist a daily scrape payload. Returns ``redis`` or ``file``."""
    payload = {
        **payload,
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }
    if backend == "redis" and cache is not None:
        cache.set(LATEST_KEY, payload)
        completed = str(payload.get("completed_at") or "")[:10]
        if completed:
            cache.set(_history_key(completed), payload, ttl_seconds=HISTORY_TTL)
        log.info("daily scrape saved to Redis (%s)", LATEST_KEY)
        return "redis"

    path = Path(knowledge_dir) / FILE_NAME
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log.info("daily scrape saved to %s (set MILEAGE_REDIS_URL for Redis Cloud)", path)
    return "file"


def load_daily_snapshot(
    *,
    knowledge_dir: Path,
    cache: Any = None,
    backend: str = "inproc",
) -> Optional[dict]:
    """Load the most recent daily scrape snapshot, or None if never run."""
    if backend == "redis" and cache is not None:
        doc = cache.get(LATEST_KEY)
        if doc is not None:
            return doc

    path = Path(knowledge_dir) / FILE_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("daily_scrape.json unreadable (%s)", exc)
        return None
