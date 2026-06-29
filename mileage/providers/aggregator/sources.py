"""Aggregator target list — load, order, and health-check sources (§6).

`knowledge/sources.yaml` is an ordered, trust-weighted list of PUBLIC,
non-WAF'd targets (anything behind a heavy WAF is the Brain's problem, §8).
This module loads them into `Target`s, resolves `file://` fixtures relative to
the knowledge dir, and runs the `--validate-urls` health check that records a
`last_404` so URL rot is caught before it silently drops data.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("mileage.aggregator.sources")

_VALID_FORMATS = {"html_table", "json", "rss", "pdf"}
_VALID_PROVIDES = {"chart", "award"}


@dataclass
class Target:
    name: str
    url: str
    format: str            # html_table | json | rss | pdf
    provides: str          # chart | award
    trust: float = 0.5
    layers: list[str] = field(default_factory=list)
    updated_at: Optional[str] = None
    # Health, mutated by validate():
    last_status: Optional[int] = None
    last_404: bool = False
    last_checked: Optional[str] = None

    def healthy(self) -> bool:
        return not self.last_404


def _resolve_url(url: str, base_dir: Path) -> str:
    """Turn a `file://relative` target into an absolute file:// URL."""
    if url.startswith("file://"):
        rest = url[len("file://"):]
        path = Path(rest)
        if not path.is_absolute():
            path = (base_dir / rest).resolve()
        return "file://" + str(path)
    return url


def load_targets(sources_path: Path) -> list[Target]:
    sources_path = Path(sources_path)
    if not sources_path.exists():
        log.info("sources.yaml not found at %s", sources_path)
        return []
    data = yaml.safe_load(sources_path.read_text(encoding="utf-8")) or {}
    base_dir = sources_path.parent
    targets: list[Target] = []
    for raw in data.get("targets", []):
        fmt = str(raw.get("format", "")).strip()
        provides = str(raw.get("provides", "")).strip()
        if fmt not in _VALID_FORMATS or provides not in _VALID_PROVIDES:
            log.warning("skipping malformed target: %s", raw.get("name"))
            continue
        targets.append(
            Target(
                name=str(raw["name"]),
                url=_resolve_url(str(raw["url"]), base_dir),
                format=fmt,
                provides=provides,
                trust=float(raw.get("trust", 0.5)),
                layers=list(raw.get("layers", [])),
                updated_at=raw.get("updated_at"),
            )
        )
    # Highest trust first: rotation/cross-check both prefer trusted sources.
    return sorted(targets, key=lambda t: t.trust, reverse=True)


def validate_targets(targets: list[Target], fetcher) -> list[Target]:
    """Probe each target; record last_status/last_404 (the `--validate-urls` job)."""
    now = datetime.now(timezone.utc).isoformat()
    for t in targets:
        ok, status = fetcher.head_ok(t.url)
        t.last_status = status
        t.last_404 = (status == 404) or (not ok and status == 0)
        t.last_checked = now
        log.info("validate %s -> status=%s ok=%s", t.name, status, ok)
    return targets
