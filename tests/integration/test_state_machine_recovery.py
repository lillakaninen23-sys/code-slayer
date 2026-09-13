"""Real SQLite writers, reopen/resume, and abrupt process termination."""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from code_slayer.audit.verify import verify_chain
from code_slayer.core import StaleTaskState, TaskState, TaskStateMachine
from code_slayer.store.db import connect, migrate
from code_slayer.store.task_repo import TaskRepo

_SRC = str(Path(__file__).resolve().parents[2] / "src")
_PATH = (
    "CREATED", "INSPECTING", "BASELINED", "PLANNING", "PLANNED", "IMPLEMENTING",
    "VERIFYING", "REVIEWING", "READY_FOR_CHECKPOINT", "CHECKPOINTED",
)


def setup_task(conn, target):
    task = TaskRepo(conn).create(
        task_id="task", description="recovery", repo_root="/r", repo_id="r", worktree_id="w",
    )
    machine = TaskStateMachine(conn)
    route = (*_PATH[:7], "REPAIRING") if target == "REPAIRING" else _PATH
    for state in route[1:route.index(target) + 1]:
        task = machine.transition(
            task.task_id, expected_state=TaskState(task.state),
            to_state=TaskState(state), reason="setup through legal path",
        )
    return task


def audit_rows(conn):
    return [dict(row) for row in conn.execute(
        "SELECT * FROM audit_events WHERE task_id = 'task' ORDER BY seq"
    )]


@pytest.mark.parametrize("origin", (*_PATH, "REPAIRING"))
@pytest.mark.parametrize("suspension", ["INTERRUPTED_RESUMABLE", "BLOCKED"])
def test_origin_and_phase_survive_close_reopen_and_resume(tmp_path, origin, suspension):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    try:
        migrate(conn)
        original = setup_task(conn, origin)
        TaskStateMachine(conn).transition(
            original.task_id, expected_state=TaskState(origin),
            to_state=TaskState(suspension), reason="suspend",
        )
    finally:
        conn.close()

    reopened = connect(db_path)
    try:
        paused = TaskRepo(reopened).get("task")
        assert (paused.state, paused.current_phase) == (suspension, origin)
        resumed = TaskStateMachine(reopened).transition(
            "task", expected_state=TaskState(suspension), to_state=TaskState(origin),
            reason="explicit return to origin",
            reconciled_target=TaskState(origin) if suspension == "BLOCKED" else None,
        )
        assert (resumed.state, resumed.current_phase) == (original.state, original.current_phase)
        assert verify_chain(reopened, task_id="task").ok
    finally:
        reopened.close()


def test_two_incompatible_writers_exactly_one_wins_original_state(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    try:
        migrate(conn)
        setup_task(conn, "PLANNED")
        before = audit_rows(conn)
    finally:
        conn.close()

    ready = Barrier(2)

    def writer(target):
        connection = connect(db_path)
        try:
            original = TaskRepo(connection).get("task")
            assert original.state == "PLANNED"
            # Both writers have observed the same starting state before
            # either begins its own transition transaction.
            ready.wait(timeout=10)
            try:
                updated = TaskStateMachine(connection).transition(
                    "task", expected_state=TaskState(original.state), to_state=target,
                    reason=f"writer requesting {target}",
                )
                return "won", updated
            except StaleTaskState:
                assert not connection.in_transaction
                return "stale", None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer, target)
                   for target in (TaskState.IMPLEMENTING, TaskState.BLOCKED)]
        outcomes = [future.result(timeout=15) for future in futures]
    assert sorted(status for status, _ in outcomes) == ["stale", "won"]
    winner = next(task for status, task in outcomes if status == "won")

    reopened = connect(db_path)
    try:
        assert TaskRepo(reopened).get("task") == winner
        after = audit_rows(reopened)
        assert after[:-1] == before
        payload = json.loads(after[-1]["payload_json"])
        assert payload["from_state"] == "PLANNED"
        assert payload["to_state"] == winner.state
        assert payload["phase_after"] == winner.current_phase
        assert verify_chain(reopened, task_id="task").ok
    finally:
        reopened.close()


_CRASH_SCRIPT = """
import os
import sys
sys.path.insert(0, sys.argv[1])

from code_slayer.audit.writer import AuditWriter
from code_slayer.core import TaskState, TaskStateMachine
from code_slayer.store.db import connect
from code_slayer.store.task_repo import TaskRepo

db_path, point, target = sys.argv[2:]
conn = connect(db_path)
original_append = AuditWriter.append
before_count = conn.execute('SELECT count(*) FROM audit_events').fetchone()[0]

def crash_during_append(self, **kwargs):
    assert conn.in_transaction
    assert TaskRepo(conn).get('task').state == target
    # A separate connection must still see the old committed state.
    reader = connect(db_path)
    assert TaskRepo(reader).get('task').state == 'IMPLEMENTING'
    assert reader.execute('SELECT count(*) FROM audit_events').fetchone()[0] == before_count
    reader.close()
    if point == 'after_update':
        os._exit(73)
    result = original_append(self, **kwargs)
    assert conn.execute('SELECT count(*) FROM audit_events').fetchone()[0] == before_count + 1
    if point == 'before_commit':
        os._exit(73)
    return result

AuditWriter.append = crash_during_append
TaskStateMachine(conn).transition(
    'task', expected_state=TaskState.IMPLEMENTING, to_state=TaskState(target),
    reason='crash exercise',
)
assert point == 'after_commit'
assert not conn.in_transaction
os._exit(73)  # no finally blocks, close, or interpreter cleanup
"""


@pytest.mark.parametrize("point", ["after_update", "before_commit", "after_commit"])
@pytest.mark.parametrize("target", ["VERIFYING", "INTERRUPTED_RESUMABLE", "BLOCKED"])
def test_subprocess_crash_never_splits_state_phase_and_audit(tmp_path, point, target):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    try:
        migrate(conn)
        original = setup_task(conn, "IMPLEMENTING")
        before = audit_rows(conn)
    finally:
        conn.close()

    result = subprocess.run(
        [sys.executable, "-c", _CRASH_SCRIPT, _SRC, str(db_path), point, target],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 73, (result.stdout, result.stderr)

    reopened = connect(db_path)
    try:
        assert migrate(reopened) == 1
        task = TaskRepo(reopened).get("task")
        after = audit_rows(reopened)
        if point == "after_commit":
            assert task.state == target
            assert task.current_phase == ("VERIFYING" if target == "VERIFYING" else "IMPLEMENTING")
            assert after[:-1] == before
            event = after[-1]
            payload = json.loads(event["payload_json"])
            assert event["event_type"] == "STATE_TRANSITION"
            assert payload["from_state"] == "IMPLEMENTING"
            assert payload["to_state"] == target
            assert payload["phase_before"] == "IMPLEMENTING"
            assert payload["phase_after"] == task.current_phase
            if target != "VERIFYING":
                assert payload["resume_origin"] == "IMPLEMENTING"
        else:
            assert task == original
            assert after == before
        assert verify_chain(reopened, task_id="task").ok
        # Prove the reopened machine can continue, including exact-origin return.
        next_state = TaskState.REVIEWING if task.state == "VERIFYING" else (
            TaskState.VERIFYING if task.state == "IMPLEMENTING" else TaskState.IMPLEMENTING
        )
        TaskStateMachine(reopened).transition(
            "task", expected_state=TaskState(task.state), to_state=next_state,
            reason="continue after reopen",
            reconciled_target=next_state if task.state == "BLOCKED" else None,
        )
        assert len(audit_rows(reopened)) == len(after) + 1
        assert verify_chain(reopened, task_id="task").ok
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()
