"""Live-scrape report — fetch → parse → resolve per target, role-aware (§6).

Single source of truth for the "did every program's PRIMARY source work?"
scoreboard, shared by:

  - ``tests/test_live_scrape.py``  (CLI: ``MILEAGE_OFFLINE=0 python tests/test_live_scrape.py``)
  - ``GET /scrape/live``           (the UI's Live Scrape page)

Parsing dispatches through :class:`AggregatorProvider._parse_chart_rows` /
``._parse_award_rows`` so the harness and the endpoint parse EXACTLY like
production. A past class of bug was the harness carrying its OWN parser dispatch
that silently drifted from the provider (it never learned about
``html_table_destination``, so Turkish/KrisFlyer looked broken when they were
fine). Routing every caller through the provider makes that drift impossible.

Role semantics (the point of this module):

  - a **primary** target failing means the program has no working source — a
    hard ``fail`` that flips ``all_primaries_ok`` false.
  - a **fallback** target failing is only a ``warn``: a working primary already
    covers that program, so a redundant mirror/PDF/blog going down is expected
    and must not read as an outage. This is what stops a fallback breaking from
    looking identical to a primary breaking.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from ...domain.charts import lookup_award_miles
from ...domain.models import Cabin, Route
from .fetch import Fetcher
from .provider import AggregatorProvider
from .sources import Target

# Two fixed probe routes for stage 3 (resolve): a chart that PARSES but resolves
# nothing is caught here, not reported as a false "ok". They mirror the demos.
DEFAULT_PROBES: list[Route] = [
    Route("LAX", "IST", Cabin.BUSINESS),  # Demo B: Aeroplan / LifeMiles / Turkish
    Route("LAX", "JFK", Cabin.ECONOMY),   # Demo A: Aeroplan
    Route("SIN", "JFK", Cabin.BUSINESS),  # KrisFlyer hub-based guide (SIN origin)
]

# Format-specific probes — some fallback parsers cover zone pairs the default
# demo routes never touch (10x ANA seasonal tables are Japan↔Korea, not LAX↔IST).
_FORMAT_PROBES: dict[str, list[Route]] = {
    "html_table_seasonal_zones": [
        Route("NRT", "ICN", Cabin.BUSINESS),
        Route("NRT", "ICN", Cabin.ECONOMY),
    ],
}

# Programs held to the strict bar: a chart that parses but resolves NEITHER probe
# is a failure for these. Others only warn — their hubs legitimately may not
# cover these exact city pairs.
STRICT_PROGRAMS: frozenset[str] = frozenset({"aeroplan", "lifemiles"})

_CABIN_ABBR = {
    "economy": "eco",
    "premium_economy": "pe",
    "business": "biz",
    "first": "first",
}

_MIN_BODY_BYTES = 200
_SAMPLE_ROWS = 5


@dataclass
class TargetResult:
    """One target's fetch→parse→resolve outcome, JSON-serializable."""

    name: str
    url: str
    role: str
    format: str
    provides: str
    trust: float
    status: str                 # ok | warn | fail (AFTER role reclassification)
    detail: str
    rows: int = 0
    program: Optional[str] = None
    resolved: Optional[str] = None      # e.g. "LAX->IST biz 90000mi (aeroplan)"
    reclassified: bool = False          # fallback fail downgraded to warn?
    sample: list[dict] = field(default_factory=list)
    # Bypass layer 1 — populated when fetch fails or returns a challenge page.
    block_type: Optional[str] = None
    block_signals: list[str] = field(default_factory=list)


@dataclass
class ProgramHealth:
    """Per-program roll-up: does this program have a WORKING primary?"""

    program: str
    has_working_primary: bool
    primaries: list[dict] = field(default_factory=list)   # [{name,status,detail}]
    fallbacks: list[dict] = field(default_factory=list)


@dataclass
class ScrapeReport:
    offline: bool
    targets: list[TargetResult]
    programs: list[ProgramHealth]
    summary: dict

    def to_dict(self) -> dict:
        return {
            "offline": self.offline,
            "targets": [asdict(t) for t in self.targets],
            "programs": [asdict(p) for p in self.programs],
            "summary": self.summary,
        }


# --------------------------------------------------------------------------- #
# Zero-row diagnosis — attribute a miss to the RIGHT cause (fetch vs parser).
# --------------------------------------------------------------------------- #
def _looks_undecoded(text: str) -> bool:
    """A body that is mostly non-printable is an *undecoded* response (a
    Brotli/zstd payload httpx couldn't inflate), NOT an HTML shell — the failure
    mode that masquerades as a parser miss. Surface it as such."""
    if not text:
        return False
    sample = text[:2000]
    printable = sum(1 for c in sample if c.isprintable() or c in "\r\n\t ")
    return (printable / len(sample)) < 0.85


def diagnose_zero_rows(target: Target, result: Any) -> str:
    """Attribute a 0-row parse to a specific cause instead of blaming the parser.

    Reports via/bytes and, for HTML, the actual ``<table>`` count so an
    undecoded/shell fetch is never mistaken for a schema mismatch.
    """
    fmt = target.format
    via = getattr(result, "via", "?")
    n = len(result.text)
    prefix = f"0 rows (fmt={fmt}, via={via}, bytes={n}"

    if fmt in (
        "html_table", "html_table_wide", "html_table_destination",
        "html_table_zone_matrix", "html_table_seasonal_zones",
    ):
        tables = result.text.lower().count("<table")
        if _looks_undecoded(result.text):
            return (
                f"{prefix}, tables=?) — body is UNDECODED (mostly non-printable): "
                "server sent a content-encoding we can't inflate (install the "
                "[aggregator] extra for brotli/zstd). NOT a parser bug."
            )
        if tables == 0:
            return (
                f"{prefix}, tables=0) — no <table> in body: JS-rendered shell, "
                "wrong URL, or non-HTML content. A fetch/source problem, not the parser."
            )
        if fmt == "html_table_destination":
            return (
                f"{prefix}, tables={tables}) — body has {tables} table(s) but the "
                "destination parser matched no 'destination | code | cabin<tier>' "
                "guide table (header/tier-label drift on the live page, or all rows "
                "dropped on an unmappable IATA code). Check the hub + table headers."
            )
        if fmt == "html_table_zone_matrix":
            return (
                f"{prefix}, tables={tables}) — body has {tables} table(s) but none "
                "resolved to a cabin-attributable zone x zone matrix (no clean "
                "'<Cabin> Class' heading directly before a numeric zone matrix — "
                "heading wording drift, or the matrix shape itself changed)."
            )
        if fmt == "html_table_seasonal_zones":
            return (
                f"{prefix}, tables={tables}) — body has {tables} table(s) but either "
                "no 'Zone name | Zone number' legend was found, or no "
                "'season | <cabin> class' table resolved against a 'Routes between "
                "X (Zone N) and Y (Zone M)' caption (caption wording drift, or an "
                "unmapped zone name in the legend)."
            )
        return (
            f"{prefix}, tables={tables}) — body has {tables} table(s) but none "
            "match the from/to/distance + cabin wide schema (schema mismatch — "
            "e.g. a guide/prose page). Parser working as designed on this shape."
        )

    if fmt == "pdf":
        return (
            f"{prefix}) — pdfplumber extracted no parseable chart table "
            "(tried wide from/to/distance grid and zone×zone matrix; "
            "or pdfplumber isn't installed). Merged-cell / multi-row PDF "
            "layouts may need a tighter selector."
        )

    return f"{prefix}) — parser produced no rows for this format."


# --------------------------------------------------------------------------- #
# Per-target check (RAW status, before role reclassification).
# --------------------------------------------------------------------------- #
def _probes_for_target(target: Target, base: list[Route]) -> list[Route]:
    """Merge default probes with any format-specific routes for this target."""
    extra = _FORMAT_PROBES.get(target.format, [])
    if not extra:
        return base
    seen: set[tuple[str, str, str]] = set()
    merged: list[Route] = []
    for route in [*extra, *base]:
        key = (route.origin, route.dest, route.cabin.value)
        if key in seen:
            continue
        seen.add(key)
        merged.append(route)
    return merged


def _resolve_detail(agg: AggregatorProvider, rows: list, probes: list[Route]) -> Optional[str]:
    """First probe route that resolves against the parsed rows, or None."""
    chart_by_program = agg._build_charts(rows)
    for route in probes:
        for program, chart in chart_by_program.items():
            hit = lookup_award_miles(
                program, chart, route, agg._region_map,
                airport_coords=agg._airport_coords,
                program_zones=agg._program_zones,
            )
            if hit is not None:
                cab = _CABIN_ABBR.get(route.cabin.value, route.cabin.value)
                return f"{route.origin}->{route.dest} {cab} {hit.miles}mi ({program})"
    return None


def check_target(
    target: Target,
    agg: AggregatorProvider,
    *,
    probes: list[Route] = DEFAULT_PROBES,
    strict_programs: frozenset[str] = STRICT_PROGRAMS,
) -> TargetResult:
    """Run fetch → parse → resolve for one target and return its RAW result.

    Status here is pre-role: role reclassification (fallback fail → warn) is
    applied by :func:`run_live_scrape`, so this stays a pure per-source check.
    """
    base = TargetResult(
        name=target.name,
        url=target.url,
        role=target.role,
        format=target.format,
        provides=target.provides,
        trust=target.trust,
        program=target.program,
        status="fail",
        detail="",
    )

    # ---- Stage 1: fetch --------------------------------------------------- #
    result = agg.fetcher.get(target.url)
    if result is None:
        offline_http = agg.fetcher.offline and target.url.startswith(("http://", "https://"))
        base.detail = (
            "offline mode: live HTTP disabled (run with offline=false / "
            "MILEAGE_OFFLINE=0 to fetch this URL)"
            if offline_http
            else "fetch returned None (fallback chain exhausted — check logs)"
        )
        base.block_type = "network" if offline_http else "unknown"
        return base
    if getattr(result, "block_type", None) and result.block_type != "none":
        base.block_type = result.block_type
        base.block_signals = list(getattr(result, "block_signals", []) or [])
    if not result.ok:
        bt = getattr(result, "block_type", None) or "unknown"
        base.block_type = bt
        base.block_signals = list(getattr(result, "block_signals", []) or [])
        base.detail = (
            f"fetch not ok: status={result.status} via={result.via} "
            f"block_type={bt}"
        )
        return base
    if len(result.text) < _MIN_BODY_BYTES:
        bt = getattr(result, "block_type", None) or "short_shell"
        base.block_type = bt
        base.block_signals = list(getattr(result, "block_signals", []) or [])
        base.detail = (
            f"fetch returned suspiciously short body ({len(result.text)} bytes) "
            f"— block_type={bt} (possible JS shell or challenge page)"
        )
        return base

    # Challenge page that still returned a long body (e.g. Cloudflare interstitial).
    if getattr(result, "block_type", None) not in (None, "none"):
        base.block_type = result.block_type
        base.block_signals = list(getattr(result, "block_signals", []) or [])
        # Continue to parse — may still yield 0 rows; diagnosis will carry block_type.

    # ---- Stage 2: parse (production dispatch, no drift) ------------------- #
    if target.provides == "award":
        rows = agg._parse_award_rows(target, result.text)
    else:
        rows = agg._parse_chart_rows(target, result.text, raw=result.raw)
    base.rows = len(rows)
    base.sample = [asdict(r) for r in rows[:_SAMPLE_ROWS]]

    if not rows:
        base.detail = diagnose_zero_rows(target, result)
        if getattr(result, "block_type", None) not in (None, "none"):
            base.block_type = result.block_type
            base.block_signals = list(getattr(result, "block_signals", []) or [])
            base.detail = f"{base.detail} [block_type={result.block_type}]"
        # Live award space fluctuates, so 0 rows there is a weak signal: warn.
        base.status = "warn" if target.provides == "award" else "fail"
        return base

    # ---- Stage 3: resolve (chart targets only) --------------------------- #
    if target.provides != "chart":
        base.status = "ok"
        base.detail = f"{len(rows)} rows"
        return base

    resolved = _resolve_detail(agg, rows, _probes_for_target(target, probes))
    if resolved is None:
        base.detail = (
            "rows parsed but 0 resolved to a probe route — likely a region/hub "
            "label mismatch (parser produced rows the resolver can't place)"
        )
        # Wrong hub/region for these exact pairs isn't fatal for a program whose
        # hubs differ; only the strict programs hard-fail on a resolve miss.
        base.status = "fail" if target.program in strict_programs else "warn"
        return base

    base.status = "ok"
    base.resolved = resolved
    base.detail = f"{len(rows)} rows · resolved {resolved}"
    return base


# --------------------------------------------------------------------------- #
# Orchestration + role-aware roll-up.
# --------------------------------------------------------------------------- #
def _build_agg(knowledge_dir: Optional[Path], offline: bool) -> AggregatorProvider:
    """A provider wired for a live scrape (Wayback + TLS impersonation on)."""
    fetcher = Fetcher(
        base_dir=Path(knowledge_dir) if knowledge_dir else None,
        offline=offline,
        impersonate=True,      # curl_cffi TLS fallback if installed (else no-op)
        use_wayback=not offline,
        timeout=15.0,
        max_429_retries=1,
    )
    return AggregatorProvider(
        knowledge_dir=knowledge_dir, fetcher=fetcher, offline=offline
    )


def run_live_scrape(
    *,
    knowledge_dir: Optional[Path] = None,
    offline: bool = False,
    agg: Optional[AggregatorProvider] = None,
    probes: list[Route] = DEFAULT_PROBES,
    strict_programs: frozenset[str] = STRICT_PROGRAMS,
) -> ScrapeReport:
    """Scrape every target and roll results up per program, role-aware.

    Pass an existing ``agg`` (with its fetcher configured) to reuse it; otherwise
    one is built for a live (or offline) run. Returns a :class:`ScrapeReport`
    whose ``summary.all_primaries_ok`` is the single green/red signal: True only
    when every charted program has at least one primary source that resolved.
    """
    if agg is None:
        agg = _build_agg(knowledge_dir, offline)
    is_offline = bool(agg.fetcher.offline)

    results: list[TargetResult] = []
    for target in agg.targets:
        try:
            res = check_target(
                target, agg, probes=probes, strict_programs=strict_programs
            )
        except Exception as exc:  # a parser/fetch bug is itself a target failure
            res = TargetResult(
                name=target.name, url=target.url, role=target.role,
                format=target.format, provides=target.provides, trust=target.trust,
                program=target.program, status="fail",
                detail=f"unexpected error: {type(exc).__name__}: {exc}",
            )
        # Role reclassification: a redundant fallback failing is only a WARN.
        if res.status == "fail" and target.role == "fallback":
            res.status = "warn"
            res.reclassified = True
            res.detail = f"fallback (redundant, primary covers it): {res.detail}"
        results.append(res)

    programs = _roll_up_programs(agg.targets, results)

    primary_ok = sum(1 for r in results if r.role == "primary" and r.status == "ok")
    primary_fail = sum(1 for r in results if r.role == "primary" and r.status == "fail")
    primary_warn = sum(1 for r in results if r.role == "primary" and r.status == "warn")
    fallback_ok = sum(1 for r in results if r.role == "fallback" and r.status == "ok")
    fallback_warn = sum(1 for r in results if r.role == "fallback" and r.status == "warn")
    all_primaries_ok = all(p.has_working_primary for p in programs)

    summary = {
        "total": len(results),
        "programs": len(programs),
        "all_primaries_ok": all_primaries_ok,
        "primary_ok": primary_ok,
        "primary_warn": primary_warn,
        "primary_fail": primary_fail,
        "fallback_ok": fallback_ok,
        "fallback_warn": fallback_warn,
    }
    return ScrapeReport(
        offline=is_offline, targets=results, programs=programs, summary=summary
    )


def _roll_up_programs(
    targets: list[Target], results: list[TargetResult]
) -> list[ProgramHealth]:
    """Group charted programs and decide each one's primary health.

    Scoped to ``provides == 'chart'`` targets that name a program (aeroplan,
    lifemiles, turkish, ana, krisflyer). Award/fixture sources still appear in
    the flat target list; they just aren't part of the "every program has a
    working primary" bar, which is specifically about chart coverage.
    """
    by_name = {r.name: r for r in results}
    order: list[str] = []
    buckets: dict[str, ProgramHealth] = {}
    for t in targets:
        if t.provides != "chart" or not t.program:
            continue
        ph = buckets.get(t.program)
        if ph is None:
            ph = ProgramHealth(program=t.program, has_working_primary=False)
            buckets[t.program] = ph
            order.append(t.program)
        r = by_name.get(t.name)
        if r is None:
            continue
        entry = {"name": r.name, "status": r.status, "detail": r.detail}
        if t.role == "primary":
            ph.primaries.append(entry)
            if r.status == "ok":
                ph.has_working_primary = True
        else:
            ph.fallbacks.append(entry)
    return [buckets[name] for name in order]


@dataclass
class DiscoveryScrapeResult:
    """Outcome of a discovery intake sweep (email + blogs + YouTube)."""

    row_count: int = 0
    email_docs: int = 0
    blog_new: int = 0
    transcript_new: int = 0
    email_links_followed: int = 0
    by_intake: dict = field(default_factory=dict)
    stale_programs: list = field(default_factory=list)
    used_fixtures: bool = False
    detail: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def run_discovery_intake(
    *,
    config: Optional[Any] = None,
    offline: bool = False,
    repo: Any = None,
    cache: Any = None,
    lock: Any = None,
    limit: int = 10,
) -> DiscoveryScrapeResult:
    """Run email + blog + YouTube discovery and persist ``discovered_charts.json``.

    Called from ``GET /scrape/live`` and ``mileage scrape-daily``. When ``cache``
    is provided (Redis Cloud via ``build_stores``), blog/YouTube de-dupe keys
    survive across runs so only *new* posts/videos are processed.

    Offline mode uses ``.eml`` fixtures for email; blogs/transcripts still fetch
    when not offline.
    """
    from dataclasses import replace

    from ...config import Config
    from .ingest import discovered_path, run_all_intakes, write_discovered
    from .ingest.devaluation import mark_devaluations_stale

    cfg = replace(config or Config.from_env(), offline=offline)
    try:
        result = run_all_intakes(
            cfg, repo=repo, cache=cache, lock=lock, limit=limit,
        )
    except Exception as exc:
        return DiscoveryScrapeResult(
            detail=f"discovery failed: {type(exc).__name__}: {exc}"
        )

    path = discovered_path(cfg.knowledge_dir)
    write_discovered(path, result.rows, result.stale_programs)
    if repo is not None and result.stale_programs:
        mark_devaluations_stale(repo, result.stale_programs, reason="live_scrape")

    source = "fixtures" if result.used_fixtures else "live (IMAP + RSS + YouTube)"
    return DiscoveryScrapeResult(
        row_count=len(result.rows),
        email_docs=result.email_docs,
        blog_new=result.blog_new,
        transcript_new=result.transcript_new,
        email_links_followed=result.email_links_followed,
        by_intake=result.by_intake,
        stale_programs=sorted(result.stale_programs),
        used_fixtures=result.used_fixtures,
        detail=f"{len(result.rows)} rows from {source}",
    )


def run_daily_scrape(
    *,
    config: Optional[Any] = None,
    repo: Any = None,
    limit: int = 10,
) -> dict:
    """Full daily scrape: discovery intake + chart live scrape, persisted to Redis/file.

    Intended for cron (``mileage scrape-daily``) — exits when done; no daemon.
    """
    from datetime import datetime, timezone

    from ...config import Config, build_repository, build_stores
    from .scrape_store import save_daily_snapshot

    cfg = config or Config.from_env()
    own_repo = repo is None
    repo = repo or build_repository(cfg)
    stores = build_stores(cfg, repo)
    try:
        discovery = run_discovery_intake(
            config=cfg,
            offline=False,
            repo=repo,
            cache=stores.cache,
            lock=stores.lock,
            limit=limit,
        )
        report = run_live_scrape(offline=False, knowledge_dir=cfg.knowledge_dir)
        completed_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "completed_at": completed_at,
            "storage_backend": stores.backend,
            "discovery": discovery.to_dict(),
            "scrape": report.to_dict(),
        }
        payload["storage"] = save_daily_snapshot(
            payload,
            knowledge_dir=cfg.knowledge_dir,
            cache=stores.cache,
            backend=stores.backend,
        )
        return payload
    finally:
        stores.close()
        if own_repo:
            repo.close()
