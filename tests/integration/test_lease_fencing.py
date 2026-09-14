"""Fencing integration: every Phase 4/5 mutation path refuses a stale
lease, a current lease still works normally, and a takeover racing an
in-flight operation is handled deterministically and safely."""

from __future__ import annotations

import hashlib

import pytest

from code_slayer.core import TaskState, TaskStateMachine
from code_slayer.lease.manager import LeaseManager
from code_slayer.policy.engine import Decision
from code_slayer.repo import checkpoint_git as cg
from code_slayer.repo import identity
from code_slayer.repo.baseline import InspectionService
from code_slayer.repo.checkpoint import CheckpointManager
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.tool_operations_repo import OperationStatus
from code_slayer.tools import command_tools
from code_slayer.tools.executor import ToolExecutor
from code_slayer.tools.models import CommandRequest, ToolRequest
from tests.repo_helpers import acquire_lease, git


def _advance(machine, task_id, *pairs):
    for expected, to_state in pairs:
        machine.transition(task_id, expected_state=expected, to_state=to_state, reason="progress")


def _takeover(conn, task, original_lease, *, worker_id="taker", worker_session_id="t"):
    """Release the original session's lease (simulating it having given
    up or been declared gone) and have a different session acquire —
    a genuine takeover with a real, strictly-higher generation, making
    `original_lease` permanently stale."""
    manager = LeaseManager(conn)
    released = manager.release(original_lease)
    assert released.decision == Decision.ALLOW, released.reason
    result = manager.acquire(
        worktree_id=task.worktree_id, task_id=task.task_id,
        worker_id=worker_id, worker_session_id=worker_session_id,
    )
    assert result.handle is not None, result.reason
    return result.handle


@pytest.fixture
def ready(db_conn, git_repo_with_commit, tmp_path):
    """A task at IMPLEMENTING, plus the lease manager and the fixture's
    own current lease handle, ready for a file mutation or checkpoint."""
    root = git_repo_with_commit
    info = identity.resolve(root)
    blobs_dir = tmp_path / "evidence"
    tmp_dir = tmp_path / "tmp"
    task = TaskRepo(db_conn).create(
        description="fencing", repo_root=str(root), repo_id=info.repo_id,
        worktree_id=info.worktree_id, config={"tool_policy": {"scope": ["."]}},
    )
    service = InspectionService(db_conn, blobs_dir=blobs_dir)
    service.start(task.task_id)
    service.capture(task.task_id)
    machine = TaskStateMachine(db_conn)
    _advance(machine, task.task_id,
              (TaskState.BASELINED, TaskState.PLANNING),
              (TaskState.PLANNING, TaskState.PLANNED),
              (TaskState.PLANNED, TaskState.IMPLEMENTING))
    lease = acquire_lease(db_conn, task)
    return root, task, lease, blobs_dir, tmp_dir


# --- stale lease denies every mutation path ---------------------------

def test_stale_lease_denies_create_file(ready, db_conn):
    root, task, lease, blobs_dir, _tmp = ready
    _takeover(db_conn, task, lease)  # a different session takes over
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)  # now-stale
    result = executor.execute(
        task.task_id, ToolRequest(tool="create_file", path="new.txt", content=b"x"),
    )
    assert result.decision == Decision.DENY
    assert result.reason == "stale_fencing_token"
    assert not (root / "new.txt").exists()


def test_stale_lease_denies_write_file(ready, db_conn):
    root, task, lease, blobs_dir, _tmp = ready
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    created = executor.execute(
        task.task_id, ToolRequest(tool="create_file", path="owned.txt", content=b"v1"),
    )
    assert created.status == OperationStatus.SUCCEEDED
    _takeover(db_conn, task, lease)
    result = executor.execute(task.task_id, ToolRequest(
        tool="write_file", path="owned.txt", content=b"v2",
        expected_hash=hashlib.sha256(b"v1").hexdigest(),
    ))
    assert result.decision == Decision.DENY
    assert result.reason == "stale_fencing_token"
    assert (root / "owned.txt").read_bytes() == b"v1"


def test_stale_lease_denies_run_command(ready, db_conn):
    _root, task, lease, blobs_dir, _tmp = ready
    _takeover(db_conn, task, lease)
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    result = executor.execute(task.task_id, ToolRequest(
        tool="run_command",
        command=CommandRequest(profile="git_rev_parse", executable="git", argv=("HEAD",)),
    ))
    assert result.decision == Decision.DENY
    assert result.reason == "stale_fencing_token"


def test_current_lease_still_succeeds_normally(ready, db_conn):
    root, task, lease, blobs_dir, _tmp = ready
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    result = executor.execute(
        task.task_id, ToolRequest(tool="create_file", path="ok.txt", content=b"x"),
    )
    assert result.status == OperationStatus.SUCCEEDED
    assert (root / "ok.txt").read_bytes() == b"x"


def test_stale_lease_denies_checkpoint_create(ready, db_conn):
    root, task, lease, blobs_dir, tmp_dir = ready
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    executor.execute(task.task_id, ToolRequest(tool="create_file", path="owned.txt", content=b"x"))
    machine = TaskStateMachine(db_conn)
    _advance(machine, task.task_id,
              (TaskState.IMPLEMENTING, TaskState.VERIFYING),
              (TaskState.VERIFYING, TaskState.REVIEWING),
              (TaskState.REVIEWING, TaskState.READY_FOR_CHECKPOINT))
    _takeover(db_conn, task, lease)
    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir, lease=lease)
    result = manager.create(task.task_id)
    assert result.decision == Decision.DENY
    assert result.reason == "stale_fencing_token"
    ref = f"refs/codeslayer/checkpoints/{task.task_id}/0"
    assert cg.resolve_ref(ref, cwd=root) is None


def test_checkpoint_create_with_no_lease_denies(ready, db_conn):
    _root, task, _lease, blobs_dir, tmp_dir = ready
    machine = TaskStateMachine(db_conn)
    _advance(machine, task.task_id,
              (TaskState.IMPLEMENTING, TaskState.VERIFYING),
              (TaskState.VERIFYING, TaskState.REVIEWING),
              (TaskState.REVIEWING, TaskState.READY_FOR_CHECKPOINT))
    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir)  # no lease at all
    result = manager.create(task.task_id)
    assert result.decision == Decision.DENY
    assert result.reason == "stale_fencing_token"


def test_stale_caller_cannot_trigger_automatic_reconciliation(ready, db_conn):
    """A caller with no valid lease must not get to trigger create()'s
    automatic reconciliation of a *different*, still-current session's
    pending operation as a side effect of merely calling create() — only
    an authoritative caller (or the explicit reconcile() escape hatch)
    may assert that a pending operation's owner is truly gone."""
    from code_slayer.store.db import transaction
    from code_slayer.store.tool_operations_repo import ToolOperationsRepo

    root, task, lease, blobs_dir, tmp_dir = ready
    with transaction(db_conn):
        ToolOperationsRepo(db_conn).start_in_transaction(
            task_id=task.task_id, worktree_id=task.worktree_id,
            worker_id=lease.worker_id, worker_session_id=lease.worker_session_id,
            lease_generation=lease.generation, tool_name="checkpoint_create",
            risk_class="GIT_MUTATION", request_hash="deadbeef",
            target_resource=f"refs/codeslayer/checkpoints/{task.task_id}/0",
        )
    stale_manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir)  # no lease
    result = stale_manager.create(task.task_id)

    assert result.decision == Decision.DENY
    assert result.reason == "stale_fencing_token"
    # The pending operation belonging to the still-current lease was
    # never touched by the unauthenticated caller's create() attempt.
    op = db_conn.execute(
        "SELECT status FROM tool_operations WHERE task_id = ? AND tool_name = 'checkpoint_create'",
        (task.task_id,),
    ).fetchone()
    assert op["status"] == OperationStatus.STARTED


# --- takeover racing an in-flight operation --------------------------------

def test_takeover_between_initial_check_and_mutation_denies_write(ready, db_conn, monkeypatch):
    """A takeover happening *after* the initial policy pass but *before*
    the actual filesystem mutation must still be caught — this is exactly
    why fencing needs its own recheck at the deepest mutation boundary,
    not just an initial check (IMPLEMENTING alone does not prove
    ownership; §11/§12)."""
    root, task, lease, blobs_dir, _tmp = ready
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    created = executor.execute(
        task.task_id, ToolRequest(tool="create_file", path="owned.txt", content=b"v1"),
    )
    assert created.status == OperationStatus.SUCCEEDED

    real_verify = command_tools.CommandRunner.verify_identity

    def verify_then_takeover(self, *a, **kw):
        result = real_verify(self, *a, **kw)
        _takeover(db_conn, task, lease)
        return result

    monkeypatch.setattr(command_tools.CommandRunner, "verify_identity", verify_then_takeover)
    result = executor.execute(task.task_id, ToolRequest(
        tool="write_file", path="owned.txt", content=b"v2",
        expected_hash=hashlib.sha256(b"v1").hexdigest(),
    ))
    assert result.status == OperationStatus.FAILED
    assert (root / "owned.txt").read_bytes() == b"v1"


def test_checkpoint_takeover_during_git_work_still_finalizes_from_evidence(
    ready, db_conn, monkeypatch,
):
    """Checkpoint truth is Git-ref evidence, not lease state (§14): a
    takeover that happens *after* a checkpoint attempt already validly
    started must not erase or block recording what actually got
    committed — the state machine's own expected-state guard is what
    keeps this safe against a genuinely conflicting concurrent session."""
    root, task, lease, blobs_dir, tmp_dir = ready
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    executor.execute(task.task_id, ToolRequest(tool="create_file", path="owned.txt", content=b"x"))
    machine = TaskStateMachine(db_conn)
    _advance(machine, task.task_id,
              (TaskState.IMPLEMENTING, TaskState.VERIFYING),
              (TaskState.VERIFYING, TaskState.REVIEWING),
              (TaskState.REVIEWING, TaskState.READY_FOR_CHECKPOINT))
    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir, lease=lease)

    real_commit_tree = cg.commit_tree

    def commit_then_takeover(*a, **kw):
        sha = real_commit_tree(*a, **kw)
        # A takeover races in while this attempt's git work is still
        # running (its own STARTED row is already durably committed).
        _takeover(db_conn, task, lease)
        return sha

    monkeypatch.setattr(cg, "commit_tree", commit_then_takeover)
    result = manager.create(task.task_id)

    assert result.operation_status == OperationStatus.SUCCEEDED
    assert TaskRepo(db_conn).get(task.task_id).state == "CHECKPOINTED"
    ref = f"refs/codeslayer/checkpoints/{task.task_id}/0"
    assert git(root, "rev-parse", ref) == result.commit_sha
