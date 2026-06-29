"""Aggregator politeness — adaptive per-domain throttle + rotation (§6).

Scheduling efficiency, NOT evasion. Each domain learns the fastest delay that
does not draw a 429: success gently shrinks the delay toward a floor; a 429 (or
network error) widens it multiplicatively up to a ceiling, with jitter to avoid
lockstep. Source rotation is the caller's job (Fetcher walks the chain); this
class only decides *how long to wait* per domain and exposes a rotation helper.

Starts hardcoded and stays simple until volume justifies more. State lives
behind this object so the move to a Redis-backed shared limiter (Phase 4,
multi-user) is an adapter swap, not a rewrite (§9).
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Backwards-compatible module-level defaults (referenced by older callers/tests).
DEFAULT_DELAY_SECONDS = 1.0
DEFAULT_JITTER_SECONDS = 0.3
MIN_DELAY_SECONDS = 0.25
MAX_DELAY_SECONDS = 30.0
BACKOFF_FACTOR = 2.0
RECOVER_FACTOR = 0.9


@dataclass
class _DomainState:
    delay: float = DEFAULT_DELAY_SECONDS
    last_request_at: float = 0.0
    consecutive_429: int = 0
    blocks: int = 0


class PolitenessPolicy:
    def __init__(
        self,
        *,
        base_delay: float = DEFAULT_DELAY_SECONDS,
        jitter: float = DEFAULT_JITTER_SECONDS,
        min_delay: float = MIN_DELAY_SECONDS,
        max_delay: float = MAX_DELAY_SECONDS,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        self.base_delay = base_delay
        self.jitter = jitter
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._domains: dict[str, _DomainState] = {}

    def _state(self, domain: str) -> _DomainState:
        st = self._domains.get(domain)
        if st is None:
            st = _DomainState(delay=self.base_delay)
            self._domains[domain] = st
        return st

    def before_request(self, domain: str) -> None:
        """Block until this domain's adaptive delay (plus jitter) has elapsed."""
        if not domain:  # local / file fixtures: no throttle
            return
        with self._lock:
            st = self._state(domain)
            now = self._clock()
            wait = (st.last_request_at + st.delay) - now
            st.last_request_at = now + max(0.0, wait)
        if wait > 0:
            self._sleep(wait + random.uniform(0.0, self.jitter))

    def on_response(self, domain: str, status: int) -> None:
        """Adapt the delay from the outcome (429/error widens; 200 narrows)."""
        if not domain:
            return
        with self._lock:
            st = self._state(domain)
            if status == 429 or status == 0:
                st.consecutive_429 += 1
                st.delay = min(self.max_delay, st.delay * BACKOFF_FACTOR)
            else:
                st.consecutive_429 = 0
                st.delay = max(self.min_delay, st.delay * RECOVER_FACTOR)

    def record_block(self, domain: str) -> None:
        if not domain:
            return
        with self._lock:
            self._state(domain).blocks += 1

    def delay_for(self, domain: str) -> float:
        with self._lock:
            return self._state(domain).delay

    @staticmethod
    def rotate(targets: Iterable, *, skip: Optional[set] = None) -> list:
        """Order candidate sources for trying: highest trust first, skip dead."""
        skip = skip or set()
        live = [t for t in targets if getattr(t, "name", t) not in skip]
        return sorted(live, key=lambda t: getattr(t, "trust", 0.0), reverse=True)


# Legacy helper retained for compatibility with the Phase 0 placeholder import.
_DEFAULT_POLICY = PolitenessPolicy()


def delay_for(domain: str) -> float:
    return _DEFAULT_POLICY.delay_for(domain)
