"""SQLite Repository — durable source of truth for Phase 0-3 (§9).

A single file, zero infrastructure. Turso/Supabase land in Phase 4 behind the
same `Repository` interface. Phase 2 adds quota usage tracking and persisted
aggregator source health for the monthly URL-rot check.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from ..domain.models import User
from .quota import QuotaGuard, current_month


_SCHEMA = """
CREATE TABLE IF NOT EXISTS edges (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    route_key    TEXT NOT NULL,
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edges_route ON edges(route_key);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    route_key    TEXT NOT NULL,
    user_id      TEXT NOT NULL DEFAULT 'local',
    verdict      TEXT,
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    card         TEXT NOT NULL,
    balances     TEXT NOT NULL,
    preferences  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_usage (
    provider     TEXT NOT NULL,
    month        TEXT NOT NULL,
    calls        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, month)
);

CREATE TABLE IF NOT EXISTS source_health (
    source_name  TEXT PRIMARY KEY,
    url          TEXT NOT NULL,
    last_status  INTEGER,
    last_404     INTEGER NOT NULL DEFAULT 0,
    checked_at   TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteRepository:
    def __init__(self, path: str = "mileage.db") -> None:
        self.path = path
        # check_same_thread=False + a guard lock keeps Phase 0 simple and safe.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Idempotent migrations for DBs created by an earlier phase.

        `CREATE TABLE IF NOT EXISTS` never alters an existing table, so columns
        added in a later phase (Phase 4: runs.user_id) must be back-filled here
        before any index/insert touches them. Safe to run on every open.
        """
        run_cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(runs)")}
        if "user_id" not in run_cols:
            self._conn.execute(
                "ALTER TABLE runs ADD COLUMN user_id TEXT NOT NULL DEFAULT 'local'"
            )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id)"
        )

    # --- shared market data ------------------------------------------------ #
    def put_edge(self, edge: dict[str, Any]) -> None:
        route_key = edge.get("route_key", "")
        with self._lock:
            self._conn.execute(
                "INSERT INTO edges (route_key, payload, created_at) VALUES (?, ?, ?)",
                (route_key, json.dumps(edge, default=str), _now()),
            )
            self._conn.commit()

    def get_edges(self, route_key: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM edges WHERE route_key = ? ORDER BY id DESC",
                (route_key,),
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    def record_run(self, run: dict[str, Any]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO runs (route_key, user_id, verdict, payload, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    run.get("route_key", ""),
                    run.get("user_id", "local"),
                    run.get("verdict"),
                    json.dumps(run, default=str),
                    _now(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def runs_for_user(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM runs WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    # --- user-scoped data -------------------------------------------------- #
    def get_user(self, user_id: str) -> Optional[User]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return None
        return User(
            user_id=row["user_id"],
            card=row["card"],
            balances=json.loads(row["balances"]),
            preferences=json.loads(row["preferences"]),
        )

    def put_user(self, user: User) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (user_id, card, balances, preferences) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "card=excluded.card, balances=excluded.balances, "
                "preferences=excluded.preferences",
                (
                    user.user_id,
                    user.card,
                    json.dumps(user.balances),
                    json.dumps(user.preferences),
                ),
            )
            self._conn.commit()

    # --- quota (Phase 2) --------------------------------------------------- #
    def quota_used(self, provider: str, month: Optional[str] = None) -> int:
        month = month or current_month()
        with self._lock:
            row = self._conn.execute(
                "SELECT calls FROM provider_usage WHERE provider = ? AND month = ?",
                (provider, month),
            ).fetchone()
        return int(row["calls"]) if row else 0

    def quota_consume(self, provider: str, count: int = 1) -> None:
        month = current_month()
        with self._lock:
            self._conn.execute(
                "INSERT INTO provider_usage (provider, month, calls) VALUES (?, ?, ?) "
                "ON CONFLICT(provider, month) DO UPDATE SET "
                "calls = calls + excluded.calls",
                (provider, month, count),
            )
            self._conn.commit()

    def quota_reset(self, provider: str, month: Optional[str] = None) -> None:
        month = month or current_month()
        with self._lock:
            self._conn.execute(
                "DELETE FROM provider_usage WHERE provider = ? AND month = ?",
                (provider, month),
            )
            self._conn.commit()

    def quota_exhaust(self, provider: str, monthly_limit: int) -> None:
        """Set usage to the limit (simulate exhaustion for demos/tests)."""
        month = current_month()
        with self._lock:
            self._conn.execute(
                "INSERT INTO provider_usage (provider, month, calls) VALUES (?, ?, ?) "
                "ON CONFLICT(provider, month) DO UPDATE SET calls = excluded.calls",
                (provider, month, monthly_limit),
            )
            self._conn.commit()

    # --- source health (Phase 2 monthly URL-rot check) -------------------- #
    def get_source_health(self, source_name: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM source_health WHERE source_name = ?",
                (source_name,),
            ).fetchone()
        if row is None:
            return None
        return {
            "source_name": row["source_name"],
            "url": row["url"],
            "last_status": row["last_status"],
            "last_404": bool(row["last_404"]),
            "checked_at": row["checked_at"],
        }

    def put_source_health(
        self,
        source_name: str,
        url: str,
        *,
        last_status: Optional[int],
        last_404: bool,
        checked_at: Optional[str] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO source_health "
                "(source_name, url, last_status, last_404, checked_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(source_name) DO UPDATE SET "
                "url=excluded.url, last_status=excluded.last_status, "
                "last_404=excluded.last_404, checked_at=excluded.checked_at",
                (
                    source_name,
                    url,
                    last_status,
                    1 if last_404 else 0,
                    checked_at or _now(),
                ),
            )
            self._conn.commit()

    def all_source_health(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM source_health ORDER BY source_name"
            ).fetchall()
        return [
            {
                "source_name": r["source_name"],
                "url": r["url"],
                "last_status": r["last_status"],
                "last_404": bool(r["last_404"]),
                "checked_at": r["checked_at"],
            }
            for r in rows
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class SqliteQuotaGuard:
    """QuotaGuard backed by SQLiteRepository (Phase 2; Redis swap in Phase 4)."""

    def __init__(self, repo: SQLiteRepository) -> None:
        self._repo = repo

    def remaining(self, provider: str, monthly_limit: Optional[int]) -> Optional[int]:
        if monthly_limit is None:
            return None
        return max(0, monthly_limit - self._repo.quota_used(provider))

    def consume(self, provider: str, count: int = 1) -> None:
        self._repo.quota_consume(provider, count)

    def used(self, provider: str) -> int:
        return self._repo.quota_used(provider)

    def reset(self, provider: str, month: Optional[str] = None) -> None:
        self._repo.quota_reset(provider, month)
