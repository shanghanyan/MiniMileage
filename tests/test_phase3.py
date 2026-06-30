"""Phase 3 guarantees — FastAPI orchestrator + UI API contract (§12).

Runs standalone (`python tests/test_phase3.py`) and is pytest-discoverable.

  1. POST /redemptions returns a run_id and starts the pipeline.
  2. GET /status/{run_id} advances through the 4 pipeline steps.
  3. Demo A completes with portal_only or comparable.
  4. Demo B completes with best or tentative_best.
  5. GET /freshness returns provider + cache metadata.
"""

from __future__ import annotations

import os as _os; _os.environ.setdefault("MILEAGE_OFFLINE", "1")  # hermetic standalone runs

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from mileage.api.app import app, get_config, get_orchestrator, reset_app_state
from mileage.api.orchestrator import RunOrchestrator
from mileage.config import Config


def _client(db_path: str) -> TestClient:
    reset_app_state()
    config = Config(db_path=db_path)
    orchestrator = RunOrchestrator(config)
    app.dependency_overrides[get_config] = lambda: config
    app.dependency_overrides[get_orchestrator] = lambda: orchestrator
    return TestClient(app)


def _poll(client: TestClient, run_id: str, *, max_polls: int = 60) -> dict:
    last = None
    for _ in range(max_polls):
        resp = client.get(f"/status/{run_id}")
        assert resp.status_code == 200
        last = resp.json()
        if last["status"] in ("complete", "error"):
            return last
    raise AssertionError(f"run {run_id} did not finish: {last}")


def test_redemptions_pipeline_steps() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _client(str(Path(tmp) / "t.db"))
        resp = client.post(
            "/redemptions",
            json={
                "origin": "LAX",
                "dest": "JFK",
                "cabin": "economy",
                "currency": "capital_one",
                "miles": 20000,
                "card": "venture_x",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "run_id" in body
        assert body["status"] in ("pending", "running")

        final = _poll(client, body["run_id"])
        assert final["status"] == "complete"
        assert final["step"] == "redemptions"
        assert "route" in final["steps_done"]
        assert "gathering" in final["steps_done"]
        assert "crosscheck" in final["steps_done"]
        assert "redemptions" in final["steps_done"]
        assert final["result"]["verdict"] in ("portal_only", "comparable")


def test_demo_b_best_verdict() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _client(str(Path(tmp) / "t.db"))
        resp = client.post(
            "/redemptions",
            json={
                "origin": "LAX",
                "dest": "IST",
                "cabin": "business",
                "currency": "capital_one",
                "miles": 90000,
                "card": "venture_x",
            },
        )
        final = _poll(client, resp.json()["run_id"])
        assert final["status"] == "complete"
        assert final["result"]["verdict"] in ("best", "tentative_best")
        assert final["result"]["best_transfer"] is not None
        assert final["result"]["best_transfer"]["cpp"] >= 1.25


def test_freshness_endpoint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _client(str(Path(tmp) / "t.db"))
        resp = client.get("/freshness")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cache_ttl_days"] >= 1
        assert any(p["name"] == "curated" for p in body["providers"])


def test_status_404() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client = _client(str(Path(tmp) / "t.db"))
        resp = client.get("/status/does-not-exist")
        assert resp.status_code == 404


if __name__ == "__main__":
    test_redemptions_pipeline_steps()
    test_demo_b_best_verdict()
    test_freshness_endpoint()
    test_status_404()
    print("Phase 3 tests passed.")
