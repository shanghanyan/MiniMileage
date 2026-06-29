"""Async run orchestrator — maps the 4-step UI stepper to the real pipeline."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..cli import run_quote
from ..config import Config, build_registry, build_repository, build_stores
from ..domain.models import Cabin, Route, User
from ..serialize import quote_result_to_dict
from .schemas import PipelineStep, RunStatus

STEPS: tuple[PipelineStep, ...] = (
    "route",
    "gathering",
    "crosscheck",
    "redemptions",
)


@dataclass
class RunRecord:
    run_id: str
    status: RunStatus = "pending"
    step: PipelineStep = "route"
    steps_done: list[PipelineStep] = field(default_factory=list)
    result: Optional[dict] = None
    error: Optional[str] = None
    message: Optional[str] = None
    user_id: str = "local"


class RunOrchestrator:
    """Run store + pipeline driver.

    Phase 4: holds ONE shared repo + registry built on the process-shared
    `StoreBundle`, so concurrent users on the same route hit a shared cache and
    a single global quota counter (one scrape, both served) — rather than the
    Phase 3 behavior of a fresh cache per run.
    """

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or Config.from_env()
        self._repo = build_repository(self.config)
        self.stores = build_stores(self.config, self._repo)
        self._registry = build_registry(self.config, self._repo, stores=self.stores)
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.Lock()

    @property
    def repo(self):
        return self._repo

    @property
    def registry(self):
        return self._registry

    def start(
        self,
        route: Route,
        user: User,
        currency: str,
        *,
        on_complete: Optional[Callable[[RunRecord], None]] = None,
    ) -> RunRecord:
        run_id = uuid.uuid4().hex
        record = RunRecord(run_id=run_id, status="running", user_id=user.user_id)
        with self._lock:
            self._runs[run_id] = record

        thread = threading.Thread(
            target=self._execute,
            args=(run_id, route, user, currency, on_complete),
            daemon=True,
        )
        thread.start()
        return record

    def get(self, run_id: str) -> Optional[RunRecord]:
        with self._lock:
            return self._runs.get(run_id)

    def _set_step(self, run_id: str, step: PipelineStep) -> None:
        with self._lock:
            record = self._runs[run_id]
            record.step = step
            if step not in record.steps_done:
                record.steps_done.append(step)

    def _execute(
        self,
        run_id: str,
        route: Route,
        user: User,
        currency: str,
        on_complete: Optional[Callable[[RunRecord], None]],
    ) -> None:
        try:
            self._set_step(run_id, "route")

            def on_step(step: PipelineStep) -> None:
                self._set_step(run_id, step)

            raw = run_quote(
                route,
                user,
                currency,
                registry=self._registry,
                repo=self._repo,
                config=self.config,
                on_step=on_step,
            )
            payload = quote_result_to_dict(raw)
            with self._lock:
                record = self._runs[run_id]
                record.result = payload
                if payload.get("error"):
                    record.status = "error"
                    record.error = payload["error"]
                    record.message = payload.get("message")
                else:
                    record.status = "complete"
                    record.step = "redemptions"
                    if "redemptions" not in record.steps_done:
                        record.steps_done.append("redemptions")
                if on_complete:
                    on_complete(record)
        except Exception as exc:
            with self._lock:
                record = self._runs[run_id]
                record.status = "error"
                record.error = "pipeline_error"
                record.message = str(exc)

    def close(self) -> None:
        if self.stores is not None:
            self.stores.close()
        self._repo.close()


def request_to_route(req) -> Route:
    return Route(req.origin.upper(), req.dest.upper(), Cabin(req.cabin))


def request_to_user(req) -> User:
    return User(balances={req.currency: req.miles}, card=req.card)
