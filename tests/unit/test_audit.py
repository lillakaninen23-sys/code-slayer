"""Append-only, hash-chained audit log."""

from __future__ import annotations

import sqlite3

import pytest

from code_slayer.audit.events import EventType
from code_slayer.audit.verify import verify_chain
from code_slayer.audit.writer import AuditWriter
from code_slayer.store.db import transaction
from code_slayer.store.task_repo import TaskRepo


def _make_task(conn) -> str:
    task = TaskRepo(conn).create(
        description="d", repo_root="/r", repo_id="repo-1", worktree_id="wt-1"
    )
    return task.task_id


def test_append_event(db_conn):
    task_id = _make_task(db_conn)
    writer = AuditWriter(db_conn)
    with transaction(db_conn):
        record = writer.append(
            task_id=task_id,
            event_type=EventType.WORKER_STARTED,
            actor_type="worker",
            actor_id="fake-1",
            payload={"role": "coder"},
        )
    assert record.seq == 2  # TASK_CREATED was seq 1
    assert record.event_hash
    row = db_conn.execute(
        "SELECT * FROM audit_events WHERE id = ?", (record.id,)
    ).fetchone()
    assert row["event_type"] == "WORKER_STARTED"


def test_monotonic_seq_per_task(db_conn):
    task_id = _make_task(db_conn)
    writer = AuditWriter(db_conn)
    seqs = []
    for i in range(5):
        with transaction(db_conn):
            record = writer.append(
                task_id=task_id,
                event_type=EventType.WORKER_HEARTBEAT,
                actor_type="worker",
                actor_id="fake-1",
                payload={"i": i},
            )
        seqs.append(record.seq)
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))


def test_hash_chain_valid_after_many_events(db_conn):
    task_id = _make_task(db_conn)
    writer = AuditWriter(db_conn)
    for i in range(10):
        with transaction(db_conn):
            writer.append(
                task_id=task_id,
                event_type=EventType.WORKER_HEARTBEAT,
                actor_type="worker",
                actor_id="fake-1",
                payload={"i": i},
            )
    result = verify_chain(db_conn, task_id=task_id)
    assert result.ok, result.issues
    assert result.events_checked == 11  # TASK_CREATED + 10 heartbeats


@pytest.mark.parametrize(
    ("column", "new_value"),
    [
        ("payload_json", '{"tampered":true}'),
        ("event_type", "TASK_COMPLETED"),
        ("actor_id", "someone-else"),
    ],
)
def test_tampering_is_detected(db_conn, column, new_value):
    task_id = _make_task(db_conn)
    writer = AuditWriter(db_conn)
    with transaction(db_conn):
        record = writer.append(
            task_id=task_id,
            event_type=EventType.WORKER_STARTED,
            actor_type="worker",
            actor_id="fake-1",
            payload={"role": "coder"},
        )
    assert verify_chain(db_conn, task_id=task_id).ok

    # Bypass AuditWriter entirely to simulate tampering directly on the row
    # (the trigger only blocks UPDATE/DELETE; here we disable it on purpose
    # to prove *verification*, not the trigger, catches semantic tampering).
    db_conn.execute("DROP TRIGGER audit_no_update")
    db_conn.execute(
        f"UPDATE audit_events SET {column} = ? WHERE id = ?", (new_value, record.id)
    )

    result = verify_chain(db_conn, task_id=task_id)
    assert not result.ok
    assert any(issue.reason == "hash_mismatch" for issue in result.issues)


def test_update_rejected_by_db_trigger(db_conn):
    task_id = _make_task(db_conn)
    writer = AuditWriter(db_conn)
    with transaction(db_conn):
        record = writer.append(
            task_id=task_id,
            event_type=EventType.WORKER_STARTED,
            actor_type="worker",
            actor_id="fake-1",
            payload={},
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db_conn.execute(
            "UPDATE audit_events SET event_type = 'X' WHERE id = ?", (record.id,)
        )


def test_delete_rejected_by_db_trigger(db_conn):
    task_id = _make_task(db_conn)
    writer = AuditWriter(db_conn)
    with transaction(db_conn):
        record = writer.append(
            task_id=task_id,
            event_type=EventType.WORKER_STARTED,
            actor_type="worker",
            actor_id="fake-1",
            payload={},
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db_conn.execute("DELETE FROM audit_events WHERE id = ?", (record.id,))


def test_rollback_leaves_neither_partial_state_nor_partial_audit(db_conn):
    task_id = _make_task(db_conn)
    before_count = db_conn.execute(
        "SELECT COUNT(*) AS c FROM audit_events WHERE task_id = ?", (task_id,)
    ).fetchone()["c"]

    writer = AuditWriter(db_conn)
    with pytest.raises(RuntimeError):
        with transaction(db_conn):
            db_conn.execute(
                "UPDATE tasks SET state = 'PLANNING' WHERE task_id = ?", (task_id,)
            )
            writer.append(
                task_id=task_id,
                event_type=EventType.STATE_TRANSITION,
                actor_type="system",
                actor_id=None,
                payload={"to_state": "PLANNING"},
            )
            raise RuntimeError("crash before commit")

    after_count = db_conn.execute(
        "SELECT COUNT(*) AS c FROM audit_events WHERE task_id = ?", (task_id,)
    ).fetchone()["c"]
    state = db_conn.execute(
        "SELECT state FROM tasks WHERE task_id = ?", (task_id,)
    ).fetchone()["state"]

    assert after_count == before_count, "audit event must not have been partially committed"
    assert state == "CREATED", "task state must not have been partially committed"
