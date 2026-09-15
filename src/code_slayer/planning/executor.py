"""`PlanningJobExecutor`: the server-owned background dispatcher for
durable planning jobs (Phase 8.2d).

## HTTP client lifetime has zero authority here

`api.service.ApplicationService` constructs exactly one
`PlanningJobExecutor` per process (started once, at server startup,
alongside the Flask app itself) — never one per HTTP request. Once
`EngineeringPlanningService.create_job()`/`.replan_job()` durably commits
a `QUEUED` row, this module is the only thing that ever executes it; an
HTTP request that created or polls a job holds no reference this module
depends on, and a disconnecting/backgrounded/suspended client changes
nothing about whether or when the job runs. `notify()` is purely a
latency optimization (wake the dispatcher promptly instead of waiting
for its next poll) — never a requirement for correctness.

## Bounded, never unbounded

Exactly `max_workers` planning turns ever run concurrently (a
`concurrent.futures.ThreadPoolExecutor` of that fixed size) — default 1:
correctness over throughput, and today's real local-model inference is
itself bottlenecked on one GPU/model instance regardless. The dispatcher
never spawns a thread per job; it submits into this fixed pool and skips
a job already `_in_flight` in this process, so a duplicate discovery
(e.g. two consecutive poll passes before the first submission's `claim_job()`
has actually run yet) can never submit the same job twice from this
process alone. The *durable* guarantee against double-execution —
across processes, or across a discovery race within one process — is
`EngineeringPlanningService.claim_job()`'s own atomic, fenced claim
(`store.db.transaction()`), not this in-process set; the set is only an
optimization to avoid wasted `claim_job()` calls that would fail anyway.

## Crash/restart recovery

`start()` immediately runs one discovery pass before entering its poll
loop, so a process that restarts with `QUEUED` jobs (Case A) or
abandoned `RUNNING` jobs whose recorded owner is provably dead (Case B,
via `claimable_job_ids()`'s liveness evidence) begins executing them
without waiting for external intervention. A job already terminal
(Case C) is never rediscovered — `claimable_job_ids()` only ever
considers `QUEUED`/non-abandoned-`RUNNING` rows, and the
`SUCCEEDED`/`FAILED` states from migration `0009`'s own
`planning_jobs_no_reopen_terminal` trigger cannot be reopened even by a
bug in this module.

## Never let one execution's exception kill the dispatcher

`EngineeringPlanningService.execute_claimed_job()` already turns any
exception into a durable `FAILED` job with `failure_category=
"internal_error"`. This module additionally wraps the whole submitted
call in its own `try/except`, purely as defense in depth — an exception
escaping here must never propagate into `ThreadPoolExecutor`'s worker
thread in a way that could stop future submissions from that thread
slot.

## Connections

Every execution constructs its own fresh `EngineeringPlanningService`
(its own `sqlite3.Connection`, per `store.db.connect()`'s documented
one-connection-per-instance discipline) and closes it when done — never
a connection shared across threads, matching every other service in
this codebase.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from code_slayer.planning.planner import Planner
from code_slayer.planning.service import EngineeringPlanningService

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_MAX_WORKERS = 1


class PlanningJobExecutor:
    def __init__(
        self, repo_path: Path | str, *, planner_factory, state_root_override=None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._repo_path = repo_path
        self._state_root_override = state_root_override
        self._planner_factory = planner_factory
        self._max_workers = max_workers
        self._poll_interval = poll_interval_seconds
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="planning-job")
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._dispatcher_thread: threading.Thread | None = None

    def start(self) -> None:
        """Idempotent-in-spirit for this process's own lifetime — call
        once at server startup. Runs one immediate discovery pass
        (crash/restart recovery) before the dispatcher thread's own poll
        loop takes over."""
        if self._dispatcher_thread is not None:
            return
        self._dispatch_once()
        self._dispatcher_thread = threading.Thread(
            target=self._loop, name="planning-job-dispatcher", daemon=True,
        )
        self._dispatcher_thread.start()

    def notify(self) -> None:
        """Wake the dispatcher promptly instead of waiting for its next
        poll — a latency optimization only; never required for
        correctness (the poll loop alone still discovers and runs every
        job eventually)."""
        self._wakeup.set()

    def stop(self) -> None:
        """Best-effort: signals the dispatcher loop to exit and stops
        accepting new submissions. Does not cancel in-flight executions
        (a daemon thread pool; process exit does not need to wait on
        them — an abrupt exit mid-execution is exactly Case B, already
        safely recoverable on the next start)."""
        self._stop.set()
        self._wakeup.set()
        if self._dispatcher_thread is not None:
            self._dispatcher_thread.join(timeout=5)
        self._pool.shutdown(wait=False)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wakeup.wait(timeout=self._poll_interval)
            self._wakeup.clear()
            if self._stop.is_set():
                return
            self._dispatch_once()

    def _dispatch_once(self) -> None:
        try:
            service = EngineeringPlanningService(
                self._repo_path, state_root_override=self._state_root_override,
            )
        except Exception:
            logger.exception("planning job dispatcher: could not open service for discovery")
            return
        try:
            claimable = service.claimable_job_ids()
        except Exception:
            logger.exception("planning job dispatcher: discovery pass failed")
            return
        finally:
            service.close()
        for job_id in claimable:
            with self._lock:
                if job_id in self._in_flight or len(self._in_flight) >= self._max_workers:
                    continue
                self._in_flight.add(job_id)
            self._pool.submit(self._run_one, job_id)

    def _run_one(self, job_id: str) -> None:
        try:
            service = EngineeringPlanningService(
                self._repo_path, state_root_override=self._state_root_override,
            )
            try:
                claimed = service.claim_job(job_id)
                if claimed is None:
                    return  # lost the race, already terminal, or owner still alive
                planner: Planner = self._planner_factory()
                service.execute_claimed_job(claimed, planner)
            finally:
                service.close()
        except Exception:
            logger.exception("planning job dispatcher: execution of %s failed", job_id)
        finally:
            with self._lock:
                self._in_flight.discard(job_id)
            self.notify()
