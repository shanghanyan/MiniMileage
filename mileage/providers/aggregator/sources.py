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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

log = logging.getLogger("mileage.aggregator.sources")

_VALID_FORMATS = {"html_table", "html_table_wide", "json", "rss", "pdf"}
_VALID_PROVIDES = {"chart", "award"}


@dataclass
class Target:
    name: str
    url: str
    format: str            # html_table | html_table_wide | json | rss | pdf
    provides: str          # chart | award
    trust: float = 0.5
    layers: list[str] = field(default_factory=list)
    updated_at: Optional[str] = None
    program: Optional[str] = None  # loyalty program (required for html_table_wide + pdf)
    # Health, mutated by validate():
    last_status: Optional[int] = None
    last_404: bool = False
    last_checked: Optional[str] = None
    # Rot-detection (§F/§G). `selector_ok` is the deep content check: True = the
    # structural parser produced >=1 canonicalizable row; False = 200-but-empty
    # (selector_miss); None = not deep-checked.
    selector_ok: Optional[bool] = None
    consecutive_failures: int = 0
    selector_misses: int = 0

    def healthy(self) -> bool:
        # A hard 404 disables a source; a transient status 0 / selector_miss does
        # NOT (it may recover) — but is surfaced distinctly by status_label().
        return not self.last_404

    def status_label(self) -> str:
        """Distinguish ok / unreachable / rotted / selector_miss (§G)."""
        if self.last_checked is None:
            return "unchecked"
        if self.last_404:
            return "rotted"
        if self.last_status == 0:
            return "unreachable"
        if self.selector_ok is False:
            return "selector_miss"
        return "ok"

    def is_rotted(self, *, max_failures: int = 3, max_selector_misses: int = 2) -> bool:
        """True when this source should trigger URL rediscovery (§F)."""
        return (
            self.last_404
            or self.consecutive_failures >= max_failures
            or self.selector_misses >= max_selector_misses
        )


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
                program=str(raw["program"]).strip().lower() if raw.get("program") else None,
            )
        )
    # Highest trust first: rotation/cross-check both prefer trusted sources.
    return sorted(targets, key=lambda t: t.trust, reverse=True)


def apply_persisted_health(
    targets: list[Target], health_repo: Any
) -> list[Target]:
    """Merge last-known health from SQLite into in-memory targets."""
    for t in targets:
        row = health_repo.get_source_health(t.name)
        if row:
            t.last_status = row.get("last_status")
            t.last_404 = bool(row.get("last_404"))
            t.last_checked = row.get("checked_at")
            t.consecutive_failures = int(row.get("consecutive_failures") or 0)
            t.selector_misses = int(row.get("selector_misses") or 0)
            t.selector_ok = row.get("selector_ok")
    return targets


def _needs_check(checked_at: Optional[str], max_age_days: int) -> bool:
    if not checked_at:
        return True
    try:
        dt = datetime.fromisoformat(checked_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - dt > timedelta(days=max_age_days)


def validate_targets(
    targets: list[Target],
    fetcher,
    *,
    health_repo: Any = None,
    force: bool = False,
    max_age_days: int = 30,
    deep: bool = False,
    content_check: Optional[Callable[[Target, str], int]] = None,
) -> list[Target]:
    """Probe each target; persist health (monthly URL-rot check, Phase 2/§G).

    `deep=True` adds CONTENT validation: after a 200, fetch the body and run the
    target's structural parser via `content_check(target, text) -> row_count`.
    Zero rows on a live page is a `selector_miss` (the page loaded but stopped
    serving the expected table) — surfaced distinctly and fed into §F rot
    detection, instead of the old behavior that reported it as `ok`.
    """
    now = datetime.now(timezone.utc).isoformat()
    for t in targets:
        if health_repo and not force and not _needs_check(t.last_checked, max_age_days):
            log.info("skip validate %s: checked %s (< %d days)", t.name, t.last_checked, max_age_days)
            continue
        ok, status = fetcher.head_ok(t.url)
        t.last_status = status
        # Only a real 404 (or 410 Gone) is permanent URL rot. A connection
        # error / unknown (status 0 — offline, blocked egress, transient DNS)
        # must NOT disable the source forever; it stays usable and is re-probed.
        t.last_404 = status in (404, 410)
        t.last_checked = now

        # Failure for the consecutive-failure counter = couldn't reach a healthy
        # page (404/410/unreachable). A 200 resets it.
        reachable = ok and status != 0 and not t.last_404
        if reachable:
            t.consecutive_failures = 0
        else:
            t.consecutive_failures += 1

        # Deep content check (only meaningful on a reachable page).
        t.selector_ok = None
        if deep and reachable and content_check is not None:
            result = fetcher.get(t.url)
            if result is not None and result.ok:
                try:
                    rows = content_check(t, result.text)
                except Exception as exc:  # a parser bug must not crash validation
                    log.info("content_check error for %s: %s", t.name, exc)
                    rows = 0
                t.selector_ok = rows > 0
                if rows > 0:
                    t.selector_misses = 0
                else:
                    t.selector_misses += 1
            else:
                # Couldn't fetch the body to validate; leave selector_ok unknown.
                t.selector_ok = None

        log.info(
            "validate %s -> status=%s label=%s fails=%d sel_miss=%d",
            t.name, status, t.status_label(), t.consecutive_failures,
            t.selector_misses,
        )
        if health_repo is not None:
            health_repo.put_source_health(
                t.name,
                t.url,
                last_status=status,
                last_404=t.last_404,
                checked_at=now,
                consecutive_failures=t.consecutive_failures,
                selector_misses=t.selector_misses,
                selector_ok=t.selector_ok,
            )
    return targets
