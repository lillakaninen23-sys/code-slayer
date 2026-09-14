"""Generic unresolved-operation recovery: discovery, epoch staleness
annotation, and dispatch-only-where-a-reconciler-exists."""

from __future__ import annotations

import pytest

from code_slayer.lease.manager import LeaseManager
from code_slayer.lease.recovery import discover_unresolved, reconcile_supported
from code_slayer.repo import identity
from code_slayer.repo.baseline import InspectionService
from code_slayer.store.db import transaction
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.tool_operations_repo import OperationStatus, ToolOperationsRepo


@pytest.fixture
def task(db_conn, git_repo_with_commit):
    info = identity.resolve(git_repo_with_commit)
    return TaskRepo(db_conn).create(
        description="d", repo_root=str(git_repo_with_commit),
        repo_id=info.repo_id, worktree_id=info.worktree_id,
    )


def start_operation(conn, task, *, tool_name, generation, target_resource="some/path.txt"):
    with transaction(conn):
        return ToolOperationsRepo(conn).start_in_transaction(
            task_id=task.task_id, worktree_id=task.worktree_id, worker_id="w",
            worker_session_id="s", lease_generation=generation, tool_name=tool_name,
            risk_class="WRITE_OWNED", request_hash="deadbeef", target_resource=target_resource,
        )


# --- discover_unresolved -----------------------------------------------

def test_discover_unresolved_empty_when_nothing_pending(db_conn, task):
    assert discover_unresolved(db_conn) == []
    assert discover_unresolved(db_conn, task_id=task.task_id) == []


def test_discover_unresolved_finds_started_row_with_current_epoch(db_conn, task):
    LeaseManager(db_conn).acquire(
        worktree_id=task.worktree_id, task_id=task.task_id, worker_id="w", worker_session_id="s",
    )
    op = start_operation(db_conn, task, tool_name="write_file", generation=1)
    found = discover_unresolved(db_conn, task_id=task.task_id)
    assert len(found) == 1
    assert found[0].operation_id == op.operation_id
    assert found[0].status == OperationStatus.STARTED
    assert found[0].lease_generation == 1
    assert found[0].current_lease_generation == 1
    assert not found[0].stale_epoch
    assert not found[0].has_reconciler


def test_discover_unresolved_marks_stale_epoch_after_takeover(db_conn, task):
    manager = LeaseManager(db_conn)
    manager.acquire(
        worktree_id=task.worktree_id, task_id=task.task_id, worker_id="a", worker_session_id="sa",
    )
    op = start_operation(db_conn, task, tool_name="write_file", generation=1)
    # A takeover: release, then a new session acquires, incrementing the epoch.
    from code_slayer.store.lease_repo import LeaseRepo

    current = LeaseRepo(db_conn).get(task.worktree_id)
    manager.release(_handle_from_row(current))
    manager.acquire(
        worktree_id=task.worktree_id, task_id=task.task_id, worker_id="b", worker_session_id="sb",
    )
    found = discover_unresolved(db_conn, task_id=task.task_id)
    assert found[0].operation_id == op.operation_id
    assert found[0].lease_generation == 1
    assert found[0].current_lease_generation == 2
    assert found[0].stale_epoch


def test_discover_unresolved_unknown_generation_is_not_marked_stale(db_conn, task):
    # No lease ever acquired for this worktree: current_lease_generation
    # is None, and a legacy/never-fenced operation's own generation is
    # also None -- neither side is comparable, so this must not be
    # reported as "stale" (that would be a guess, not evidence).
    op = start_operation(db_conn, task, tool_name="write_file", generation=None)
    found = discover_unresolved(db_conn, task_id=task.task_id)
    assert found[0].operation_id == op.operation_id
    assert found[0].lease_generation is None
    assert found[0].current_lease_generation is None
    assert not found[0].stale_epoch


def _handle_from_row(row):
    from code_slayer.lease.manager import LeaseHandle

    return LeaseHandle(
        row.worktree_id, row.task_id, row.worker_id, row.worker_session_id,
        row.generation, row.acquired_at,
    )


# --- reconcile_supported: dispatch only where a reconciler exists ----------

def test_reconcile_supported_leaves_unsupported_tool_unresolved(db_conn, task, tmp_path):
    op = start_operation(db_conn, task, tool_name="write_file", generation=1)
    outcomes = reconcile_supported(
        db_conn, blobs_dir=tmp_path / "blobs", tmp_dir=tmp_path / "tmp", task_id=task.task_id,
    )
    assert len(outcomes) == 1
    operation, result = outcomes[0]
    assert operation.operation_id == op.operation_id
    assert not operation.has_reconciler
    assert result is None
    reloaded = ToolOperationsRepo(db_conn).get(op.operation_id)
    assert reloaded.status == OperationStatus.STARTED  # untouched, fails closed


def test_reconcile_supported_dispatches_checkpoint_create_ref_absent_as_failed(
    db_conn, task, tmp_path,
):
    blobs_dir = tmp_path / "blobs"
    service = InspectionService(db_conn, blobs_dir=blobs_dir)
    service.start(task.task_id)
    service.capture(task.task_id)
    op = start_operation(
        db_conn, task, tool_name="checkpoint_create", generation=1,
        target_resource=f"refs/codeslayer/checkpoints/{task.task_id}/0",
    )
    outcomes = reconcile_supported(
        db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_path / "tmp", task_id=task.task_id,
    )
    assert len(outcomes) == 1
    operation, result = outcomes[0]
    assert operation.has_reconciler
    assert result is None  # the ref never existed: resolved FAILED, nothing to finalize
    reloaded = ToolOperationsRepo(db_conn).get(op.operation_id)
    assert reloaded.status == OperationStatus.FAILED


def test_discover_unresolved_scans_all_tasks_when_unscoped(db_conn, git_repo_with_commit):
    info = identity.resolve(git_repo_with_commit)
    task_a = TaskRepo(db_conn).create(
        description="a", repo_root=str(git_repo_with_commit), repo_id=info.repo_id,
        worktree_id="wt-a",
    )
    task_b = TaskRepo(db_conn).create(
        description="b", repo_root=str(git_repo_with_commit), repo_id=info.repo_id,
        worktree_id="wt-b",
    )
    start_operation(db_conn, task_a, tool_name="write_file", generation=1)
    start_operation(db_conn, task_b, tool_name="run_command", generation=1)
    found = discover_unresolved(db_conn)
    assert {o.task_id for o in found} == {task_a.task_id, task_b.task_id}
