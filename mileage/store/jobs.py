"""Background job queue — scrapes/refreshes run off the request path (§9.4).

In a multi-user service you don't want a user's request to block on a live
scrape. The registry already de-dupes concurrent fetches behind the `Lock`
interface; this adds the *other* half — the ability to warm the shared cache
ahead of (or behind) a request, so interactive traffic is served from cache.

`InProcJobQueue` is the local/single-process worker pool (Phase 4 default). The
multi-worker swap is a Redis list (`LPUSH`/`BRPOP`) consumed by a separate
worker process — same `JobQueue` interface, no caller changes.
"""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol, runtime_checkable

log = logging.getLogger("mileage.jobs")


@dataclass
class Job:
    fn: Callable[..., Any]
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    name: str = "job"


@runtime_checkable
class JobQueue(Protocol):
    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "job",
        **kwargs: Any,
    ) -> None:
        """Enqueue work to run off the request path."""
        ...

    def close(self) -> None:
        ...


class InProcJobQueue:
    """A small daemon worker pool. Drains a queue of jobs in the background.

    Workers start lazily on the first `submit`, so a transient registry that
    never enqueues anything (e.g. the /freshness snapshot) spawns no threads.
    """

    def __init__(self, *, workers: int = 2) -> None:
        self._q: "queue.Queue[Optional[Job]]" = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._workers = max(1, workers)
        self._started = False
        self._closed = False
        self._processed = 0
        self._lock = threading.Lock()

    def _ensure_started(self) -> None:
        with self._lock:
            if self._started or self._closed:
                return
            self._started = True
            for i in range(self._workers):
                t = threading.Thread(
                    target=self._run, name=f"mileage-worker-{i}", daemon=True
                )
                t.start()
                self._threads.append(t)

    def _run(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                self._q.task_done()
                return
            try:
                job.fn(*job.args, **job.kwargs)
            except Exception as exc:  # never let a bad job kill the worker
                log.warning("background job %s failed: %s", job.name, exc)
            finally:
                with self._lock:
                    self._processed += 1
                self._q.task_done()

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        name: str = "job",
        **kwargs: Any,
    ) -> None:
        if self._closed:
            raise RuntimeError("job queue is closed")
        self._ensure_started()
        self._q.put(Job(fn=fn, args=args, kwargs=kwargs, name=name))

    def join(self, timeout: Optional[float] = None) -> None:
        """Block until the queue drains (tests / graceful shutdown)."""
        if timeout is None:
            self._q.join()
            return
        # queue.join has no timeout; poll unfinished tasks instead.
        import time

        deadline = time.monotonic() + timeout
        while self._q.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)

    @property
    def processed(self) -> int:
        with self._lock:
            return self._processed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for _ in self._threads:
            self._q.put(None)
        self._threads.clear()
