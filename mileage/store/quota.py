"""Monthly quota guard — free tiers never exceeded (§5, Phase 2).

Tracks per-provider API call counts in SQLite (single-user now; Redis atomic
counter in Phase 4). The registry consumes quota only on cache *misses* — cache
hits cost zero, so interactive re-runs within the 2-day cadence never touch
the monthly budget.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Protocol, runtime_checkable


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


@runtime_checkable
class QuotaGuard(Protocol):
    def remaining(self, provider: str, monthly_limit: Optional[int]) -> Optional[int]:
        """None = unlimited; otherwise calls left this month."""
        ...

    def consume(self, provider: str, count: int = 1) -> None:
        """Record a live fetch against this month's budget."""
        ...

    def used(self, provider: str) -> int:
        """Calls consumed this month."""
        ...

    def reset(self, provider: str, month: Optional[str] = None) -> None:
        """Reset a provider's counter (tests / admin)."""
        ...
