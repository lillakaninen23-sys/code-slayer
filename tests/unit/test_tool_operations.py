"""Durable tool-operation journal."""

from __future__ import annotations

import pytest

from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.tool_operations_repo import (
    OperationStatus,
    ToolOperationsRepo,
    compute_request_hash,
)


@pytest.fixture
def task_id(db_conn) -> str:
    task = TaskRepo(db_conn).create(
        description="d", repo_root="/r", repo_id="repo-1", worktree_id="wt-1"
    )
    return task.task_id


@pytest.fixture
def repo(db_conn) -> ToolOperationsRepo:
    return ToolOperationsRepo(db_conn)


def test_started_persists_durably(repo, task_id, tmp_path, db_conn):
    from code_slayer.store import db as db_module

    op = repo.start(
        task_id=task_id,
        worktree_id="wt-1",
        worker_id="fake-worker",
        worker_session_id="sess-1",
        tool_name="fs.write",
        risk_class="WRITE",
        request_hash=compute_request_hash("fs.write", {"path": "a.txt"}),
        target_resource="a.txt",
    )
    assert op.status == OperationStatus.STARTED

    # Simulate a fresh process reopening the same database.
    db_conn.close()
    reopened = db_module.connect(tmp_path / "state.db")
    row = reopened.execute(
        "SELECT * FROM tool_operations WHERE operation_id = ?", (op.operation_id,)
    ).fetchone()
    reopened.close()
    assert row is not None
    assert row["status"] == OperationStatus.STARTED


def test_started_to_succeeded(repo, task_id):
    op = repo.start(
        task_id=task_id, worktree_id="wt-1", worker_id="w", worker_session_id="s",
        tool_name="fs.write", risk_class="WRITE",
        request_hash=compute_request_hash("fs.write", {}), target_resource="a.txt",
    )
    finished = repo.finish(op.operation_id, status=OperationStatus.SUCCEEDED, after_evidence="h2")
    assert finished.status == OperationStatus.SUCCEEDED
    assert finished.finished_at is not None
    assert finished.after_evidence == "h2"


def test_started_to_failed(repo, task_id):
    op = repo.start(
        task_id=task_id, worktree_id="wt-1", worker_id="w", worker_session_id="s",
        tool_name="fs.write", risk_class="WRITE",
        request_hash=compute_request_hash("fs.write", {}), target_resource="a.txt",
    )
    finished = repo.finish(
        op.operation_id, status=OperationStatus.FAILED, result={"error": "denied"}
    )
    assert finished.status == OperationStatus.FAILED
    assert finished.result_json is not None
    assert "denied" in finished.result_json


def test_unresolved_started_can_be_classified_and_reloaded(repo, task_id):
    op = repo.start(
        task_id=task_id, worktree_id="wt-1", worker_id="w", worker_session_id="s",
        tool_name="git.commit_tree", risk_class="GIT_MUTATION",
        request_hash=compute_request_hash("git.commit_tree", {}), target_resource="refs/x",
    )
    unresolved = repo.list_unresolved(task_id=task_id)
    assert [o.operation_id for o in unresolved] == [op.operation_id]

    marked = repo.mark_unknown(op.operation_id)
    assert marked.status == OperationStatus.UNKNOWN
    still_unresolved = repo.list_unresolved(task_id=task_id)
    assert [o.operation_id for o in still_unresolved] == [op.operation_id]

    reconciled = repo.finish(op.operation_id, status=OperationStatus.SUCCEEDED)
    assert reconciled.status == OperationStatus.SUCCEEDED
    assert repo.list_unresolved(task_id=task_id) == []


def test_operation_id_uniqueness(repo, task_id):
    op1 = repo.start(
        task_id=task_id, worktree_id="wt-1", worker_id="w", worker_session_id="s",
        tool_name="fs.write", risk_class="WRITE",
        request_hash=compute_request_hash("fs.write", {}), target_resource="a.txt",
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):  # duplicate primary key
        repo.start(
            task_id=task_id, worktree_id="wt-1", worker_id="w", worker_session_id="s",
            tool_name="fs.write", risk_class="WRITE",
            request_hash=compute_request_hash("fs.write", {}), target_resource="a.txt",
            operation_id=op1.operation_id,
        )


def test_request_hash_is_deterministic():
    a = compute_request_hash("fs.write", {"path": "a.txt", "mode": "644"})
    b = compute_request_hash("fs.write", {"mode": "644", "path": "a.txt"})  # different order
    c = compute_request_hash("fs.write", {"path": "b.txt", "mode": "644"})
    assert a == b, "key order must not change the hash"
    assert a != c


def test_record_child_pid(repo, task_id):
    op = repo.start(
        task_id=task_id, worktree_id="wt-1", worker_id="w", worker_session_id="s",
        tool_name="command.pytest", risk_class="EXECUTE",
        request_hash=compute_request_hash("command.pytest", {}), target_resource="pytest -q",
    )
    updated = repo.record_child_pid(op.operation_id, 12345, "2026-01-01T00:00:00.000000Z")
    assert updated.child_pid == 12345
    assert updated.child_pid_started_at == "2026-01-01T00:00:00.000000Z"


def test_lease_generation_is_nullable_in_phase_1(repo, task_id):
    op = repo.start(
        task_id=task_id, worktree_id="wt-1", worker_id="w", worker_session_id="s",
        tool_name="fs.write", risk_class="WRITE",
        request_hash=compute_request_hash("fs.write", {}), target_resource="a.txt",
    )
    assert op.lease_generation is None
