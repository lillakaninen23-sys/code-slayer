"""Code-owned task-lifecycle orchestration: `READY_FOR_CHECKPOINT ->
CHECKPOINTED -> COMPLETED`.

This module owns no checkpoint logic and no completion-authority logic
of its own — it exists purely to decide *whether* to call
`repo.checkpoint.CheckpointManager.create()` / attempt the
`CHECKPOINTED -> COMPLETED` transition, and to report what those
existing, already-tested authorities actually decided. It is the single
remaining piece needed to close the deterministic task lifecycle before
any model is ever given a real mutation tool.

## No second checkpoint subsystem

`advance_ready_for_checkpoint()` is a thin wrapper around
`CheckpointManager.create()` — the exact same class every existing
test, and the exact same class a future `READY_FOR_CHECKPOINT` job would
use if driven manually. It performs no Git plumbing, no policy
evaluation, and no evidence validation of its own; `CheckpointManager`'s
own crash-safety (`STARTED` committed before any Git side effect,
`reconcile()`'s ref-existence-is-truth recovery) is inherited unchanged,
including its own idempotency: calling `create()` again for an
already-`CHECKPOINTED` task denies with `wrong_task_state`, and calling
it again after a crash first resolves the prior unfinished attempt
before ever starting a new one — so calling `advance_ready_for_
checkpoint()` twice, or after a restart, is always safe by construction,
never a duplicate checkpoint.

## The completion guard remains sole authority

`advance_checkpointed_completion()` does not re-implement, weaken, or
duplicate `finalization.service.checkpointed_completion_guard()`'s
decision in any way. It performs exactly the fencing check every other
lease-consuming module in this codebase already performs before
attempting a write (`LeaseManager.is_current()`, revalidated fresh
inside the same write transaction as the attempted transition — the
same "revalidate at the deepest practical boundary" discipline
`CheckpointManager`/`Finalizer` already use), then asks the real
`core.state_machine.TaskStateMachine`, with the real guard attached, to
attempt the transition. Whatever the guard decides — `FINAL` (allow) or
any of its `BLOCKED` reasons (`no_checkpoint_evidence`,
`no_verification_evidence`, `verification_content_mismatch`,
`unresolved_operations`) — is reported back unchanged; this module adds
no new veto reason and overrides none of the guard's own.

## No new TaskState

Both functions operate entirely over the existing `READY_FOR_CHECKPOINT`
/`CHECKPOINTED`/`COMPLETED` states and the existing `BLOCKED` state (via
`Finalizer`'s own verification-stage handling, unaffected by this
module) — nothing here adds to `core.states.TaskState`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from code_slayer.core import TaskState, TaskStateMachine
from code_slayer.core.transitions import InvalidTransition, StaleTaskState, TransitionRequest
from code_slayer.finalization.service import checkpointed_completion_guard
from code_slayer.lease.manager import LeaseHandle, LeaseManager
from code_slayer.policy.engine import Decision
from code_slayer.repo.checkpoint import CheckpointManager, CheckpointResult
from code_slayer.store.db import transaction
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.tool_operations_repo import OperationStatus


class CheckpointAdvanceOutcome(StrEnum):
    """What `advance_ready_for_checkpoint()` actually did. `CREATED`
    is the only outcome under which a real, durable checkpoint was
    confirmed to now exist."""

    CREATED = "CREATED"
    DENIED = "DENIED"
    FAILED = "FAILED"
    NOT_READY = "NOT_READY"
    STALE_LEASE = "STALE_LEASE"
    UNKNOWN = "UNKNOWN"
    CONTAINED_EXCEPTION = "CONTAINED_EXCEPTION"


@dataclass(frozen=True)
class CheckpointAdvanceResult:
    outcome: CheckpointAdvanceOutcome
    reason: str
    checkpoint_result: CheckpointResult | None = None


class CompletionAdvanceOutcome(StrEnum):
    """What `advance_checkpointed_completion()` actually did."""

    COMPLETED = "COMPLETED"
    DENIED = "DENIED"
    NOT_READY = "NOT_READY"
    STALE_LEASE = "STALE_LEASE"
    CONTAINED_EXCEPTION = "CONTAINED_EXCEPTION"


@dataclass(frozen=True)
class CompletionAdvanceResult:
    outcome: CompletionAdvanceOutcome
    reason: str


def advance_ready_for_checkpoint(
    conn: sqlite3.Connection, task_id: str, lease: LeaseHandle, *,
    blobs_dir: Path | str, tmp_dir: Path | str,
) -> CheckpointAdvanceResult:
    """Attempt `READY_FOR_CHECKPOINT -> CHECKPOINTED` for `task_id` by
    calling the real `CheckpointManager.create()` — never a second
    checkpoint implementation. Fails closed (no checkpoint attempted at
    all) on a malformed/stale lease or a task that is not currently
    `READY_FOR_CHECKPOINT`; contains (never re-raises) any unexpected
    exception, reporting it as `CONTAINED_EXCEPTION` rather than ever
    claiming a checkpoint was created when it was not."""
    leases = LeaseManager(conn)
    if not isinstance(lease, LeaseHandle):
        return CheckpointAdvanceResult(
            CheckpointAdvanceOutcome.STALE_LEASE, "malformed_lease_handle",
        )
    if not leases.is_current(lease):
        return CheckpointAdvanceResult(CheckpointAdvanceOutcome.STALE_LEASE, "stale_fencing_token")
    try:
        task = TaskRepo(conn).get(task_id)
    except Exception as exc:  # noqa: BLE001 -- contained, reported, never re-raised
        return CheckpointAdvanceResult(
            CheckpointAdvanceOutcome.CONTAINED_EXCEPTION,
            f"task_lookup_failed:{type(exc).__name__}",
        )
    if task.state != TaskState.READY_FOR_CHECKPOINT.value:
        # Not an error: idempotency requirement B/C (already CHECKPOINTED/
        # COMPLETED, or not there yet) -- CheckpointManager.create() would
        # independently reach the same conclusion via its own
        # `wrong_task_state` policy denial, but checking first avoids
        # constructing a checkpoint attempt (and its own STARTED-lease
        # revalidation cost) for a task that plainly is not eligible.
        return CheckpointAdvanceResult(
            CheckpointAdvanceOutcome.NOT_READY, f"task_state_is_{task.state}",
        )
    try:
        manager = CheckpointManager(conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir, lease=lease)
        result = manager.create(task_id)
    except Exception as exc:  # noqa: BLE001 -- contained, reported, never re-raised
        return CheckpointAdvanceResult(
            CheckpointAdvanceOutcome.CONTAINED_EXCEPTION,
            f"checkpoint_manager_exception:{type(exc).__name__}",
        )
    if result.decision != Decision.ALLOW:
        return CheckpointAdvanceResult(CheckpointAdvanceOutcome.DENIED, result.reason, result)
    if result.operation_status == OperationStatus.SUCCEEDED:
        return CheckpointAdvanceResult(CheckpointAdvanceOutcome.CREATED, result.reason, result)
    if result.operation_status == OperationStatus.FAILED:
        return CheckpointAdvanceResult(CheckpointAdvanceOutcome.FAILED, result.reason, result)
    # UNKNOWN (or no operation_status at all): genuinely uncertain,
    # exactly CheckpointManager's own crash-adjacent classification --
    # never guessed as CREATED. A later call to this same function (or a
    # direct `CheckpointManager.reconcile()`) resolves it from durable
    # Git-ref evidence, unchanged.
    return CheckpointAdvanceResult(CheckpointAdvanceOutcome.UNKNOWN, result.reason, result)


def advance_checkpointed_completion(
    conn: sqlite3.Connection, task_id: str, lease: LeaseHandle,
) -> CompletionAdvanceResult:
    """Attempt `CHECKPOINTED -> COMPLETED` for `task_id` through the real
    `TaskStateMachine`, gated exclusively by the real
    `checkpointed_completion_guard()` — this function makes no
    completion decision of its own. Fails closed on a malformed/stale
    lease (revalidated fresh, inside the same write transaction as the
    attempted transition) or a task that is not currently `CHECKPOINTED`
    (idempotency requirement C/H: already `COMPLETED`, or not there yet,
    is a clean no-op, never an error). A guard denial is reported with
    its own exact reason, never reinterpreted; an unexpected exception is
    contained, never re-raised, and never mistaken for a completion."""
    leases = LeaseManager(conn)
    if not isinstance(lease, LeaseHandle):
        return CompletionAdvanceResult(
            CompletionAdvanceOutcome.STALE_LEASE, "malformed_lease_handle",
        )
    machine = TaskStateMachine(conn, guards=(checkpointed_completion_guard(conn),))
    try:
        with transaction(conn):
            if not leases.is_current(lease):
                return CompletionAdvanceResult(
                    CompletionAdvanceOutcome.STALE_LEASE, "stale_fencing_token",
                )
            task = TaskRepo(conn).get(task_id)
            if task.state != TaskState.CHECKPOINTED.value:
                return CompletionAdvanceResult(
                    CompletionAdvanceOutcome.NOT_READY, f"task_state_is_{task.state}",
                )
            machine.transition_in_transaction(
                task_id, request=TransitionRequest(
                    expected_state=TaskState.CHECKPOINTED, to_state=TaskState.COMPLETED,
                    reason="lifecycle:automatic_completion", completion_decision=True,
                ), actor_id="code-slayer-lifecycle",
            )
    except InvalidTransition as exc:
        # The guard's own veto (or, in principle, a graph-level denial) --
        # its exact reason is reported unchanged, never reinterpreted.
        message = str(exc)
        reason = (
            message.split("finalization_guard:", 1)[1]
            if "finalization_guard:" in message else message
        )
        return CompletionAdvanceResult(CompletionAdvanceOutcome.DENIED, reason)
    except StaleTaskState:
        # The task moved to something else between our own pre-check and
        # the state machine's fresh re-read under the same write lock --
        # structurally not reachable in practice (both reads happen under
        # one already-open transaction), kept only as defense in depth.
        return CompletionAdvanceResult(
            CompletionAdvanceOutcome.NOT_READY, "task_state_changed_concurrently",
        )
    except Exception as exc:  # noqa: BLE001 -- contained, reported, never re-raised
        return CompletionAdvanceResult(
            CompletionAdvanceOutcome.CONTAINED_EXCEPTION,
            f"unexpected_exception:{type(exc).__name__}",
        )
    return CompletionAdvanceResult(
        CompletionAdvanceOutcome.COMPLETED, "checkpoint_and_verification_evidence_confirmed",
    )
