"""Repository interface — the durable source of truth (§9).

Only balances, card holdings, and preferences carry a `user` dimension; all
market data (edges/quotes, runs) is shared and user-independent. SQLite backs
this in Phase 0; Turso/Supabase later behind the same interface.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from ..domain.models import User


@runtime_checkable
class Repository(Protocol):
    # --- shared market data ------------------------------------------------ #
    def put_edge(self, edge: dict[str, Any]) -> None:
        """Persist a verified graph edge (a quote with provenance)."""
        ...

    def get_edges(self, route_key: str) -> list[dict[str, Any]]:
        ...

    def record_run(self, run: dict[str, Any]) -> int:
        """Persist a query run + verdict; return the run id."""
        ...

    # --- user-scoped data -------------------------------------------------- #
    def get_user(self, user_id: str) -> Optional[User]:
        ...

    def put_user(self, user: User) -> None:
        ...

    def close(self) -> None:
        ...
