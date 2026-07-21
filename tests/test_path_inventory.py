"""Tests for path_inventory.build_path_inventory (§6)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("MILEAGE_OFFLINE", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.config import Config
from mileage.providers.aggregator.path_inventory import build_path_inventory

_KNOWLEDGE = Path(__file__).resolve().parent.parent / "mileage" / "knowledge"


def test_inventory_lists_discovery_and_providers() -> None:
    config = Config(knowledge_dir=_KNOWLEDGE, offline=True)
    inv = build_path_inventory(config)

    kinds = {d.kind for d in inv.discovery}
    assert "email" in kinds
    assert "blog" in kinds
    assert "youtube" in kinds

    blogs = [d for d in inv.discovery if d.kind == "blog"]
    youtube = [d for d in inv.discovery if d.kind == "youtube"]
    assert len(blogs) == 8   # +view_from_the_wing, 2026-07-20 (blog_rss only, no channel_id)
    assert len(youtube) == 6

    names = {p.name for p in inv.providers}
    assert "amadeus" in names
    assert "travelpayouts" in names
    assert "seats_aero" in names
    assert "aviationstack" in names
    assert "aggregator" in names

    # Read from the loader rather than hardcoding a count that drifts every
    # time a source is added (15 as of 2026-07-08: +10x-eva-chart,
    # +10x-krisflyer-chart — see knowledge/sources.yaml).
    from mileage.providers.aggregator.sources import load_targets

    assert inv.summary["chart_targets"] == len(load_targets(config.sources_path))
    assert inv.summary["discovery_channels"] == 1 + 8 + 6


def test_inventory_email_ready_offline() -> None:
    config = Config(knowledge_dir=_KNOWLEDGE, offline=True)
    email = next(d for d in build_path_inventory(config).discovery if d.kind == "email")
    assert email.ready is True
    assert "fixtures" in email.detail.lower()


def test_inventory_amadeus_down_without_creds(monkeypatch) -> None:
    monkeypatch.delenv("AMADEUS_CLIENT_ID", raising=False)
    monkeypatch.delenv("AMADEUS_CLIENT_SECRET", raising=False)
    config = Config(knowledge_dir=_KNOWLEDGE, offline=True)
    amadeus = next(
        p for p in build_path_inventory(config).providers if p.name == "amadeus"
    )
    assert amadeus.health == "down"
    assert amadeus.config_hint is not None


def test_inventory_aviationstack_stub_note() -> None:
    config = Config(knowledge_dir=_KNOWLEDGE, offline=True)
    av = next(
        p for p in build_path_inventory(config).providers if p.name == "aviationstack"
    )
    assert av.health == "down"
    assert av.note and "stub" in av.note.lower()
