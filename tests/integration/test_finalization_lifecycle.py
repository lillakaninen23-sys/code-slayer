"""`finalization.lifecycle`: the code-owned `READY_FOR_CHECKPOINT ->
CHECKPOINTED -> COMPLETED` driver, against real temporary Git repos and a
real SQLite state database.

Covers: the full happy path from a real `Finalizer` verification pass
through an automatic checkpoint and a guarded automatic completion; a
checkpoint that does not match the verified tree is never completed; a
task with no verification evidence at all cannot silently reach
`COMPLETED`; stale leases block both the checkpoint and completion steps
without writing anything; crash-adjacent reconciliation and idempotent
re-invocation; a checkpoint policy denial (baseline drift) blocks
checkpoint creation; an unexpected exception during checkpoint creation
is contained, never faked as success; and an explained guard denial for
completion.
"""

from __future__ import annotations

import pytest

from code_slayer.core import InvalidTransition, TaskState, TaskStateMachine
from code_slayer.finalization.lifecycle import (
    CheckpointAdvanceOutcome,
    CompletionAdvanceOutcome,
    advance_checkpointed_completion,
    advance_ready_for_checkpoint,
)
from code_slayer.finalization.service import Finalizer, checkpointed_completion_guard
from code_slayer.finalization.types import FinalizerVerdict
from code_slayer.lease.manager import LeaseHandle
from code_slayer.policy.engine import Decision
from code_slayer.repo import identity
from code_slayer.repo.baseline import InspectionService
from code_slayer.repo.checkpoint import CheckpointManager
from code_slayer.store.checkpoint_repo import CheckpointRepo
from code_slayer.store.db import transaction
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.tool_operations_repo import OperationStatus, ToolOperationsRepo
from code_slayer.tools.executor import ToolExecutor
from code_slayer.tools.models import ToolRequest
from tests.repo_helpers import acquire_lease, commit, git


def _advance(machine, task_id, *pairs):
    for expected, to_state in pairs:
        machine.transition(task_id, expected_state=expected, to_state=to_state, reason="progress")


def _new_task(db_conn, root, blobs_dir, *, pyproject_toml="[tool.ruff]\n"):
    if pyproject_toml is not None:
        (root / "pyproject.toml").write_text(pyproject_toml)
        commit(root, "add verification config")
    info = identity.resolve(root)
    task = TaskRepo(db_conn).create(
        description="lifecycle flow", repo_root=str(root), repo_id=info.repo_id,
        worktree_id=info.worktree_id, config={"tool_policy": {"scope": ["."]}},
    )
    service = InspectionService(db_conn, blobs_dir=blobs_dir)
    service.start(task.task_id)
    service.capture(task.task_id)
    return task


@pytest.fixture
def implementing_context(db_conn, git_repo_with_commit, tmp_path):
    """A task at IMPLEMENTING, ruff configured in baseline -- so a test
    can own real content, verify it for real, and drive it through the
    lifecycle."""
    root = git_repo_with_commit
    blobs_dir = tmp_path / "evidence"
    task = _new_task(db_conn, root, blobs_dir)
    machine = TaskStateMachine(db_conn)
    _advance(machine, task.task_id,
              (TaskState.BASELINED, TaskState.PLANNING),
              (TaskState.PLANNING, TaskState.PLANNED),
              (TaskState.PLANNED, TaskState.IMPLEMENTING))
    lease = acquire_lease(db_conn, task)
    return root, task.task_id, blobs_dir, lease


def own_file(executor, task_id, path, content):
    result = executor.execute(task_id, ToolRequest(tool="create_file", path=path, content=content))
    assert result.status == OperationStatus.SUCCEEDED
    return result


def _reach_ready_for_checkpoint(db_conn, root, task_id, blobs_dir, lease, tmp_dir):
    """Own a clean file, transition to VERIFYING, and run the REAL
    Finalizer to reach READY_FOR_CHECKPOINT -- never a shortcut/bypass."""
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "clean.py", b"VALUE = 1\n")
    TaskStateMachine(db_conn).transition(
        task_id, expected_state=TaskState.IMPLEMENTING, to_state=TaskState.VERIFYING, reason="p",
    )
    decision = Finalizer(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir).decide_after_verification(
        task_id, lease,
    )
    assert decision.verdict == FinalizerVerdict.VERIFIED
    assert TaskRepo(db_conn).get(task_id).state == "READY_FOR_CHECKPOINT"


# --- A. happy path -----------------------------------------------------

def test_a_happy_path_verifying_through_completed(implementing_context, db_conn, tmp_path):
    root, task_id, blobs_dir, lease = implementing_context
    tmp_dir = tmp_path / "tmp"
    _reach_ready_for_checkpoint(db_conn, root, task_id, blobs_dir, lease, tmp_dir)

    checkpoint_result = advance_ready_for_checkpoint(
        db_conn, task_id, lease, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
    )
    assert checkpoint_result.outcome == CheckpointAdvanceOutcome.CREATED
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"
    assert checkpoint_result.checkpoint_result.commit_sha is not None

    completion_result = advance_checkpointed_completion(db_conn, task_id, lease)
    assert completion_result.outcome == CompletionAdvanceOutcome.COMPLETED
    assert TaskRepo(db_conn).get(task_id).state == "COMPLETED"
    # Real Git ref exists, real audit-evidenced tree id.
    ref = f"refs/codeslayer/checkpoints/{task_id}/0"
    assert git(root, "rev-parse", ref) == checkpoint_result.checkpoint_result.commit_sha


# --- B. checkpoint tree != verified tree -> never COMPLETED -------------

def test_b_checkpoint_content_mismatch_never_completes(implementing_context, db_conn, tmp_path):
    root, task_id, blobs_dir, lease = implementing_context
    tmp_dir = tmp_path / "tmp"
    _reach_ready_for_checkpoint(db_conn, root, task_id, blobs_dir, lease, tmp_dir)
    checkpoint_result = advance_ready_for_checkpoint(
        db_conn, task_id, lease, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
    )
    assert checkpoint_result.outcome == CheckpointAdvanceOutcome.CREATED

    # Simulate a race/bug: a *different* VERIFIED record (different
    # content) becomes the most recent unsuperseded one, with no
    # STATE_TRANSITION to IMPLEMENTING in between -- mirrors this
    # repository's own established adversarial-content test technique.
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    from code_slayer.tools import file_tools as files

    TaskStateMachine(db_conn).transition(
        task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.IMPLEMENTING,
        reason="more work",
    )
    executor.execute(task_id, ToolRequest(
        tool="write_file", path="clean.py", content=b"VALUE = 2\n",
        expected_hash=files.digest(b"VALUE = 1\n"),
    ))
    TaskStateMachine(db_conn).transition(
        task_id, expected_state=TaskState.IMPLEMENTING, to_state=TaskState.VERIFYING, reason="p",
    )
    decision_b = Finalizer(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir).decide_after_verification(
        task_id, lease,
    )
    assert decision_b.verdict == FinalizerVerdict.VERIFIED
    assert TaskRepo(db_conn).get(task_id).state == "READY_FOR_CHECKPOINT"

    # Force straight to CHECKPOINTED without ever creating a real
    # checkpoint for content B -- CheckpointRepo.latest() still answers
    # with checkpoint #0 (content A).
    with transaction(db_conn):
        TaskRepo(db_conn)._record_transition_in_transaction(
            task_id, to_state="CHECKPOINTED", to_phase="CHECKPOINTED", reason="simulated race",
        )

    completion_result = advance_checkpointed_completion(db_conn, task_id, lease)
    assert completion_result.outcome == CompletionAdvanceOutcome.DENIED
    assert completion_result.reason == "verification_content_mismatch"
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"


# --- C. no VERIFIED evidence -> cannot silently succeed ------------------

def test_c_no_verification_evidence_blocks_completion(implementing_context, db_conn, tmp_path):
    root, task_id, blobs_dir, lease = implementing_context
    tmp_dir = tmp_path / "tmp"
    machine = TaskStateMachine(db_conn)
    # Reach READY_FOR_CHECKPOINT and CHECKPOINTED without ever running the
    # Finalizer at all.
    _advance(machine, task_id, (TaskState.IMPLEMENTING, TaskState.VERIFYING))
    _advance(machine, task_id,
              (TaskState.VERIFYING, TaskState.REVIEWING),
              (TaskState.REVIEWING, TaskState.READY_FOR_CHECKPOINT))
    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir, lease=lease)
    result = manager.create(task_id)
    assert result.decision == Decision.ALLOW
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"

    completion_result = advance_checkpointed_completion(db_conn, task_id, lease)
    assert completion_result.outcome == CompletionAdvanceOutcome.DENIED
    assert completion_result.reason == "no_verification_evidence"
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"


# --- D/E. stale lease before checkpoint / before completion --------------

def test_d_stale_lease_before_checkpoint_denies_without_writing(
    implementing_context, db_conn, tmp_path,
):
    root, task_id, blobs_dir, lease = implementing_context
    tmp_dir = tmp_path / "tmp"
    _reach_ready_for_checkpoint(db_conn, root, task_id, blobs_dir, lease, tmp_dir)
    stale = LeaseHandle(lease.worktree_id, lease.task_id, "someone-else", "s", 0, "x")

    result = advance_ready_for_checkpoint(
        db_conn, task_id, stale, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
    )
    assert result.outcome == CheckpointAdvanceOutcome.STALE_LEASE
    assert TaskRepo(db_conn).get(task_id).state == "READY_FOR_CHECKPOINT"
    assert CheckpointRepo(db_conn).list_for_task(task_id) == []


def test_e_stale_lease_before_completion_denies_without_writing(
    implementing_context, db_conn, tmp_path,
):
    root, task_id, blobs_dir, lease = implementing_context
    tmp_dir = tmp_path / "tmp"
    _reach_ready_for_checkpoint(db_conn, root, task_id, blobs_dir, lease, tmp_dir)
    advance_ready_for_checkpoint(db_conn, task_id, lease, blobs_dir=blobs_dir, tmp_dir=tmp_dir)
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"
    stale = LeaseHandle(lease.worktree_id, lease.task_id, "someone-else", "s", 0, "x")

    result = advance_checkpointed_completion(db_conn, task_id, stale)
    assert result.outcome == CompletionAdvanceOutcome.STALE_LEASE
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"


# --- F. crash/interruption around checkpoint creation --------------------

def test_f_stuck_checkpoint_operation_reconciles_then_succeeds(
    implementing_context, db_conn, tmp_path,
):
    """Simulates a crash between the durable `STARTED` journal row and any
    Git side effect -- the same evidence-based scenario `repo.checkpoint`'s
    own crash tests exercise via a real subprocess kill; here constructed
    directly (still real SQLite, real Git) to keep this test scoped to
    what `advance_ready_for_checkpoint()` itself must do: delegate to
    `CheckpointManager`'s own reconciliation, never re-implement it."""
    root, task_id, blobs_dir, lease = implementing_context
    tmp_dir = tmp_path / "tmp"
    _reach_ready_for_checkpoint(db_conn, root, task_id, blobs_dir, lease, tmp_dir)

    with transaction(db_conn):
        ToolOperationsRepo(db_conn).start_in_transaction(
            task_id=task_id, worktree_id=lease.worktree_id, worker_id=lease.worker_id,
            worker_session_id=lease.worker_session_id, lease_generation=lease.generation,
            tool_name="checkpoint_create", risk_class="GIT_MUTATION",
            request_hash="deadbeef", target_resource=f"refs/codeslayer/checkpoints/{task_id}/0",
        )

    result = advance_ready_for_checkpoint(
        db_conn, task_id, lease, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
    )
    # The stuck operation reconciles as FAILED (no git ref exists for
    # it) and a fresh, real attempt succeeds in the same call --
    # CheckpointManager.create()'s own existing behavior, unmodified.
    assert result.outcome == CheckpointAdvanceOutcome.CREATED
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"
    assert len(CheckpointRepo(db_conn).list_for_task(task_id)) == 1


# --- G. calling the driver twice is idempotent ---------------------------

def test_g_calling_checkpoint_driver_twice_creates_no_duplicate(
    implementing_context, db_conn, tmp_path,
):
    root, task_id, blobs_dir, lease = implementing_context
    tmp_dir = tmp_path / "tmp"
    _reach_ready_for_checkpoint(db_conn, root, task_id, blobs_dir, lease, tmp_dir)

    first = advance_ready_for_checkpoint(
        db_conn, task_id, lease, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
    )
    assert first.outcome == CheckpointAdvanceOutcome.CREATED
    second = advance_ready_for_checkpoint(
        db_conn, task_id, lease, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
    )
    assert second.outcome == CheckpointAdvanceOutcome.NOT_READY
    assert len(CheckpointRepo(db_conn).list_for_task(task_id)) == 1


# --- H. CHECKPOINTED task on restart -> completion resumes safely --------

def test_h_completion_can_resume_with_a_freshly_acquired_lease(
    implementing_context, db_conn, tmp_path,
):
    """A different (later, "restarted") lease acquisition can still
    complete a task that a prior lease already checkpointed -- completion
    only requires a CURRENTLY valid lease, never the specific one that
    performed the checkpoint."""
    root, task_id, blobs_dir, lease = implementing_context
    tmp_dir = tmp_path / "tmp"
    _reach_ready_for_checkpoint(db_conn, root, task_id, blobs_dir, lease, tmp_dir)
    advance_ready_for_checkpoint(db_conn, task_id, lease, blobs_dir=blobs_dir, tmp_dir=tmp_dir)
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"

    from code_slayer.lease.manager import LeaseManager

    LeaseManager(db_conn).release(lease)
    reacquired = LeaseManager(db_conn).acquire(
        worktree_id=lease.worktree_id, task_id=task_id, worker_id="code-slayer-finalizer",
        worker_session_id="restarted-session",
    )
    assert reacquired.handle is not None

    result = advance_checkpointed_completion(db_conn, task_id, reacquired.handle)
    assert result.outcome == CompletionAdvanceOutcome.COMPLETED
    assert TaskRepo(db_conn).get(task_id).state == "COMPLETED"


# --- I. already COMPLETED task -> clean no-op -----------------------------

def test_i_already_completed_task_is_a_clean_noop(implementing_context, db_conn, tmp_path):
    root, task_id, blobs_dir, lease = implementing_context
    tmp_dir = tmp_path / "tmp"
    _reach_ready_for_checkpoint(db_conn, root, task_id, blobs_dir, lease, tmp_dir)
    advance_ready_for_checkpoint(db_conn, task_id, lease, blobs_dir=blobs_dir, tmp_dir=tmp_dir)
    first = advance_checkpointed_completion(db_conn, task_id, lease)
    assert first.outcome == CompletionAdvanceOutcome.COMPLETED

    second = advance_checkpointed_completion(db_conn, task_id, lease)
    assert second.outcome == CompletionAdvanceOutcome.NOT_READY
    assert TaskRepo(db_conn).get(task_id).state == "COMPLETED"


# --- J. policy denial -> no checkpoint -------------------------------------

def test_j_baseline_drift_denies_checkpoint_creation(implementing_context, db_conn, tmp_path):
    root, task_id, blobs_dir, lease = implementing_context
    tmp_dir = tmp_path / "tmp"
    machine = TaskStateMachine(db_conn)
    _advance(machine, task_id,
              (TaskState.IMPLEMENTING, TaskState.VERIFYING),
              (TaskState.VERIFYING, TaskState.REVIEWING),
              (TaskState.REVIEWING, TaskState.READY_FOR_CHECKPOINT))
    (root / "extra.txt").write_text("external commit")
    commit(root, "external work")

    result = advance_ready_for_checkpoint(
        db_conn, task_id, lease, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
    )
    assert result.outcome == CheckpointAdvanceOutcome.DENIED
    assert result.reason == "baseline_drift_detected"
    assert TaskRepo(db_conn).get(task_id).state == "READY_FOR_CHECKPOINT"
    assert CheckpointRepo(db_conn).list_for_task(task_id) == []


# --- K. checkpoint creation exception -> durable safe state, never COMPLETED

def test_k_checkpoint_manager_exception_is_contained(
    implementing_context, db_conn, tmp_path, monkeypatch,
):
    root, task_id, blobs_dir, lease = implementing_context
    tmp_dir = tmp_path / "tmp"
    _reach_ready_for_checkpoint(db_conn, root, task_id, blobs_dir, lease, tmp_dir)

    def boom(self, task_id):
        raise RuntimeError("simulated checkpoint manager crash")

    monkeypatch.setattr(CheckpointManager, "create", boom)

    result = advance_ready_for_checkpoint(
        db_conn, task_id, lease, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
    )
    assert result.outcome == CheckpointAdvanceOutcome.CONTAINED_EXCEPTION
    assert "RuntimeError" in result.reason
    task = TaskRepo(db_conn).get(task_id)
    assert task.state == "READY_FOR_CHECKPOINT"
    assert task.state != "CHECKPOINTED"
    assert task.state != "COMPLETED"


# --- L. completion guard denial remains explained ------------------------

def test_l_guard_denial_reason_is_reported_unchanged(implementing_context, db_conn, tmp_path):
    root, task_id, blobs_dir, lease = implementing_context
    tmp_dir = tmp_path / "tmp"
    machine = TaskStateMachine(db_conn)
    _advance(machine, task_id,
              (TaskState.IMPLEMENTING, TaskState.VERIFYING),
              (TaskState.VERIFYING, TaskState.REVIEWING),
              (TaskState.REVIEWING, TaskState.READY_FOR_CHECKPOINT))
    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir, lease=lease)
    manager.create(task_id)

    guarded = TaskStateMachine(db_conn, guards=(checkpointed_completion_guard(db_conn),))
    with pytest.raises(InvalidTransition, match="no_checkpoint_evidence|no_verification_evidence"):
        guarded.transition(
            task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.COMPLETED,
            reason="direct attempt", completion_decision=True,
        )
    # And the driver reports the identical reason, never reinterpreting it.
    result = advance_checkpointed_completion(db_conn, task_id, lease)
    assert result.outcome == CompletionAdvanceOutcome.DENIED
    assert result.reason == "no_verification_evidence"


# --- N. no Coder/model mutation tooling introduced -------------------------

def test_n_worker_allowed_tools_unchanged():
    from code_slayer.workers.execution import _ALLOWED_TOOLS

    assert _ALLOWED_TOOLS == ("read_file",)
