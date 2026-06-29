"""Phase 4 guarantees — multi-user + shared memory layer (§9, §12 Phase 4).

Runs standalone (`python tests/test_phase4.py`) and is pytest-discoverable.

  1. Shared hot cache: a warm route is served from cache (zero live fetches).
  2. One scrape, both served: two concurrent users on the same route trigger a
     single set of live fetches (no double-scrape).
  3. Global quota counter is shared — charged once for two users, not twice.
  4. Per-user verdicts: same market data, different balances -> different verdict.
  5. User persistence + run scoping via the Repository.
  6. Auth resolves the acting user from a bearer token; balances are server-side.
  7. Redis adapters (Cache/RateLimiter/Lock/QuotaGuard) behave correctly
     (via fakeredis; skipped if unavailable).
  8. Redis backend selection degrades gracefully when the server is unreachable.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.cli import DEFAULT_CURRENCY, run_quote
from mileage.config import Config, build_registry, build_repository, build_stores
from mileage.domain.models import Cabin, Route, User

ROUTE_B = Route("LAX", "IST", Cabin.BUSINESS)


def _setup(db_path: str):
    config = Config(db_path=db_path)
    repo = build_repository(config)
    stores = build_stores(config, repo)
    registry = build_registry(config, repo, stores=stores)
    return config, repo, stores, registry


# --------------------------------------------------------------------------- #
# 1 + 2 + 3 — shared cache, one scrape both served, shared quota counter
# --------------------------------------------------------------------------- #
def test_shared_cache_one_scrape_both_served() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config, repo, stores, reg = _setup(str(Path(tmp) / "t.db"))
        user = User(balances={DEFAULT_CURRENCY: 90000})

        # Cold baseline: how many live fetches does one run cost?
        reg.cache.clear()
        reg.reset_stats()
        run_quote(ROUTE_B, user, DEFAULT_CURRENCY, registry=reg, repo=repo, config=config)
        single_misses = reg.stats.cache_misses
        assert single_misses > 0

        # Two concurrent users on the SAME route, cold cache.
        reg.cache.clear()
        reg.reset_stats()
        alice = User(user_id="alice", balances={DEFAULT_CURRENCY: 30000})
        bob = User(user_id="bob", balances={DEFAULT_CURRENCY: 90000})
        threads = [
            threading.Thread(
                target=run_quote,
                args=(ROUTE_B, u, DEFAULT_CURRENCY),
                kwargs=dict(registry=reg, repo=repo, config=config),
            )
            for u in (alice, bob)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # One scrape total (no double-scrape) + the other user served from cache.
        assert reg.stats.cache_misses == single_misses, "two users must not double-scrape"
        assert reg.stats.cache_hits == single_misses, "second user served from cache"
        repo.close()
        stores.close()


def test_global_quota_counter_shared() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config, repo, stores, reg = _setup(str(Path(tmp) / "t.db"))
        user = User(balances={DEFAULT_CURRENCY: 90000})

        run_quote(ROUTE_B, user, DEFAULT_CURRENCY, registry=reg, repo=repo, config=config)
        used_after_one = repo.quota_used("travelpayouts")
        # A second user on the same route is served from cache -> zero new quota.
        run_quote(ROUTE_B, user, DEFAULT_CURRENCY, registry=reg, repo=repo, config=config)
        used_after_two = repo.quota_used("travelpayouts")

        assert used_after_one >= 1
        assert used_after_two == used_after_one, "shared cache -> quota charged once"
        repo.close()
        stores.close()


# --------------------------------------------------------------------------- #
# 4 — per-user verdicts from shared market data
# --------------------------------------------------------------------------- #
def test_per_user_verdicts_differ_by_balance() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config, repo, stores, reg = _setup(str(Path(tmp) / "t.db"))
        alice = User(user_id="alice", balances={DEFAULT_CURRENCY: 30000}, card="venture_x")
        bob = User(user_id="bob", balances={DEFAULT_CURRENCY: 90000}, card="venture_x")

        ra = run_quote(ROUTE_B, alice, DEFAULT_CURRENCY, registry=reg, repo=repo, config=config)
        rb = run_quote(ROUTE_B, bob, DEFAULT_CURRENCY, registry=reg, repo=repo, config=config)

        # Same verified market data, but the verdict is computed per balance.
        assert ra["verdict"].label.value in ("portal_only", "comparable")
        assert rb["verdict"].label.value in ("best", "tentative_best")
        assert rb["verdict"].best_transfer is not None
        repo.close()
        stores.close()


# --------------------------------------------------------------------------- #
# 5 — user persistence + run scoping
# --------------------------------------------------------------------------- #
def test_user_persistence_and_run_scoping() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        config, repo, stores, reg = _setup(str(Path(tmp) / "t.db"))
        bob = User(user_id="bob", balances={DEFAULT_CURRENCY: 90000}, card="venture")
        repo.put_user(bob)

        loaded = repo.get_user("bob")
        assert loaded is not None
        assert loaded.balances[DEFAULT_CURRENCY] == 90000
        assert loaded.card == "venture"
        assert repo.get_user("nobody") is None

        run_quote(ROUTE_B, loaded, DEFAULT_CURRENCY, registry=reg, repo=repo, config=config)
        runs = repo.runs_for_user("bob")
        assert len(runs) == 1
        assert runs[0]["user_id"] == "bob"
        assert repo.runs_for_user("alice") == []
        repo.close()
        stores.close()


# --------------------------------------------------------------------------- #
# 6 — auth resolves the acting user; balances are server-side truth
# --------------------------------------------------------------------------- #
def test_auth_resolution() -> None:
    from fastapi import HTTPException

    from mileage.api.auth import resolve_user

    with tempfile.TemporaryDirectory() as tmp:
        repo = build_repository(Config(db_path=str(Path(tmp) / "t.db")))
        repo.put_user(User(user_id="bob", balances={DEFAULT_CURRENCY: 90000}))

        # Auth off: token (or none) maps to a permissive local-style account.
        u = resolve_user(repo, token=None, auth_enabled=False)
        assert u.user_id == "local"

        # Auth on: token IS the user id and must exist in the Repository.
        bob = resolve_user(repo, token="bob", auth_enabled=True)
        assert bob.balances[DEFAULT_CURRENCY] == 90000

        for bad in (None, "ghost"):
            try:
                resolve_user(repo, token=bad, auth_enabled=True)
                raise AssertionError("expected HTTPException")
            except HTTPException as exc:
                assert exc.status_code == 401
        repo.close()


def test_api_auth_scopes_balances() -> None:
    from fastapi.testclient import TestClient

    from mileage.api.app import app, get_config, get_orchestrator, reset_app_state
    from mileage.api.orchestrator import RunOrchestrator

    with tempfile.TemporaryDirectory() as tmp:
        reset_app_state()
        config = Config(db_path=str(Path(tmp) / "t.db"), auth_enabled=True)
        orchestrator = RunOrchestrator(config)
        app.dependency_overrides[get_config] = lambda: config
        app.dependency_overrides[get_orchestrator] = lambda: orchestrator
        client = TestClient(app)

        # Seed two accounts (server-side truth).
        client.put("/users/alice", json={"card": "venture_x",
                                          "balances": {DEFAULT_CURRENCY: 30000}})
        client.put("/users/bob", json={"card": "venture_x",
                                       "balances": {DEFAULT_CURRENCY: 90000}})

        # No token -> 401.
        assert client.post("/redemptions", json={"origin": "LAX", "dest": "IST",
                                                  "cabin": "business"}).status_code == 401

        def verdict_for(token: str) -> str:
            resp = client.post(
                "/redemptions",
                headers={"Authorization": f"Bearer {token}"},
                json={"origin": "LAX", "dest": "IST", "cabin": "business"},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["user_id"] == token
            run_id = resp.json()["run_id"]
            for _ in range(60):
                s = client.get(f"/status/{run_id}").json()
                if s["status"] in ("complete", "error"):
                    assert s["status"] == "complete", s
                    return s["result"]["verdict"]
            raise AssertionError("run did not finish")

        # Same route, balances from the token's account -> different verdicts.
        assert verdict_for("alice") in ("portal_only", "comparable")
        assert verdict_for("bob") in ("best", "tentative_best")

        me = client.get("/me", headers={"Authorization": "Bearer bob"}).json()
        assert me["balances"][DEFAULT_CURRENCY] == 90000

        app.dependency_overrides.clear()
        reset_app_state()


# --------------------------------------------------------------------------- #
# 7 — Redis adapters (fakeredis); skipped if redis/fakeredis unavailable
# --------------------------------------------------------------------------- #
def _fake_redis():
    try:
        import fakeredis  # type: ignore
    except ImportError:
        return None
    return fakeredis.FakeStrictRedis()


def test_redis_adapters() -> None:
    client = _fake_redis()
    if client is None:
        print("  (skipped Redis adapter test — fakeredis not installed)")
        return

    from mileage.store.redis_impl import (
        RedisCache,
        RedisLock,
        RedisQuotaGuard,
        RedisRateLimiter,
    )

    cache = RedisCache(client)
    cache.set("k", [{"program": "turkish", "miles": 45000}], ttl_seconds=60)
    assert cache.get("k") == [{"program": "turkish", "miles": 45000}]
    cache.delete("k")
    assert cache.get("k") is None

    try:
        rl = RedisRateLimiter(client, rate=0.0001, capacity=2)
        assert rl.allow("p") and rl.allow("p")
        assert not rl.allow("p"), "third call exhausts the 2-token bucket"
    except Exception as exc:  # fakeredis without the [lua] extra can't EVAL
        if "evalsha" in str(exc).lower() or "script" in str(exc).lower():
            print("  (skipped RedisRateLimiter — fakeredis lacks Lua scripting)")
        else:
            raise

    lock = RedisLock(client, ttl_seconds=5, wait_seconds=0.2, poll_seconds=0.02)
    with lock.acquire("route") as a:
        assert a
        with lock.acquire("route") as b:
            assert b is False, "already held -> waiter times out (de-dupe)"
    with lock.acquire("route") as c:
        assert c, "released -> acquirable again"

    q = RedisQuotaGuard(client)
    q.consume("amadeus", 1)
    q.consume("amadeus", 2)
    assert q.used("amadeus") == 3
    assert q.remaining("amadeus", 10) == 7
    assert q.remaining("amadeus", None) is None
    q.exhaust("amadeus", 10)
    assert q.remaining("amadeus", 10) == 0
    q.reset("amadeus")
    assert q.used("amadeus") == 0


def test_redis_backend_falls_back_when_unreachable() -> None:
    # Points at a port nothing is listening on -> build_stores must degrade.
    config = Config(redis_url="redis://127.0.0.1:6599/0")
    with tempfile.TemporaryDirectory() as tmp:
        repo = build_repository(Config(db_path=str(Path(tmp) / "t.db")))
        stores = build_stores(config, repo)
        assert stores.backend == "inproc", "unreachable Redis must fall back to in-proc"
        stores.close()
        repo.close()


if __name__ == "__main__":
    test_shared_cache_one_scrape_both_served()
    test_global_quota_counter_shared()
    test_per_user_verdicts_differ_by_balance()
    test_user_persistence_and_run_scoping()
    test_auth_resolution()
    test_api_auth_scopes_balances()
    test_redis_adapters()
    test_redis_backend_falls_back_when_unreachable()
    print(
        "OK: shared cache (one scrape, both served), shared quota counter, "
        "per-user verdicts, user persistence + run scoping, bearer auth, "
        "Redis adapters, and graceful Redis fallback."
    )
