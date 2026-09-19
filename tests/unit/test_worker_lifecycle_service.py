"""H.3 Stage 2: the canonical lifecycle service (`workers.lifecycle`).

Covers the atomic active-runner-work precheck + lifecycle UPDATE +
audit-event append, per-status active-work policy (RUNNING/ANALYZING/
READY refuse; BLOCKED_ON_QUESTIONS/INTERRUPTED_RESUMABLE do not),
idempotency, and audit-event shape. No live Ollama contact, no
certificate/trust/permission mutation.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from code_slayer.store import db as db_module
from code_slayer.store.db import transaction
from code_slayer.store.runner_repo import RunnerRepo
from code_slayer.store.workers_repo import WorkerLifecycleState, WorkersRepo
from code_slayer.workers.lifecycle import archive_worker, reactivate_worker

WORKER = "w1"


@pytest.fixture
def registered(db_conn) -> str:
    WorkersRepo(db_conn).register(worker_id=WORKER, kind="fake", network_class="local")
    return WORKER


def _seed_run(conn, *, run_id: str, worker_id: str, status: str) -> None:
    with transaction(conn):
        RunnerRepo(conn).create_in_transaction(
            run_id=run_id, created_at="2026-01-01T00:00:00.000000Z",
            repo_id="repo-1", primary_worktree_id="wt-1", original_prompt_hash="hash-1",
            worker_id=worker_id, role="PLANNER", requires_mutation=False, status=status,
        )


def _audit_rows(conn, event_type: str) -> list[dict]:
    rows = conn.execute(
        "SELECT task_id, actor_type, actor_id, payload_json FROM audit_events "
        "WHERE event_type = ? ORDER BY id",
        (event_type,),
    ).fetchall()
    return [dict(r) for r in rows]


# -- basic archive/reactivate ---------------------------------------------


def test_archive_active_worker_succeeds_and_audits(db_conn, registered):
    result = archive_worker(db_conn, worker_id=registered)
    assert result.ok is True
    assert result.changed is True
    assert result.reason == "archived"
    assert result.worker.lifecycle_state == WorkerLifecycleState.ARCHIVED

    rows = _audit_rows(db_conn, "WORKER_ARCHIVED")
    assert len(rows) == 1
    assert rows[0]["task_id"] is None
    assert rows[0]["actor_type"] == "system"
    payload = rows[0]["payload_json"]
    assert '"worker_id": "w1"' in payload or '"worker_id":"w1"' in payload
    assert "ACTIVE" in payload
    assert "ARCHIVED" in payload


def test_reactivate_archived_worker_succeeds_and_audits(db_conn, registered):
    archive_worker(db_conn, worker_id=registered)
    result = reactivate_worker(db_conn, worker_id=registered)
    assert result.ok is True
    assert result.changed is True
    assert result.worker.lifecycle_state == WorkerLifecycleState.ACTIVE
    assert len(_audit_rows(db_conn, "WORKER_REACTIVATED")) == 1


def test_archive_unknown_worker_refuses(db_conn):
    result = archive_worker(db_conn, worker_id="ghost")
    assert result.ok is False
    assert result.reason == "unknown_worker"
    assert result.worker is None
    assert _audit_rows(db_conn, "WORKER_ARCHIVED") == []


def test_reactivate_unknown_worker_refuses(db_conn):
    result = reactivate_worker(db_conn, worker_id="ghost")
    assert result.ok is False
    assert result.reason == "unknown_worker"


def test_malformed_worker_id_refuses(db_conn):
    for bad in (None, "", 123):
        result = archive_worker(db_conn, worker_id=bad)
        assert result.ok is False
        assert result.reason == "malformed_lifecycle_request"


# -- idempotency: no duplicate audit event on a no-op ----------------------


def test_archive_already_archived_is_idempotent_no_duplicate_audit(db_conn, registered):
    archive_worker(db_conn, worker_id=registered)
    second = archive_worker(db_conn, worker_id=registered)
    assert second.ok is True
    assert second.changed is False
    assert second.reason == "already_archived"
    assert len(_audit_rows(db_conn, "WORKER_ARCHIVED")) == 1


def test_reactivate_already_active_is_idempotent_no_duplicate_audit(db_conn, registered):
    result = reactivate_worker(db_conn, worker_id=registered)
    assert result.ok is True
    assert result.changed is False
    assert result.reason == "already_active"
    assert _audit_rows(db_conn, "WORKER_REACTIVATED") == []


# -- active ordinary-runner policy: per durable run status ------------------
#
# RUNNING/ANALYZING/READY represent pending or in-flight worker
# execution and refuse archiving; BLOCKED_ON_QUESTIONS/INTERRUPTED_
# RESUMABLE/terminal do not -- see `workers.lifecycle`'s own module
# docstring "Active ordinary-runner policy" for the full reasoning.


@pytest.mark.parametrize("status", ["RUNNING", "ANALYZING", "READY"])
def test_archive_refuses_with_blocking_non_terminal_run(db_conn, registered, status):
    _seed_run(db_conn, run_id=f"run-{status}", worker_id=registered, status=status)
    result = archive_worker(db_conn, worker_id=registered)
    assert result.ok is False
    assert result.reason == "worker_has_active_work"
    assert WorkersRepo(db_conn).get(registered).lifecycle_state == WorkerLifecycleState.ACTIVE
    assert _audit_rows(db_conn, "WORKER_ARCHIVED") == []


@pytest.mark.parametrize("status", ["BLOCKED_ON_QUESTIONS", "INTERRUPTED_RESUMABLE"])
def test_archive_allowed_with_non_blocking_non_terminal_run(db_conn, registered, status):
    _seed_run(db_conn, run_id=f"run-{status}", worker_id=registered, status=status)
    result = archive_worker(db_conn, worker_id=registered)
    assert result.ok is True
    assert result.changed is True
    assert WorkersRepo(db_conn).get(registered).lifecycle_state == WorkerLifecycleState.ARCHIVED


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "DENIED_TRUST"])
def test_archive_allowed_with_only_terminal_runs(db_conn, registered, status):
    _seed_run(db_conn, run_id=f"run-{status}", worker_id=registered, status=status)
    result = archive_worker(db_conn, worker_id=registered)
    assert result.ok is True
    assert result.changed is True


def test_archive_refuses_when_any_run_among_several_is_blocking(db_conn, registered):
    _seed_run(db_conn, run_id="run-done", worker_id=registered, status="COMPLETED")
    _seed_run(db_conn, run_id="run-running", worker_id=registered, status="RUNNING")
    result = archive_worker(db_conn, worker_id=registered)
    assert result.ok is False
    assert result.reason == "worker_has_active_work"


def test_archive_refuses_with_blocking_run_hidden_behind_200_newer_rows(db_conn, registered):
    """H.3 review finding: the active-work check must never use
    `RunnerRepo.list_for_worker()`'s own history-page `LIMIT` (200) as
    authority -- an old blocking run could be hidden behind more than
    that many newer historical rows and archive would incorrectly
    succeed. Seeds one old RUNNING row, then 250 newer terminal rows,
    and proves archive still refuses and lifecycle stays ACTIVE."""
    _seed_run(db_conn, run_id="run-old-running", worker_id=registered, status="RUNNING")
    for i in range(250):
        _seed_run(db_conn, run_id=f"run-newer-{i:04d}", worker_id=registered, status="COMPLETED")

    # Sanity: the capped history view really does hide the old row.
    recent = RunnerRepo(db_conn).list_for_worker(registered)
    assert len(recent) == 200
    assert "run-old-running" not in {r.run_id for r in recent}

    result = archive_worker(db_conn, worker_id=registered)
    assert result.ok is False
    assert result.reason == "worker_has_active_work"
    assert WorkersRepo(db_conn).get(registered).lifecycle_state == WorkerLifecycleState.ACTIVE
    assert _audit_rows(db_conn, "WORKER_ARCHIVED") == []


def test_archiving_already_archived_worker_skips_active_work_check(db_conn, registered):
    """Archiving a worker that is already ARCHIVED is a no-op
    regardless of runner state -- there is nothing new to authorize,
    so the active-work precheck is not even performed."""
    archive_worker(db_conn, worker_id=registered)
    _seed_run(db_conn, run_id="run-running", worker_id=registered, status="RUNNING")
    result = archive_worker(db_conn, worker_id=registered)
    assert result.ok is True
    assert result.changed is False


def test_reactivate_never_checks_active_work(db_conn, registered):
    archive_worker(db_conn, worker_id=registered)
    _seed_run(db_conn, run_id="run-running", worker_id=registered, status="RUNNING")
    result = reactivate_worker(db_conn, worker_id=registered)
    assert result.ok is True
    assert result.changed is True


# -- atomicity: the lifecycle write and audit event share one commit --------


def test_archive_is_atomic_in_open_db_connection_only(db_conn, registered):
    """The transition and its audit event are written by the same
    `transaction()` block -- there is no code path that could leave
    one committed without the other in the same connection."""
    archive_worker(db_conn, worker_id=registered)
    assert db_conn.in_transaction is False
    assert WorkersRepo(db_conn).get(registered).lifecycle_state == "ARCHIVED"
    assert len(_audit_rows(db_conn, "WORKER_ARCHIVED")) == 1


# -- concurrency -------------------------------------------------------------


def test_concurrent_archive_calls_leave_exactly_one_lifecycle_value_and_no_duplicate_authority(
    tmp_path,
):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    db_module.migrate(conn)
    WorkersRepo(conn).register(worker_id=WORKER, kind="fake", network_class="local")
    conn.close()

    barrier = threading.Barrier(2)
    outcomes: list = [None, None]

    def _run(index):
        thread_conn = db_module.connect(path)
        try:
            barrier.wait(timeout=5)
            outcomes[index] = archive_worker(thread_conn, worker_id=WORKER)
        except sqlite3.OperationalError as exc:
            outcomes[index] = exc
        finally:
            thread_conn.close()

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads)

    verify_conn = db_module.connect(path)
    worker = WorkersRepo(verify_conn).get(WORKER)
    assert worker.lifecycle_state == "ARCHIVED"
    # Exactly one real transition -- the other observed the already-
    # ARCHIVED row and returned a no-op -- so exactly one audit event,
    # never two, regardless of which thread "won".
    audit_rows = _audit_rows(verify_conn, "WORKER_ARCHIVED")
    assert len(audit_rows) == 1
    verify_conn.close()
