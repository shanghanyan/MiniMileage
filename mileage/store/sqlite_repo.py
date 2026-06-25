"""SQLite Repository — durable source of truth for Phase 0-3 (§9).

A single file, zero infrastructure. Turso/Supabase land in Phase 4 behind the
same `Repository` interface.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from ..domain.models import User


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
            self._conn.commit()

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
                "INSERT INTO runs (route_key, verdict, payload, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    run.get("route_key", ""),
                    run.get("verdict"),
                    json.dumps(run, default=str),
                    _now(),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

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

    def close(self) -> None:
        with self._lock:
            self._conn.close()
