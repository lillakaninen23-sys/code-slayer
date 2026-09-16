"""`TaskLifecycleExecutor`: the server-owned background dispatcher that
automatically rediscovers and resumes stranded post-verification task
lifecycle work (`READY_FOR_CHECKPOINT -> CHECKPOINTED -> COMPLETED`) and
stranded `checkpoint_create` operations, after a process/runtime restart
— closing the orchestration gap the restart/resume audit found: the
lifecycle driver functions in `finalization.lifecycle` are individually
restart-safe and idempotent, but nothing previously *called* them again
once a process died. `runner.local_worker_runner.LocalWorkerRunner.
resume()` cannot fill that role — it is client/`run_id`-triggered, and
(by design; see that module's own docstring) short-circuits on a
`RunnerRun.status` that is already terminal, which a mutating job's own
worker turn reaches *before* the finalization pipeline that follows it
even starts. This module owns no client-facing surface at all.

## Modeled directly on `planning.executor.PlanningJobExecutor`

Same shape, deliberately: `start()` runs one immediate discovery pass
before entering a background daemon poll loop (crash/restart recovery,
free); durable database state is the sole authority for what needs
attention, never in-memory bookkeeping from a prior process; every
poll opens its own fresh connection and closes it when done; an
exception anywhere in one tick is contained and logged, never allowed to
stop the dispatcher thread itself; `notify()` is a pure latency
optimization, never required for correctness — the poll loop alone
still finds and resumes every stranded task eventually.

## No second authority

This module owns no checkpoint policy, no completion authority, no Git
plumbing, and no reconciliation logic of its own. Discovery is a plain,
deterministic, read-only SQL query against `tasks.state`/
`tool_operations.status` (Part 1); every actual state change is made by
calling the exact same code this codebase already trusts for it:
`finalization.lifecycle.advance_ready_for_checkpoint()`/
`advance_checkpointed_completion()` for the two task-lifecycle steps,
and `lease.recovery.reconcile_supported()` (in turn
`repo.checkpoint.CheckpointManager.reconcile()`, unchanged) for stranded
`checkpoint_create` operations. This module decides *when* to call
them, nothing more.

## Lease/fencing: never steal, never skip validation

For every discovered task, this module attempts a *fresh*
`LeaseManager.acquire()` for that task's own `worktree_id` before acting
at all. A worktree another valid actor currently owns (an ordinary
worker turn still finishing, or a concurrent dispatcher tick/instance
that got there first) denies the acquisition; this module then skips
that task for this tick, exactly as directed — it never steals, retries
within the same tick, or infers ownership from anything but the fresh
acquisition's own result. `advance_ready_for_checkpoint()`/
`advance_checkpointed_completion()` each independently re-read task
state and revalidate the lease's currency at their own write boundary
(unchanged from the prior slice) — this module deliberately does not
duplicate either check itself; a task whose state changed between this
module's discovery read and the moment the advance function actually
runs is caught there, not here.

The one lease acquired for a task is held across its *entire* safe
progression in this tick — `READY_FOR_CHECKPOINT -> CHECKPOINTED ->
COMPLETED` in one continuous hold when checkpoint creation succeeds —
and released exactly once, in `finally`, mirroring `runner.
local_worker_runner._run_finalizer_best_effort()`'s own discipline
(Part 3).

## Concurrency and idempotency: durable fencing only

Two dispatcher ticks (same process or two process instances) racing on
the same task never both act: exactly one of their `LeaseManager.
acquire()` calls succeeds (SQLite's own write-lock discipline
serializes the attempt; `LeaseManager` denies the loser), and the
underlying `CheckpointManager`/`checkpointed_completion_guard` machinery
this module calls into is itself already idempotent by construction
(see `finalization.lifecycle`'s own module docstring). Nothing here uses
an in-memory "currently processing" set as a safety mechanism — durable
fencing is the only thing a caller may rely on for correctness. (An
in-memory set would only ever be a same-process optimization to avoid a
wasted `acquire()` call; this first version does not even add that,
since the volumes involved make it unnecessary.)

## Scope (this version)

Exactly three kinds of stranded work, and nothing else: tasks in
`READY_FOR_CHECKPOINT`, tasks in `CHECKPOINTED`, and unresolved
`checkpoint_create` operations `lease.recovery` already knows how to
reconcile. No Coder mutation, no Reviewer, no Security, no Repairer, no
certification, no routing, and no generalized "universal scheduler" —
see `docs/ROADMAP.md` for where those belong later. Model mutation
tooling is entirely unaffected: `workers.execution._ALLOWED_TOOLS`
remains `("read_file",)`.

## A known, disclosed scope boundary: one repository/worktree only

Every task/lease/checkpoint this module ever touches lives in exactly
one place: the *primary* repository's own control-plane database
(`store.location.db_path(repo_id, worktree_id)`) — the same connection
`runner.local_worker_runner.LocalWorkerRunner`'s control plane uses for
every run that does not require mutation isolation. A run whose policy
requires mutation isolation gets its own separate job-worktree database
(`repo.job_worktree`); this module does not enumerate or discover those.
This is a real, disclosed limitation, not an oversight — see this
module's own tests and the delivery report's "remaining lifecycle gaps"
section. It is not a regression today: `api.service.ApplicationService.
start()` always calls `LocalWorkerRunner.start(requires_mutation=False)`
(model mutation is not yet enabled anywhere in production), so no task
that this dispatcher cannot see is reachable in production yet. Job
worktrees are a real gap to close before model mutation is *actually*
enabled.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from code_slayer.core import TaskState
from code_slayer.finalization.lifecycle import (
    CheckpointAdvanceOutcome,
    CompletionAdvanceOutcome,
    advance_checkpointed_completion,
    advance_ready_for_checkpoint,
)
from code_slayer.lease import recovery as lease_recovery
from code_slayer.lease.manager import LeaseManager
from code_slayer.policy.engine import Decision
from code_slayer.repo import identity
from code_slayer.store import db as db_module
from code_slayer.store import location
from code_slayer.store.db import utcnow_iso

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_SECONDS = 2.0

# The identity this module's own lease acquisitions record — distinct
# from `runner.local_worker_runner`'s own `"code-slayer-finalizer"`, so
# audit/lease history can always tell which actor advanced a task: the
# worker turn's own best-effort attempt, or this restart-recovery
# dispatcher discovering it later.
WORKER_ID = "code-slayer-lifecycle-dispatcher"


def discover_ready_for_checkpoint(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every task currently `READY_FOR_CHECKPOINT`: `(task_id,
    worktree_id)` pairs, oldest first. A plain, deterministic read of
    durable `tasks.state` — never in-memory ownership, and safe to call
    repeatedly/concurrently: discovery alone changes nothing, and
    whichever caller actually wins the fresh lease acquisition for a
    given worktree is the only one that can act on what it found here."""
    rows = conn.execute(
        "SELECT task_id, worktree_id FROM tasks WHERE state = ? ORDER BY updated_at, task_id",
        (TaskState.READY_FOR_CHECKPOINT.value,),
    ).fetchall()
    return [(row["task_id"], row["worktree_id"]) for row in rows]


def discover_checkpointed(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Every task currently `CHECKPOINTED`: `(task_id, worktree_id)`
    pairs, oldest first. Same discipline as `discover_ready_for_
    checkpoint()` above."""
    rows = conn.execute(
        "SELECT task_id, worktree_id FROM tasks WHERE state = ? ORDER BY updated_at, task_id",
        (TaskState.CHECKPOINTED.value,),
    ).fetchall()
    return [(row["task_id"], row["worktree_id"]) for row in rows]


@dataclass(frozen=True)
class LifecycleDispatchSnapshot:
    """Minimal, in-memory-only observability (Part 12) — enough for a
    future WebUI/operations surface, or a test, to answer "is recovery
    running, and what did it last do", without any metrics
    infrastructure this codebase does not already have. Never durable,
    never authoritative for any decision — purely descriptive."""

    scan_count: int = 0
    last_scan_started_at: str | None = None
    last_scan_completed_at: str | None = None
    last_scan_error: str | None = None
    last_ready_for_checkpoint_discovered: int = 0
    last_checkpointed_discovered: int = 0
    last_reconciled_operations_discovered: int = 0
    last_advanced_task_id: str | None = None
    last_advanced_outcome: str | None = None


class TaskLifecycleExecutor:
    """One persistent, server-owned background dispatcher bound to
    exactly one primary repository — construct once per process
    (`api.service.ApplicationService.__init__`), never once per request.
    See this module's own docstring for the full design."""

    def __init__(
        self, repo_path: Path | str, *, state_root_override: str | Path | None = None,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        # Repository identity and every durable-state path are resolved
        # exactly once, here, at construction -- never re-resolved on
        # each tick. `store.location`'s own resolution falls back to the
        # process-global `$CODESLAYER_STATE_ROOT` env var whenever no
        # explicit `state_root_override` is given; re-resolving on every
        # tick would mean a background thread belonging to one
        # long-lived instance silently starts pointing at a *different*
        # repository's state the moment that env var is later changed
        # (e.g. by another test's fixture) -- exactly `runner.
        # local_worker_runner.LocalWorkerRunner.__init__()`'s own
        # discipline, mirrored here for the same reason.
        primary = identity.resolve(repo_path)
        self._primary = primary
        self._state_root_override = state_root_override
        self._db_path = location.db_path(
            primary.repo_id, primary.worktree_id, override=state_root_override,
        )
        self._blobs_dir = location.blobs_dir(
            primary.repo_id, primary.worktree_id, override=state_root_override,
        )
        self._tmp_dir = location.tmp_dir(
            primary.repo_id, primary.worktree_id, override=state_root_override,
        )
        self._poll_interval = poll_interval_seconds
        self._wakeup = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._dispatcher_thread: threading.Thread | None = None
        self._snapshot = LifecycleDispatchSnapshot()

    def start(self) -> None:
        """Idempotent-in-spirit for this process's own lifetime — call
        once at server startup. Runs one immediate discovery-and-resume
        pass (the actual crash/restart recovery) before the dispatcher
        thread's own poll loop takes over, so a process that restarts
        with stranded `READY_FOR_CHECKPOINT`/`CHECKPOINTED` tasks or
        interrupted `checkpoint_create` operations begins resolving them
        without waiting for the first poll interval to elapse, and
        without any external trigger (`resume()` or otherwise)."""
        if self._dispatcher_thread is not None:
            return
        self._dispatch_once()
        self._dispatcher_thread = threading.Thread(
            target=self._loop, name="lifecycle-dispatcher", daemon=True,
        )
        self._dispatcher_thread.start()

    def notify(self) -> None:
        """Wake the dispatcher promptly instead of waiting for its next
        poll — a latency optimization only, never required for
        correctness; the poll loop alone still discovers and resumes
        every stranded task eventually."""
        self._wakeup.set()

    def stop(self) -> None:
        """Best-effort: signals the dispatcher loop to exit and joins it.
        Never cancels a tick already in progress by force; the daemon
        thread simply is not given more ticks after this returns. An
        abrupt process exit mid-tick is exactly the restart scenario this
        module exists to recover from on the next `start()` — nothing
        here needs to wait for in-flight work to reach a terminal state
        first."""
        self._stop.set()
        self._wakeup.set()
        if self._dispatcher_thread is not None:
            self._dispatcher_thread.join(timeout=5)
            self._dispatcher_thread = None

    def snapshot(self) -> LifecycleDispatchSnapshot:
        """This dispatcher's own last-scan observability state (Part
        12) — never authoritative for anything; read durable `tasks`/
        `checkpoints`/`audit_events` state for that."""
        with self._lock:
            return self._snapshot

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wakeup.wait(timeout=self._poll_interval)
            self._wakeup.clear()
            if self._stop.is_set():
                return
            try:
                self._dispatch_once()
            except Exception:  # noqa: BLE001 -- defense in depth: a bug inside
                # `_dispatch_once()`'s own containment must never be able to
                # silently kill this daemon thread -- every future poll
                # would then simply never run again, with no restart to
                # recover it (this thread's own process is still alive).
                logger.exception("lifecycle dispatcher: uncontained failure in dispatch tick")

    def _dispatch_once(self) -> None:
        """One full discovery-and-resume pass: reconcile any stranded
        `checkpoint_create` operation, then attempt every discovered
        `READY_FOR_CHECKPOINT` and `CHECKPOINTED` task. Never raises —
        every failure mode (connection/migration, discovery itself, one
        task's own advance) is contained and logged; a problem with one
        task never stops the pass for the rest, and a problem with this
        whole tick never stops the next one from being scheduled
        (`_loop()` additionally wraps this whole method as defense in
        depth)."""
        started_at = utcnow_iso()
        try:
            conn = db_module.connect(self._db_path)
        except Exception as exc:  # noqa: BLE001 -- contained; a later tick retries
            logger.exception("lifecycle dispatcher: could not open state database")
            self._record_scan_error(started_at, f"connection_failed:{type(exc).__name__}")
            return

        try:
            try:
                db_module.migrate(conn)
            except Exception as exc:  # noqa: BLE001 -- contained; a later tick retries
                logger.exception("lifecycle dispatcher: schema migration failed")
                self._record_scan_error(started_at, f"migration_failed:{type(exc).__name__}")
                return

            try:
                reconciled = lease_recovery.reconcile_supported(
                    conn, blobs_dir=self._blobs_dir, tmp_dir=self._tmp_dir,
                )
            except Exception:  # noqa: BLE001 -- contained; a later tick retries
                logger.exception("lifecycle dispatcher: checkpoint-operation reconciliation failed")
                reconciled = []

            try:
                ready = discover_ready_for_checkpoint(conn)
                checkpointed = discover_checkpointed(conn)
            except Exception as exc:  # noqa: BLE001 -- contained; a later tick retries
                logger.exception("lifecycle dispatcher: discovery pass failed")
                self._record_scan_error(started_at, f"discovery_failed:{type(exc).__name__}")
                return

            last_task_id: str | None = None
            last_outcome: str | None = None
            for task_id, worktree_id in ready:
                outcome = self._advance_ready_for_checkpoint_task(
                    conn, task_id, worktree_id, blobs_dir=self._blobs_dir, tmp_dir=self._tmp_dir,
                )
                if outcome is not None:
                    last_task_id, last_outcome = task_id, outcome
            for task_id, worktree_id in checkpointed:
                outcome = self._advance_checkpointed_task(conn, task_id, worktree_id)
                if outcome is not None:
                    last_task_id, last_outcome = task_id, outcome

            with self._lock:
                self._snapshot = LifecycleDispatchSnapshot(
                    scan_count=self._snapshot.scan_count + 1,
                    last_scan_started_at=started_at, last_scan_completed_at=utcnow_iso(),
                    last_scan_error=None,
                    last_ready_for_checkpoint_discovered=len(ready),
                    last_checkpointed_discovered=len(checkpointed),
                    last_reconciled_operations_discovered=len(reconciled),
                    last_advanced_task_id=last_task_id, last_advanced_outcome=last_outcome,
                )
        finally:
            conn.close()

    def _record_scan_error(self, started_at: str, error: str) -> None:
        with self._lock:
            self._snapshot = LifecycleDispatchSnapshot(
                scan_count=self._snapshot.scan_count + 1,
                last_scan_started_at=started_at, last_scan_completed_at=utcnow_iso(),
                last_scan_error=error,
                last_ready_for_checkpoint_discovered=0, last_checkpointed_discovered=0,
                last_reconciled_operations_discovered=0,
                last_advanced_task_id=self._snapshot.last_advanced_task_id,
                last_advanced_outcome=self._snapshot.last_advanced_outcome,
            )

    def _advance_ready_for_checkpoint_task(
        self, conn: sqlite3.Connection, task_id: str, worktree_id: str, *,
        blobs_dir: Path, tmp_dir: Path,
    ) -> str | None:
        """Attempt the full safe progression for one discovered
        `READY_FOR_CHECKPOINT` task under one freshly acquired lease,
        held across both steps: checkpoint creation, then (only if that
        succeeds) guarded completion — never a duplicated decision of
        either, purely a call into `finalization.lifecycle`'s existing
        authority. Returns `None` (nothing to report) if another valid
        actor currently holds this worktree's lease — this task is
        simply skipped for this tick, never stolen."""
        leases = LeaseManager(conn)
        acquired = leases.acquire(
            worktree_id=worktree_id, task_id=task_id,
            worker_id=WORKER_ID, worker_session_id=uuid.uuid4().hex,
        )
        if acquired.decision != Decision.ALLOW or acquired.handle is None:
            return None
        handle = acquired.handle
        try:
            checkpoint_result = advance_ready_for_checkpoint(
                conn, task_id, handle, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
            )
            if checkpoint_result.outcome != CheckpointAdvanceOutcome.CREATED:
                return f"checkpoint:{checkpoint_result.outcome.value}"
            completion_result = advance_checkpointed_completion(conn, task_id, handle)
            return f"completion:{completion_result.outcome.value}"
        except Exception:  # noqa: BLE001 -- contained; a later tick retries
            logger.exception(
                "lifecycle dispatcher: unexpected failure advancing task %s from "
                "READY_FOR_CHECKPOINT", task_id,
            )
            return "checkpoint:CONTAINED_EXCEPTION"
        finally:
            leases.release(handle)

    def _advance_checkpointed_task(
        self, conn: sqlite3.Connection, task_id: str, worktree_id: str,
    ) -> str | None:
        """Attempt guarded completion for one discovered `CHECKPOINTED`
        task under a freshly acquired lease — purely a call into
        `finalization.lifecycle.advance_checkpointed_completion()`, whose
        own `checkpointed_completion_guard()` remains sole authority.
        Returns `None` if another valid actor currently holds this
        worktree's lease."""
        leases = LeaseManager(conn)
        acquired = leases.acquire(
            worktree_id=worktree_id, task_id=task_id,
            worker_id=WORKER_ID, worker_session_id=uuid.uuid4().hex,
        )
        if acquired.decision != Decision.ALLOW or acquired.handle is None:
            return None
        handle = acquired.handle
        try:
            result = advance_checkpointed_completion(conn, task_id, handle)
            return f"completion:{result.outcome.value}"
        except Exception:  # noqa: BLE001 -- contained; a later tick retries
            logger.exception(
                "lifecycle dispatcher: unexpected failure advancing task %s from "
                "CHECKPOINTED", task_id,
            )
            return "completion:CONTAINED_EXCEPTION"
        finally:
            leases.release(handle)


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "WORKER_ID",
    "CompletionAdvanceOutcome",
    "LifecycleDispatchSnapshot",
    "TaskLifecycleExecutor",
    "discover_checkpointed",
    "discover_ready_for_checkpoint",
]
