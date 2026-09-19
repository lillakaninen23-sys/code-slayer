"""Canonical durable worker lifecycle transitions (H.3, schema v18).

## What this module is, and is not

This module is the ONE authorized place that turns a request to
archive or reactivate a worker into an actual `workers.lifecycle_state`
write. `store.workers_repo.WorkersRepo` owns only the mechanical
column read/write (`archive_in_transaction()`/`reactivate_in_
transaction()`); this module owns the POLICY around one such
transition: whether it is currently legal, and what durable audit
trail it leaves. Every other caller in this codebase (`api.admin.
AdminFacade`, tests) MUST go through `archive_worker()`/
`reactivate_worker()` here rather than calling `WorkersRepo` directly
for a real administrative transition.

This module does NOT own, and never touches:

- certificates (`worker_baseline_security_certificates`,
  `worker_role_certificates`) — archiving/reactivating never mutates,
  invalidates, or re-derives either. `workers.production_eligibility`
  is the only place lifecycle and certificates are consulted together,
  and even there only to gate a NEW eligibility decision, never to
  change stored evidence.
- trust (`worker_trust_events`) or permissions (`permission_grants`).
- live runtime probing or model execution of any kind — this module
  makes no network call, ever.

## Atomicity boundary (this is the important part)

`archive_worker()`/`reactivate_worker()` open exactly ONE `BEGIN
IMMEDIATE` transaction against the PRODUCTION control-plane connection
they are given, and inside it: reload the worker row, check for
active *ordinary runner* work (`runner_runs`, same DB), write the
lifecycle column, and append the `WORKER_ARCHIVED`/`WORKER_REACTIVATED`
audit event — genuinely atomic; either the whole thing commits or none
of it does.

**Certification Center's active-run check is deliberately NOT part of
this transaction.** `certification_runs` lives in a separate SQLite
database (`store.location.validation_certification_db_path()`) —
this codebase never attempts a distributed transaction across two
SQLite files, and this module does not start now. A caller that wants
to also refuse archiving a worker with an in-flight Certification
Center run (`api.admin.AdminFacade.archive_worker()`, at the time of
writing) performs that check as a SEPARATE, best-effort, non-atomic
precondition BEFORE calling this function — advisory only, and
correctness never depends on it. The real safety net for a
certification run that was QUEUED just before an archive commits is
the execution-time lifecycle recheck `security.certification_service.
CertificationService.execute_claimed_run()`/`_execute_claimed_planner_
run()` perform immediately before any model call — see that module's
own docstring. If the precondition here races and loses, the worst
case is a run sitting briefly in QUEUED against an archived worker;
that run still finishes INCOMPLETE/`worker_archived` without ever
calling a model or minting a certificate.

Likewise, the production `runner_runs` active-work precheck performed
INSIDE this transaction is a genuine, atomic guarantee against a new
*ordinary* run starting concurrently with an archive (both go through
the same production connection's write lock) — but it does not, and
cannot, reach into an already-`RUNNING` turn's in-flight worker/tool
call in another thread or process and stop it. Archive is a
prospective gate on the NEXT boundary that would invoke the worker
(`runner.local_worker_runner.LocalWorkerRunner.start()`/`resume()`),
never a kill switch for one already genuinely in progress — see "Active
ordinary-runner policy" below for exactly which non-terminal states
that precheck refuses on, and why.

## Active ordinary-runner policy

`runner.local_worker_runner.RunStatus`'s five non-terminal values are
NOT treated identically here — mechanically refusing on every one of
them would make a worker with an old `BLOCKED_ON_QUESTIONS` run (which
can legitimately sit for days waiting on a human) or an
`INTERRUPTED_RESUMABLE` run (which this codebase's own dispatcher
never automatically re-executes — see `LocalWorkerRunner.resume()`'s
own "Known limitations") permanently unarchivable. Instead:

- `RUNNING` — refused. A bounded worker turn may be executing right
  now.
- `ANALYZING` — refused. Either a `PromptAnalyst.analyze()` call is in
  flight, or the process crashed between creating this row and
  resolving it — either way this run has not yet reached a state where
  no further worker-adjacent work is expected of it.
- `READY` — refused. This status specifically means "claimed, and the
  very next `resume(adapter=...)` call executes a bounded turn" — the
  definition of pending worker execution.
- `BLOCKED_ON_QUESTIONS` — NOT refused. No worker/model call is
  outstanding right now; the run is dormant, waiting on a human/system
  resolution that may never come. `LocalWorkerRunner.resume()`'s own
  lifecycle recheck (H.3) is what stops this path from reaching
  execution if the worker is later found archived — see that module's
  own docstring.
- `INTERRUPTED_RESUMABLE` — NOT refused, for the same reason: nothing
  automatic ever touches this row again (no lease/liveness sweep
  reclaims it), so leaving it un-refusable-to-archive would be a
  disclosed, permanent gap with no operator remedy.

A terminal run (`COMPLETED`/`FAILED`/`DENIED_TRUST`) is never a reason
to refuse, regardless of when it happened.

## Idempotency

`archive_worker()` on an already-`ARCHIVED` worker, and
`reactivate_worker()` on an already-`ACTIVE` worker, both succeed as a
genuine no-op (`LifecycleTransitionResult.changed is False`) — no
second lifecycle write, no duplicate audit event, and (for archive)
no active-work check is even performed (archiving a worker that is
already archived cannot be blocked by "active work" — there is nothing
new being authorized). This mirrors `store.workers_repo.WorkersRepo.
register()`'s own established idempotent convention."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.store.db import transaction
from code_slayer.store.models import Worker
from code_slayer.store.runner_repo import RunnerRepo
from code_slayer.store.workers_repo import WorkerLifecycleState, WorkersRepo

# See the module docstring's "Active ordinary-runner policy". Raw
# string literals, not an imported `RunStatus` -- `workers` sits below
# `runner` in this codebase's dependency direction (`runner.
# local_worker_runner` already imports from `workers`; the reverse
# would be circular), exactly the same discipline `store.
# certification_runs_repo.ACTIVE_STATES`/`CLAIMABLE_STATES` already use
# for `certification_runs.state` string literals.
_BLOCKING_NON_TERMINAL_RUNNER_STATUSES = frozenset({"ANALYZING", "READY", "RUNNING"})

_ACTOR_ID = "workers.lifecycle"


@dataclass(frozen=True)
class LifecycleTransitionResult:
    """`ok=False` means nothing was written -- `reason` is a stable
    machine-readable code (`unknown_worker`, `worker_has_active_work`).
    `ok=True` covers both a real transition and an idempotent no-op;
    `changed` is what distinguishes them (see the module docstring's
    "Idempotency" section)."""

    ok: bool
    reason: str
    worker: Worker | None
    changed: bool


def _active_runner_block_reason(conn: sqlite3.Connection, worker_id: str) -> str | None:
    """`None` if no ordinary `runner_runs` row for `worker_id` is in a
    status this module treats as genuinely active (see the module
    docstring). Otherwise the blocking run's own status, for a
    caller's diagnostic use only -- never itself the returned refusal
    reason (that is always the stable `worker_has_active_work`)."""
    for run in RunnerRepo(conn).list_for_worker(worker_id):
        if run.status in _BLOCKING_NON_TERMINAL_RUNNER_STATUSES:
            return run.status
    return None


def archive_worker(conn: sqlite3.Connection, *, worker_id: str) -> LifecycleTransitionResult:
    """The ONE authorized way to transition a worker `ACTIVE ->
    ARCHIVED`. Opens its own transaction against `conn` (a PRODUCTION
    control-plane connection) -- never call this from inside another
    already-open transaction on the same connection. See the module
    docstring for the complete atomicity/policy contract."""
    if not isinstance(worker_id, str) or not worker_id:
        return LifecycleTransitionResult(False, "malformed_lifecycle_request", None, False)
    with transaction(conn):
        workers = WorkersRepo(conn)
        current = workers.get(worker_id)
        if current is None:
            return LifecycleTransitionResult(False, "unknown_worker", None, False)
        if current.lifecycle_state == WorkerLifecycleState.ACTIVE:
            blocking = _active_runner_block_reason(conn, worker_id)
            if blocking is not None:
                return LifecycleTransitionResult(
                    False, "worker_has_active_work", current, False,
                )
        updated, changed = workers.archive_in_transaction(worker_id)
        if changed:
            AuditWriter(conn).append(
                task_id=None,
                event_type=EventType.WORKER_ARCHIVED,
                actor_type="system",
                actor_id=_ACTOR_ID,
                payload={
                    "worker_id": worker_id,
                    "from_state": current.lifecycle_state,
                    "to_state": WorkerLifecycleState.ARCHIVED,
                },
            )
        reason = "archived" if changed else "already_archived"
        return LifecycleTransitionResult(True, reason, updated, changed)


def reactivate_worker(conn: sqlite3.Connection, *, worker_id: str) -> LifecycleTransitionResult:
    """The ONE authorized way to transition a worker `ARCHIVED ->
    ACTIVE`. No active-work precondition -- reactivating never starts
    anything by itself (no certificate is issued, no run is resumed
    automatically); it only makes the worker eligible again for
    whatever a NEW request separately chooses to do. Same atomicity/
    idempotency contract as `archive_worker()`."""
    if not isinstance(worker_id, str) or not worker_id:
        return LifecycleTransitionResult(False, "malformed_lifecycle_request", None, False)
    with transaction(conn):
        workers = WorkersRepo(conn)
        current = workers.get(worker_id)
        if current is None:
            return LifecycleTransitionResult(False, "unknown_worker", None, False)
        updated, changed = workers.reactivate_in_transaction(worker_id)
        if changed:
            AuditWriter(conn).append(
                task_id=None,
                event_type=EventType.WORKER_REACTIVATED,
                actor_type="system",
                actor_id=_ACTOR_ID,
                payload={
                    "worker_id": worker_id,
                    "from_state": current.lifecycle_state,
                    "to_state": WorkerLifecycleState.ACTIVE,
                },
            )
        reason = "reactivated" if changed else "already_active"
        return LifecycleTransitionResult(True, reason, updated, changed)
