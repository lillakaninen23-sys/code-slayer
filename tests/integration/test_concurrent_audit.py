"""Racing independent connections must retain every event in a valid chain."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from code_slayer.audit.events import EventType
from code_slayer.audit.verify import verify_chain
from code_slayer.audit.writer import AuditWriter
from code_slayer.runner import LocalWorkerRunner
from code_slayer.store.task_repo import TaskRepo


@pytest.mark.parametrize("kind", ["task", "system", "runner"])
def test_concurrent_appends_are_serialized(git_repo_with_commit, monkeypatch, kind):
    primary = git_repo_with_commit
    setup = LocalWorkerRunner(primary)
    task_id = None
    if kind == "task":
        task_id = TaskRepo(setup._control_conn).create(
            description="audit race", repo_root=str(primary), repo_id=setup._primary.repo_id,
            worktree_id=setup._primary.worktree_id,
        ).task_id
    before = setup._control_conn.execute(
        "SELECT count(*) FROM audit_events WHERE task_id IS ?", (task_id,),
    ).fetchone()[0]
    original = AuditWriter._next_seq_and_prev_hash

    def require_transaction(self, task_id):
        # Assert at the exact former race window, before the chain-tip read.
        assert self._conn.in_transaction
        return original(self, task_id)

    monkeypatch.setattr(AuditWriter, "_next_seq_and_prev_hash", require_transaction)
    workers, events_per_worker = 6, 30
    barrier = Barrier(workers)

    def append_events(worker):
        runner = LocalWorkerRunner(primary)
        try:
            for event in range(events_per_worker):
                barrier.wait(timeout=10)
                payload = {"worker": worker, "event": event}
                if kind == "runner":
                    runner._audit("racing-run", EventType.RUN_RESUMED, payload)
                else:
                    AuditWriter(runner._control_conn).append(
                        task_id=task_id, event_type=EventType.WORKER_HEARTBEAT,
                        actor_type="system", actor_id="race", payload=payload,
                    )
        finally:
            runner.close()

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(append_events, range(workers)))  # surface every writer failure
        chain = verify_chain(setup._control_conn, task_id=task_id)
        assert chain.ok, chain.issues
        assert chain.events_checked == before + workers * events_per_worker
        rows = setup._control_conn.execute(
            "SELECT seq, payload_json FROM audit_events WHERE task_id IS ? ORDER BY seq",
            (task_id,),
        ).fetchall()
        assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
        assert len({row["payload_json"] for row in rows[before:]}) == workers * events_per_worker
    finally:
        setup.close()
