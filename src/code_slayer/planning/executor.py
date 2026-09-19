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

## H.4: worker-bound routing, revalidated fresh on every execution

A job's Planner worker was already durably selected and fixed at
creation time (`planning.routing.select_planner_route()`, called from
`api.service.ApplicationService.create_plan()`/`.replan_plan()` — never
by this module). `self._planner_factory` is GONE from this module's own
execution path: `_run_one()` never calls a zero-argument factory. For
each claimed job it instead:

1. Rebuilds `job`'s own durable `planning.routing.PlannerRouteBinding`
   (`planning.routing.route_binding_from_job()`) — `None` for a legacy,
   pre-H.4 job with no binding at all.
2. Resolves FRESH routing inputs (`_routing_inputs()`, below) — never a
   snapshot captured once at process/executor construction time.
3. Calls `planning.routing.revalidate_route_binding()` against that
   fresh snapshot, `verify_live_runtime=True` (this module always runs
   from a background thread, never an HTTP request thread — the one
   place in this codebase a live Ollama probe belongs during job
   execution). Any refusal (`ok=False`) durably fails the job via
   `EngineeringPlanningService.fail_claimed_job()`, `failure_category=
   "routing"` — no Planner factory call, no model call, ever.
4. Only once revalidation passes does it call `planner_factory_for_
   worker(job.worker_id, job.job_id)` and hand the result to
   `execute_claimed_job()`.

### Never a stale config/binding snapshot

`bindings_factory` (optional), when given, is called FRESH on every
single job execution — mirrors `security.certification_executor.
CertificationJobExecutor._service()`'s own established pattern for the
identical reason: `api.service.ApplicationService` constructs this
dispatcher exactly once at process startup, but persistent worker
configuration can change (a config save) at any later point while the
process keeps running. A `baseline_targets`/`role_targets`/
`planner_factory_for_worker` triple captured once in `__init__` would
silently keep using whatever configuration existed at that one instant,
forever — `bindings_factory` (typically `ApplicationService.
_compose_bindings`) is what lets a long-lived dispatcher observe a
config change the very next time it executes a job, without needing to
be restarted. The `baseline_targets`/`role_targets`/
`planner_factory_for_worker` constructor arguments remain as a static
fallback only for callers that genuinely have no persistent config to
reload (tests, and any dev wiring that never calls
`ApplicationService(..., load_persistent_config=True)`).
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from code_slayer.planning.models import JobFailureCategory
from code_slayer.planning.planner import Planner
from code_slayer.planning.routing import revalidate_route_binding, route_binding_from_job
from code_slayer.planning.service import EngineeringPlanningService

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 2.0
DEFAULT_MAX_WORKERS = 1


class PlanningJobExecutor:
    def __init__(
        self, repo_path: Path | str, *,
        planner_factory_for_worker=None,
        baseline_targets=(),
        role_targets=(),
        bindings_factory=None,
        state_root_override=None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._repo_path = repo_path
        self._state_root_override = state_root_override
        self._planner_factory_for_worker = planner_factory_for_worker
        self._baseline_targets = baseline_targets
        self._role_targets = role_targets
        self._bindings_factory = bindings_factory
        self._max_workers = max_workers
        self._poll_interval = poll_interval_seconds
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="planning-job")
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._dispatcher_thread: threading.Thread | None = None

    def _routing_inputs(self):
        """Fresh, per-execution `(baseline_targets, role_targets,
        planner_factory_for_worker)` — see the module docstring's
        "Never a stale config/binding snapshot" section."""
        if self._bindings_factory is not None:
            bindings = self._bindings_factory()
            return (
                bindings.baseline_certification_targets,
                bindings.role_evaluation_targets,
                bindings.planner_factory_for_worker,
            )
        return self._baseline_targets, self._role_targets, self._planner_factory_for_worker

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
                baseline_targets, role_targets, planner_factory_for_worker = (
                    self._routing_inputs()
                )
                binding = route_binding_from_job(claimed)
                revalidation = revalidate_route_binding(
                    service.production_conn(), binding,
                    baseline_targets=baseline_targets, role_targets=role_targets,
                    verify_live_runtime=True,
                )
                if not revalidation.ok:
                    service.fail_claimed_job(
                        claimed, failure_category=JobFailureCategory.ROUTING.value,
                        failure_reason=revalidation.failure_reason,
                    )
                    return
                if planner_factory_for_worker is None:
                    # Revalidation passed against current config/eligibility,
                    # but this process has no worker-bound Planner factory
                    # configured at all (e.g. `load_persistent_config=False`
                    # dev/test wiring with no `planner_factory_for_worker`
                    # supplied) -- fail closed rather than guess.
                    service.fail_claimed_job(
                        claimed, failure_category=JobFailureCategory.ROUTING.value,
                        failure_reason="planner_worker_not_configured",
                    )
                    return
                planner: Planner = planner_factory_for_worker(claimed.worker_id, claimed.job_id)
                service.execute_claimed_job(claimed, planner)
            finally:
                service.close()
        except Exception:
            logger.exception("planning job dispatcher: execution of %s failed", job_id)
        finally:
            with self._lock:
                self._in_flight.discard(job_id)
            self.notify()
