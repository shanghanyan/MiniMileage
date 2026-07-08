"""Path inventory — every scrape/discovery channel wired in the system (§6).

The Live Scrape page walks only ``sources.yaml`` (13 chart targets). This
module surfaces the rest: discovery intakes (email, blog RSS, YouTube
transcripts) and federated providers (Amadeus, Travelpayouts, seats.aero, …)
so the UI can show the full picture without running a live scrape.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from ...config import Config, build_registry, build_repository
from .ingest import discovered_path, load_creators
from .sources import load_targets

# Env vars / notes surfaced on the inventory page.
_PROVIDER_CONFIG: dict[str, Optional[str]] = {
    "amadeus": "AMADEUS_CLIENT_ID + AMADEUS_CLIENT_SECRET",
    "seats_aero": "SEATS_AERO_API_KEY",
    "aviationstack": None,
    "travelpayouts": None,
    "curated": None,
    "aggregator": None,
}

_PROVIDER_NOTES: dict[str, str] = {
    "aggregator": "Chart targets in sources.yaml — checked via live scrape below",
    "amadeus": "L1/L2 cash fares (not points charts)",
    "aviationstack": "Stub — not implemented",
    "curated": "Static YAML ratios/charts — Phase 0 fallback",
    "seats_aero": "fetch() returns [] until paid API is wired",
    "travelpayouts": "Cached fares from travelpayouts_cache.yaml",
}

_DISCOVERY_COMMAND = {
    "email": "Live Scrape (or mileage discover)",
    "blog": "Live Scrape (or mileage discover --all)",
    "youtube": "Live Scrape (or mileage discover --all)",
}


@dataclass
class DiscoveryChannel:
    """One configured discovery intake path."""

    kind: str           # email | blog | youtube
    name: str
    url: Optional[str]
    trust: float
    ready: bool
    command: str
    detail: str


@dataclass
class ProviderPath:
    """One federated provider outside the sources.yaml walk."""

    name: str
    health: str
    trust: float
    layers: list[str]
    disabled: bool
    monthly_limit: Optional[int] = None
    config_hint: Optional[str] = None
    note: Optional[str] = None


@dataclass
class DiscoveredChartsMeta:
    updated_at: Optional[str] = None
    row_count: int = 0
    by_intake: dict[str, int] = field(default_factory=dict)
    stale_programs: list[str] = field(default_factory=list)


@dataclass
class PathInventory:
    discovery: list[DiscoveryChannel]
    providers: list[ProviderPath]
    discovered: DiscoveredChartsMeta
    summary: dict

    def to_dict(self) -> dict:
        return {
            "discovery": [asdict(d) for d in self.discovery],
            "providers": [asdict(p) for p in self.providers],
            "discovered": asdict(self.discovered),
            "summary": self.summary,
        }


def _load_discovered_meta(knowledge_dir: Path) -> DiscoveredChartsMeta:
    path = discovered_path(knowledge_dir)
    if not path.exists():
        return DiscoveredChartsMeta()
    try:
        doc = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return DiscoveredChartsMeta()
    rows = doc.get("rows") or []
    by_intake: dict[str, int] = {}
    for row in rows:
        kind = str(row.get("source_name", "")).split(":", 1)[0] or "other"
        by_intake[kind] = by_intake.get(kind, 0) + 1
    return DiscoveredChartsMeta(
        updated_at=doc.get("updated_at"),
        row_count=len(rows),
        by_intake=by_intake,
        stale_programs=sorted(doc.get("stale_programs") or []),
    )


def _email_channel(config: Config) -> DiscoveryChannel:
    has_creds = bool(config.gmail_address and config.gmail_app_password)
    if config.offline:
        ready = True
        detail = "offline mode — uses .eml fixtures under knowledge/fixtures/"
    elif has_creds:
        ready = True
        addr = config.gmail_address or ""
        masked = addr[:3] + "…" + addr[addr.index("@"):] if "@" in addr else addr
        detail = f"IMAP ready ({masked})"
    else:
        ready = False
        detail = "Set GMAIL_ADDRESS + GMAIL_APP_PASSWORD in .env"
    return DiscoveryChannel(
        kind="email",
        name="newsletter_inbox",
        url=f"imap://{config.gmail_address}" if config.gmail_address else None,
        trust=0.45,
        ready=ready,
        command=_DISCOVERY_COMMAND["email"],
        detail=detail,
    )


def _blog_channels(creators_path: Path) -> list[DiscoveryChannel]:
    channels: list[DiscoveryChannel] = []
    for creator in load_creators(creators_path):
        if not creator.has_blog:
            continue
        channels.append(
            DiscoveryChannel(
                kind="blog",
                name=creator.name,
                url=creator.blog_rss,
                trust=creator.trust,
                ready=True,
                command=_DISCOVERY_COMMAND["blog"],
                detail="RSS feed configured",
            )
        )
    return channels


def _youtube_channels(creators_path: Path) -> list[DiscoveryChannel]:
    channels: list[DiscoveryChannel] = []
    for creator in load_creators(creators_path):
        if not creator.has_youtube:
            continue
        handle = creator.youtube_handle or ""
        channels.append(
            DiscoveryChannel(
                kind="youtube",
                name=creator.name,
                url=(
                    f"https://www.youtube.com/feeds/videos.xml"
                    f"?channel_id={creator.channel_id}"
                ),
                trust=creator.trust,
                ready=True,
                command=_DISCOVERY_COMMAND["youtube"],
                detail=f"YouTube {handle}".strip() if handle else "channel_id resolved",
            )
        )
    return channels


def build_path_inventory(config: Optional[Config] = None) -> PathInventory:
    """Collect every wired scrape/discovery path and its readiness."""
    config = config or Config.from_env()
    creators_path = config.knowledge_dir / "creators.yaml"

    discovery: list[DiscoveryChannel] = [_email_channel(config)]
    discovery.extend(_blog_channels(creators_path))
    discovery.extend(_youtube_channels(creators_path))

    repo = build_repository(config)
    try:
        registry = build_registry(config, repo)
        providers: list[ProviderPath] = []
        for row in registry.provider_status():
            name = row["name"]
            providers.append(
                ProviderPath(
                    name=name,
                    health=row["health"],
                    trust=row["trust"],
                    layers=row["layers"],
                    disabled=row["disabled"],
                    monthly_limit=row.get("monthly_limit"),
                    config_hint=_PROVIDER_CONFIG.get(name),
                    note=_PROVIDER_NOTES.get(name),
                )
            )
    finally:
        repo.close()

    discovered = _load_discovered_meta(config.knowledge_dir)
    chart_targets = len(load_targets(config.sources_path))

    summary = {
        "chart_targets": chart_targets,
        "discovery_channels": len(discovery),
        "discovery_ready": sum(1 for d in discovery if d.ready),
        "providers": len(providers),
        "providers_healthy": sum(
            1 for p in providers if p.health == "healthy" and not p.disabled
        ),
        "discovered_rows": discovered.row_count,
    }
    return PathInventory(
        discovery=discovery,
        providers=providers,
        discovered=discovered,
        summary=summary,
    )
