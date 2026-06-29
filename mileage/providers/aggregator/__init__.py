"""ENGINE A — the aggregator (real scraper). §6

DEFAULT Layer 3 award space + Layer 4 charts source, FROM PHASE 1. It emits the
SAME normalized `AwardQuote` contract as every other provider, so the
verification core cannot tell a scrape from an API call (§2.2). Fetch stack:
httpx + optional curl_cffi, with Wayback / RSS fallbacks. No browser, no
sensor-forging — that is the Brain (Engine B), quarantined (§8).

Public surface:
  - `AggregatorProvider` — the Provider wired into the registry.
  - `Fetcher` / `PolitenessPolicy` — the resilient fetch stack (reusable, tested).
"""

from .fetch import FetchResult, Fetcher
from .politeness import PolitenessPolicy
from .provider import AggregatorProvider
from .sources import Target, load_targets

__all__ = [
    "AggregatorProvider",
    "Fetcher",
    "FetchResult",
    "PolitenessPolicy",
    "Target",
    "load_targets",
]
