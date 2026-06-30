"""URL rediscovery (§F) — deterministic by default, LLM/web search only on rot.

Deterministic scraping of known-good URLs is the default and the cheap path.
A web search is invoked ONLY when a source rots, never on the hot path:

  1. Rot detection is deterministic (no LLM): per-source health in the store
     tracks `last_404`, a consecutive-failure counter, and selector-misses
     (fetched 200 but the parser produced 0 rows, §G). A source is *rotted* when
     it 404s OR has N consecutive failures OR M consecutive selector-misses.
  2. The job runs only when the rotted-source count crosses a threshold, and is
     gated behind `MILEAGE_URL_REDISCOVERY=1` + a search key. With no key it is a
     no-op.
  3. The web search ONLY proposes candidate replacement URLs. It never extracts
     chart numbers — extraction stays deterministic + grounded.
  4. Every proposed URL must pass validation before adoption: a real fetch (not
     404) AND a content check (the structural parser must hit >=1 canonicalizable
     row). Only then is it written into sources.yaml with a low initial trust and
     a `discovered_url` provenance note. A URL that doesn't parse is discarded.
  5. Cost control: runs off the jobs queue, rate-limited, and cached (the same
     rotted source is not re-searched within TTL). Expected volume: at most one
     search per rotted source per TTL — never on the request path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, List, Optional, Protocol

import httpx

from .sources import Target

if TYPE_CHECKING:
    from ...config import Config
    from .provider import AggregatorProvider

log = logging.getLogger("mileage.aggregator.rediscover")

# Don't re-search the same rotted source within this window (cost control).
DEFAULT_REDISCOVERY_TTL = 7 * 86400.0


# --------------------------------------------------------------------------- #
# Web-search backends (pluggable; SerpAPI primary, Bing secondary)
# --------------------------------------------------------------------------- #
class WebSearch(Protocol):
    def propose_urls(self, query: str, *, limit: int = 5) -> List[str]:
        ...


class NoopSearch:
    """No key / disabled -> proposes nothing (the safe default)."""

    def propose_urls(self, query: str, *, limit: int = 5) -> List[str]:
        return []


class SerpApiSearch:
    def __init__(self, api_key: str, *, timeout: float = 10.0) -> None:
        self._key = api_key
        self._timeout = timeout

    def propose_urls(self, query: str, *, limit: int = 5) -> List[str]:  # pragma: no cover - network
        try:
            resp = httpx.get(
                "https://serpapi.com/search.json",
                params={"engine": "google", "q": query, "api_key": self._key},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.info("serpapi search failed: %s", exc)
            return []
        urls = [r.get("link") for r in data.get("organic_results", []) if r.get("link")]
        return urls[:limit]


class BingSearch:
    def __init__(self, api_key: str, *, timeout: float = 10.0) -> None:
        self._key = api_key
        self._timeout = timeout

    def propose_urls(self, query: str, *, limit: int = 5) -> List[str]:  # pragma: no cover - network
        try:
            resp = httpx.get(
                "https://api.bing.microsoft.com/v7.0/search",
                params={"q": query, "count": limit},
                headers={"Ocp-Apim-Subscription-Key": self._key},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.info("bing search failed: %s", exc)
            return []
        items = (data.get("webPages") or {}).get("value", [])
        return [it.get("url") for it in items if it.get("url")][:limit]


def build_search(config: "Config") -> WebSearch:
    """Pick a search backend from config; NoopSearch when disabled/keyless."""
    if not config.url_rediscovery_enabled:
        return NoopSearch()
    if config.serpapi_api_key:
        return SerpApiSearch(config.serpapi_api_key)
    if config.bing_search_api_key:
        return BingSearch(config.bing_search_api_key)
    return NoopSearch()


# --------------------------------------------------------------------------- #
# Rediscovery run
# --------------------------------------------------------------------------- #
@dataclass
class Adoption:
    source_name: str
    old_url: str
    new_url: str


@dataclass
class RediscoveryReport:
    ran: bool = False
    reason: str = ""
    rotted: List[str] = field(default_factory=list)
    adoptions: List[Adoption] = field(default_factory=list)
    rejected: List[str] = field(default_factory=list)


def _query_for(target: Target) -> str:
    program = target.program or target.name
    kind = "award chart" if target.provides == "chart" else "award availability"
    return f"{program} partner {kind}"


def validate_candidate(
    provider: "AggregatorProvider", rotted: Target, url: str
) -> bool:
    """A candidate is adoptable only if it fetches (not 404) AND parses >=1 row."""
    ok, status = provider.fetcher.head_ok(url)
    if not ok or status in (404, 410):
        return False
    result = provider.fetcher.get(url)
    if result is None or not result.ok:
        return False
    probe = Target(
        name=f"{rotted.name}-candidate",
        url=url,
        format=rotted.format,
        provides=rotted.provides,
        trust=0.3,
        program=rotted.program,
    )
    try:
        return provider._content_rows(probe, result.text) > 0  # noqa: SLF001
    except Exception as exc:
        log.info("candidate content check failed for %s: %s", url, exc)
        return False


def _adopt(provider: "AggregatorProvider", rotted: Target, url: str) -> None:
    """Append a low-trust, provenance-noted target to sources.yaml (§F.4)."""
    path = provider._sources_path  # noqa: SLF001 - same package
    today = datetime.now(timezone.utc).date().isoformat()
    block = [
        "",
        f"  # discovered_url: auto-adopted by URL rediscovery (§F) on {today}",
        f"  #   replaces rotted source '{rotted.name}'",
        f"  - name: {rotted.name}-rediscovered",
        f"    url: {url}",
        f"    format: {rotted.format}",
    ]
    if rotted.program:
        block.append(f"    program: {rotted.program}")
    block += [
        f"    provides: {rotted.provides}",
        "    trust: 0.30",
        f'    updated_at: "{today}"',
        "    layers: [charts]",
        "    discovered_url: true",
        "",
    ]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(block))
    log.info("rediscovery adopted %s -> %s", rotted.name, url)


def run_rediscovery(
    provider: "AggregatorProvider",
    config: "Config",
    *,
    search: Optional[WebSearch] = None,
    cache: Any = None,
    rate_limiter: Any = None,
    write: bool = True,
    ttl: float = DEFAULT_REDISCOVERY_TTL,
) -> RediscoveryReport:
    """Rediscover replacement URLs for rotted sources (gated, bounded, cached)."""
    report = RediscoveryReport()
    if not config.url_rediscovery_enabled:
        report.reason = "disabled (MILEAGE_URL_REDISCOVERY unset)"
        return report
    if search is None:
        search = build_search(config)
    if isinstance(search, NoopSearch):
        report.reason = "no search backend (no SERPAPI/BING key)"
        return report

    rotted = provider.rotted_targets(
        max_failures=config.rot_max_failures,
        max_selector_misses=config.rot_max_selector_misses,
    )
    report.rotted = [t.name for t in rotted]
    if len(rotted) < config.rediscovery_min_rotted:
        report.reason = (
            f"{len(rotted)} rotted < threshold {config.rediscovery_min_rotted}"
        )
        return report

    report.ran = True
    for t in rotted:
        cache_key = f"rediscover:{t.name}"
        if cache is not None and cache.get(cache_key):
            continue  # already searched within TTL (cost control)
        if rate_limiter is not None and not rate_limiter.allow("rediscovery"):
            log.info("rediscovery rate-limited; stopping this sweep")
            break
        candidates = search.propose_urls(_query_for(t))
        if cache is not None:
            cache.set(cache_key, "1", ttl)
        adopted: Optional[str] = None
        for url in candidates:
            if validate_candidate(provider, t, url):
                adopted = url
                break
            report.rejected.append(url)
        if adopted:
            if write:
                _adopt(provider, t, adopted)
            report.adoptions.append(
                Adoption(source_name=t.name, old_url=t.url, new_url=adopted)
            )
    return report
