"""The Provider interface — one contract for every data source (§5).

    class Provider(Protocol):
        name: str
        def capabilities(self) -> set[Layer]: ...   # {SCHEDULES, FARES, AWARD, CHARTS}
        def remaining_quota(self) -> int | None: ...
        def fetch(self, q: Query) -> list[Quote]: ...

`fetch` returns normalized domain quotes (FareQuote / AwardQuote /
TransferRatio). The verification core consumes these identically regardless of
which provider produced them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol, Union, runtime_checkable

from ..domain.models import (
    AwardQuote,
    FareQuote,
    Layer,
    Route,
    TransferRatio,
)

Quote = Union[FareQuote, AwardQuote, TransferRatio]


@dataclass
class Query:
    """A request to the provider layer for one data layer of a route."""

    route: Route
    layer: Layer
    currency: str = "capital_one"
    # Target programs to consider for AWARD/CHARTS (empty = provider decides).
    programs: list[str] = field(default_factory=list)


class ProviderHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


@runtime_checkable
class Provider(Protocol):
    name: str
    trust: float

    def capabilities(self) -> set[Layer]: ...

    def health(self) -> ProviderHealth: ...

    def remaining_quota(self) -> Optional[int]:
        """None = effectively unlimited (e.g. local curated YAML)."""
        ...

    def fetch(self, q: Query) -> list[Quote]: ...
