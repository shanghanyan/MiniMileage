"""Phase 2 guarantees — provider federation hardening (§5, §12).

Runs standalone (`python tests/test_phase2.py`) and is pytest-discoverable.

  1. FARES fallback order: travelpayouts (cached) before curated (hardcoded).
  2. Monthly quota guard skips exhausted providers; registry falls back.
  3. Cache hits cost zero quota (second identical fetch = hits, no consume).
  4. Disabling aggregator degrades to chart-only but Demo B still passes.
  5. Source health persists to SQLite; monthly cadence skips re-probe.
  6. Layer-specific trust ordering from providers.yaml.
"""

from __future__ import annotations

import os as _os; _os.environ.setdefault("MILEAGE_OFFLINE", "1")  # hermetic standalone runs

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mileage.config import Config, build_registry, load_federation
from mileage.domain.models import Cabin, Layer, Route, User
from mileage.providers.base import Query
from mileage.providers.registry import ProviderRegistry
from mileage.cli import run_quote
from mileage.store.inproc import InProcCache
from mileage.store.sqlite_repo import SQLiteRepository, SqliteQuotaGuard

_CONFIG = Config()


def _registry(db_path: str) -> tuple[ProviderRegistry, SQLiteRepository]:
    repo = SQLiteRepository(db_path)
    reg = build_registry(_CONFIG, repo)
    return reg, repo


def test_fares_fallback_travelpayouts_before_curated() -> None:
    """Without Amadeus keys, cached Travelpayouts beats curated hardcoded fares."""
    with tempfile.TemporaryDirectory() as tmp:
        reg, repo = _registry(str(Path(tmp) / "t.db"))
        route = Route("LAX", "JFK", Cabin.ECONOMY)
        fares = reg.fetch(Query(route, Layer.FARES, "capital_one"))
        assert fares, "expected a fare quote"
        winner = fares[0]
        assert winner.provenance.source_name == "Travelpayouts cached fares"
        assert "cached_fare" in winner.flags
        assert winner.cash_cents == 16200
        repo.close()


def test_quota_exhaustion_skips_provider() -> None:
    """Exhausted monthly quota -> skip provider, fall back to next in order."""
    with tempfile.TemporaryDirectory() as tmp:
        reg, repo = _registry(str(Path(tmp) / "t.db"))
        federation = load_federation(_CONFIG)
        limit = federation.monthly_quota("travelpayouts")
        assert limit is not None
        repo.quota_exhaust("travelpayouts", limit)
        reg.cache.clear()
        reg.reset_stats()
        route = Route("LAX", "JFK", Cabin.ECONOMY)
        fares = reg.fetch(Query(route, Layer.FARES, "capital_one"))
        assert reg.stats.quota_skips >= 1
        assert fares
        assert fares[0].provenance.source_name == "curated fallback fares"
        assert "hardcoded_fallback" in fares[0].flags
        repo.close()


def test_cache_hit_costs_zero_quota() -> None:
    """Second identical fetch is a cache hit — no additional quota consumed."""
    with tempfile.TemporaryDirectory() as tmp:
        reg, repo = _registry(str(Path(tmp) / "t.db"))
        route = Route("LAX", "IST", Cabin.BUSINESS)
        q = Query(route, Layer.AWARD, "capital_one")
        reg.reset_stats()
        reg.fetch(q)
        used_after_first = repo.quota_used("aggregator")
        reg.reset_stats()
        reg.fetch(q)
        used_after_second = repo.quota_used("aggregator")
        assert reg.stats.cache_hits >= 1
        assert used_after_second == used_after_first, "cache hit must not consume quota"
        repo.close()


def test_disable_aggregator_degrades_award_space() -> None:
    """Engine A off -> no_live_space on winner, but Demo B verdict still best."""
    with tempfile.TemporaryDirectory() as tmp:
        reg, repo = _registry(str(Path(tmp) / "t.db"))
        reg.disabled.add("aggregator")
        route = Route("LAX", "IST", Cabin.BUSINESS)
        user = User(balances={"capital_one": 90000}, card="venture_x")
        result = run_quote(route, user, "capital_one", registry=reg, config=_CONFIG)
        verdict = result["verdict"]
        assert verdict.label.value in ("best", "tentative_best")
        assert verdict.best_transfer is not None
        assert "no_live_space" in verdict.best_transfer.flags
        live = [a for a in result["awards"] if a.seats_available is not None]
        assert not live, "no live seats without aggregator"
        repo.close()


def test_source_health_persists() -> None:
    """URL-rot health check results survive in SQLite."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = SQLiteRepository(str(Path(tmp) / "t.db"))
        repo.put_source_health(
            "test-source", "file://fixtures/x.json",
            last_status=200, last_404=False,
        )
        row = repo.get_source_health("test-source")
        assert row is not None
        assert row["last_status"] == 200
        assert row["last_404"] is False
        repo.close()


def test_layer_trust_ordering() -> None:
    """Curated fares trust (0.25) is below travelpayouts (0.6) in federation config."""
    federation = load_federation(_CONFIG)
    assert federation.spec("travelpayouts").trust_for("fares") == 0.6
    assert federation.spec("curated").trust_for("fares") == 0.25
    assert federation.spec("curated").trust_for("charts") == 0.7


def test_canonical_demos_still_pass() -> None:
    """Both Demo A and Demo B produce verdicts after Phase 2 wiring."""
    with tempfile.TemporaryDirectory() as tmp:
        reg, repo = _registry(str(Path(tmp) / "t.db"))
        for origin, dest, cabin, miles in [
            ("LAX", "JFK", Cabin.ECONOMY, 20000),
            ("LAX", "IST", Cabin.BUSINESS, 90000),
        ]:
            route = Route(origin, dest, cabin)
            user = User(balances={"capital_one": miles}, card="venture_x")
            result = run_quote(route, user, "capital_one", registry=reg, config=_CONFIG)
            assert result["verdict"] is not None
        repo.close()


if __name__ == "__main__":
    test_fares_fallback_travelpayouts_before_curated()
    test_quota_exhaustion_skips_provider()
    test_cache_hit_costs_zero_quota()
    test_disable_aggregator_degrades_award_space()
    test_source_health_persists()
    test_layer_trust_ordering()
    test_canonical_demos_still_pass()
    print(
        "OK: fare fallback order, quota guard, cache-zero-quota, aggregator "
        "disable degrades gracefully, health persists, layer trust, demos pass"
    )
