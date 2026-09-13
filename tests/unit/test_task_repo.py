"""Task persistence primitives."""

from __future__ import annotations

import pytest

from code_slayer.audit.verify import verify_chain
from code_slayer.store.task_repo import TaskAlreadyActiveError, TaskRepo


def test_create_and_get(db_conn):
    repo = TaskRepo(db_conn)
    task = repo.create(
        description="do the thing", repo_root="/r", repo_id="repo-1", worktree_id="wt-1"
    )
    fetched = repo.get(task.task_id)
    assert fetched == task
    assert fetched.state == "CREATED"
    assert fetched.current_phase is None


def test_create_emits_task_created_audit_event(db_conn):
    repo = TaskRepo(db_conn)
    task = repo.create(
        description="do the thing", repo_root="/r", repo_id="repo-1", worktree_id="wt-1"
    )
    row = db_conn.execute(
        "SELECT * FROM audit_events WHERE task_id = ? AND event_type = 'TASK_CREATED'",
        (task.task_id,),
    ).fetchone()
    assert row is not None
    assert verify_chain(db_conn, task_id=task.task_id).ok


def test_second_non_terminal_task_on_same_worktree_is_rejected(db_conn):
    repo = TaskRepo(db_conn)
    repo.create(description="first", repo_root="/r", repo_id="repo-1", worktree_id="wt-1")
    with pytest.raises(TaskAlreadyActiveError):
        repo.create(description="second", repo_root="/r", repo_id="repo-1", worktree_id="wt-1")


def test_new_task_allowed_once_prior_task_is_terminal(db_conn):
    repo = TaskRepo(db_conn)
    first = repo.create(description="first", repo_root="/r", repo_id="repo-1", worktree_id="wt-1")
    repo.record_transition(
        first.task_id, to_state="COMPLETED", to_phase=None, reason="done"
    )
    second = repo.create(description="second", repo_root="/r", repo_id="repo-1", worktree_id="wt-1")
    assert second.task_id != first.task_id


def test_record_transition_updates_state_and_phase_atomically_with_audit(db_conn):
    repo = TaskRepo(db_conn)
    task = repo.create(
        description="d", repo_root="/r", repo_id="repo-1", worktree_id="wt-1"
    )
    updated = repo.record_transition(
        task.task_id, to_state="INSPECTING", to_phase=None, reason="task accepted"
    )
    assert updated.state == "INSPECTING"

    row = db_conn.execute(
        "SELECT * FROM audit_events WHERE task_id = ? AND event_type = 'STATE_TRANSITION' "
        "ORDER BY seq DESC LIMIT 1",
        (task.task_id,),
    ).fetchone()
    assert row is not None
    import json

    payload = json.loads(row["payload_json"])
    assert payload["from_state"] == "CREATED"
    assert payload["to_state"] == "INSPECTING"


def test_get_active_for_worktree(db_conn):
    repo = TaskRepo(db_conn)
    assert repo.get_active_for_worktree("wt-1") is None
    task = repo.create(description="d", repo_root="/r", repo_id="repo-1", worktree_id="wt-1")
    active = repo.get_active_for_worktree("wt-1")
    assert active is not None
    assert active.task_id == task.task_id

    repo.record_transition(task.task_id, to_state="FAILED", to_phase=None, reason="abandoned")
    assert repo.get_active_for_worktree("wt-1") is None


def test_get_unknown_task_raises_key_error(db_conn):
    with pytest.raises(KeyError):
        TaskRepo(db_conn).get("does-not-exist")
