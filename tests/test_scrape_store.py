"""Daily scrape snapshot persistence."""

from __future__ import annotations

from pathlib import Path

from mileage.providers.aggregator.scrape_store import (
    load_daily_snapshot,
    save_daily_snapshot,
)
from mileage.store.inproc import InProcCache


def test_daily_snapshot_file_roundtrip(tmp_path: Path) -> None:
    payload = {
        "completed_at": "2026-07-08T12:00:00+00:00",
        "discovery": {"row_count": 3},
        "scrape": {"summary": {"all_primaries_ok": True}},
    }
    cache = InProcCache()
    storage = save_daily_snapshot(
        payload,
        knowledge_dir=tmp_path,
        cache=cache,
        backend="inproc",
    )
    assert storage == "file"
    path = tmp_path / "daily_scrape.json"
    assert path.exists()
    loaded = load_daily_snapshot(
        knowledge_dir=tmp_path, cache=cache, backend="inproc"
    )
    assert loaded is not None
    assert loaded["discovery"]["row_count"] == 3


def test_daily_snapshot_redis_backend_uses_cache() -> None:
    cache = InProcCache()
    payload = {"completed_at": "2026-07-08T12:00:00+00:00", "discovery": {}}
    storage = save_daily_snapshot(
        payload,
        knowledge_dir=Path("."),
        cache=cache,
        backend="redis",
    )
    assert storage == "redis"
    loaded = load_daily_snapshot(
        knowledge_dir=Path("."),
        cache=cache,
        backend="redis",
    )
    assert loaded is not None
    assert loaded["completed_at"].startswith("2026-07-08")
