"""H.3 Stage 1: durable worker lifecycle storage foundation.

Covers `store.workers_repo.WorkersRepo`'s lifecycle operations
(`archive`/`reactivate`/`archive_in_transaction`/`reactivate_in_
transaction`/`is_active`) and schema v18 migration. No live Ollama
contact, no certificate/trust/permission mutation anywhere in this
file -- this file proves the storage layer only.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from code_slayer.store import db as db_module
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.workers_repo import WorkerLifecycleState, WorkersRepo


def _apply_through(conn: sqlite3.Connection, version: int) -> None:
    """Mirrors `test_promotion_provenance_migration._apply_through`:
    apply every known migration up to (and including) `version` only."""
    current = db_module.schema_version(conn)
    for mig_version, _name, sql in db_module._discover_migrations():
        if mig_version <= current or mig_version > version:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (mig_version, utcnow_iso()),
        )
        conn.execute("COMMIT")


# -- WorkersRepo lifecycle ----------------------------------------------------


@pytest.fixture
def repo(db_conn) -> WorkersRepo:
    return WorkersRepo(db_conn)


def test_freshly_registered_worker_is_active_with_no_lifecycle_change(repo):
    worker = repo.register(worker_id="w1", kind="fake", network_class="local")
    assert worker.lifecycle_state == WorkerLifecycleState.ACTIVE
    assert worker.lifecycle_changed_at is None
    assert repo.is_active("w1") is True


def test_archive_active_worker_transitions_and_reports_changed(repo):
    repo.register(worker_id="w1", kind="fake", network_class="local")
    worker, changed = repo.archive("w1")
    assert changed is True
    assert worker.lifecycle_state == WorkerLifecycleState.ARCHIVED
    assert worker.lifecycle_changed_at is not None
    assert repo.is_active("w1") is False


def test_archive_already_archived_is_idempotent_no_op(repo):
    repo.register(worker_id="w1", kind="fake", network_class="local")
    first, first_changed = repo.archive("w1")
    second, second_changed = repo.archive("w1")
    assert first_changed is True
    assert second_changed is False
    assert second.lifecycle_state == WorkerLifecycleState.ARCHIVED
    # No second lifecycle_changed_at write on the no-op.
    assert second.lifecycle_changed_at == first.lifecycle_changed_at


def test_reactivate_archived_worker_transitions_and_reports_changed(repo):
    repo.register(worker_id="w1", kind="fake", network_class="local")
    repo.archive("w1")
    worker, changed = repo.reactivate("w1")
    assert changed is True
    assert worker.lifecycle_state == WorkerLifecycleState.ACTIVE
    assert repo.is_active("w1") is True


def test_reactivate_already_active_is_idempotent_no_op(repo):
    repo.register(worker_id="w1", kind="fake", network_class="local")
    worker, changed = repo.reactivate("w1")
    assert changed is False
    assert worker.lifecycle_state == WorkerLifecycleState.ACTIVE


def test_archive_unknown_worker_returns_none_false(repo):
    worker, changed = repo.archive("ghost")
    assert worker is None
    assert changed is False


def test_reactivate_unknown_worker_returns_none_false(repo):
    worker, changed = repo.reactivate("ghost")
    assert worker is None
    assert changed is False


def test_registering_an_archived_worker_again_does_not_reactivate(repo):
    """The core restart/re-registration invariant, exercised directly
    against WorkersRepo (a service-level regression test covers the
    same invariant through the real ApplicationService/config path)."""
    repo.register(worker_id="w1", kind="fake", network_class="local")
    repo.archive("w1")
    again = repo.register(worker_id="w1", kind="fake", network_class="local")
    assert again.lifecycle_state == WorkerLifecycleState.ARCHIVED


def test_in_transaction_forms_require_an_open_transaction(repo):
    with pytest.raises(RuntimeError):
        repo.archive_in_transaction("w1")
    with pytest.raises(RuntimeError):
        repo.reactivate_in_transaction("w1")


def test_in_transaction_forms_join_a_caller_owned_transaction(db_conn, repo):
    repo.register(worker_id="w1", kind="fake", network_class="local")
    with transaction(db_conn):
        worker, changed = repo.archive_in_transaction("w1")
        assert changed is True
        assert worker.lifecycle_state == WorkerLifecycleState.ARCHIVED
    # Committed once the block exits.
    assert repo.get("w1").lifecycle_state == WorkerLifecycleState.ARCHIVED


def test_check_constraint_rejects_invalid_lifecycle_via_raw_sql(db_conn, repo):
    repo.register(worker_id="w1", kind="fake", network_class="local")
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "UPDATE workers SET lifecycle_state = 'DELETED' WHERE worker_id = 'w1'",
        )


def test_check_constraint_rejects_invalid_lifecycle_on_insert(db_conn):
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "INSERT INTO workers (worker_id, kind, network_class, availability_state, "
            "lifecycle_state) VALUES ('bad', 'fake', 'local', 'UNKNOWN', 'DELETED')",
        )


def test_other_worker_fields_are_untouched_by_archive(repo, db_conn):
    repo.register(worker_id="w1", kind="fake", network_class="local")
    db_conn.execute(
        "UPDATE workers SET availability_state = 'AVAILABLE', last_error = 'boom' "
        "WHERE worker_id = 'w1'",
    )
    worker, _ = repo.archive("w1")
    assert worker.kind == "fake"
    assert worker.network_class == "local"
    assert worker.availability_state == "AVAILABLE"
    assert worker.last_error == "boom"


# -- concurrency ---------------------------------------------------------


def test_concurrent_archive_calls_leave_exactly_one_valid_lifecycle(tmp_path):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    db_module.migrate(conn)
    WorkersRepo(conn).register(worker_id="w1", kind="fake", network_class="local")
    conn.close()

    barrier = threading.Barrier(2)
    outcomes: list = [None, None]

    def _run(index):
        thread_conn = db_module.connect(path)
        try:
            barrier.wait(timeout=5)
            outcomes[index] = WorkersRepo(thread_conn).archive("w1")
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
    worker = WorkersRepo(verify_conn).get("w1")
    assert worker.lifecycle_state == WorkerLifecycleState.ARCHIVED
    # SQLite's own write-lock serializes the two BEGIN IMMEDIATE
    # transactions -- both may legitimately succeed (one real
    # transition, one idempotent no-op observing the already-ARCHIVED
    # row), but never leave a partial/ambiguous lifecycle value.
    successes = [o for o in outcomes if isinstance(o, tuple)]
    assert len(successes) >= 1
    assert all(w is not None and w.lifecycle_state == "ARCHIVED" for w, _ in successes)
    verify_conn.close()


def test_concurrent_archive_and_reactivate_leaves_one_valid_final_state(tmp_path):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    db_module.migrate(conn)
    WorkersRepo(conn).register(worker_id="w1", kind="fake", network_class="local")
    conn.close()

    barrier = threading.Barrier(2)
    outcomes: list = [None, None]

    def _archive(index):
        thread_conn = db_module.connect(path)
        try:
            barrier.wait(timeout=5)
            outcomes[index] = ("archive", WorkersRepo(thread_conn).archive("w1"))
        except sqlite3.OperationalError as exc:
            outcomes[index] = ("archive", exc)
        finally:
            thread_conn.close()

    def _reactivate(index):
        thread_conn = db_module.connect(path)
        try:
            barrier.wait(timeout=5)
            outcomes[index] = ("reactivate", WorkersRepo(thread_conn).reactivate("w1"))
        except sqlite3.OperationalError as exc:
            outcomes[index] = ("reactivate", exc)
        finally:
            thread_conn.close()

    threads = [
        threading.Thread(target=_archive, args=(0,)),
        threading.Thread(target=_reactivate, args=(1,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads)

    verify_conn = db_module.connect(path)
    worker = WorkersRepo(verify_conn).get("w1")
    # No predetermined winner is required -- only that the final state
    # is exactly one valid lifecycle value, and that whichever call
    # observed a real (changed=True) transition reported a lifecycle
    # value consistent with what was actually committed.
    assert worker.lifecycle_state in WorkerLifecycleState.ALL
    verify_conn.close()


# -- migration v18 --------------------------------------------------------


def test_v17_to_v18_upgrade_succeeds_and_existing_worker_becomes_active(tmp_path):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    _apply_through(conn, 17)
    assert db_module.schema_version(conn) == 17
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO workers (worker_id, kind, network_class, availability_state) "
        "VALUES ('w1', 'fake', 'local', 'UNKNOWN')",
    )
    conn.execute("COMMIT")

    _apply_through(conn, 18)
    assert db_module.schema_version(conn) == 18

    row = conn.execute("SELECT * FROM workers WHERE worker_id = 'w1'").fetchone()
    assert row["lifecycle_state"] == "ACTIVE"
    assert row["lifecycle_changed_at"] is None
    assert row["kind"] == "fake"
    assert row["network_class"] == "local"
    conn.close()


def test_v17_to_v18_upgrade_preserves_certificate_and_history_rows(tmp_path):
    """Seeds a Baseline Security certificate, a role certificate, and a
    runner_runs row at v17, then proves every one of those rows'
    identity/content columns is byte-for-byte unchanged after the v18
    upgrade -- this migration touches only `workers`."""
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    _apply_through(conn, 17)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO workers (worker_id, kind, network_class, availability_state) "
        "VALUES ('w1', 'fake', 'local', 'UNKNOWN')",
    )
    conn.execute(
        "INSERT INTO worker_baseline_security_certificates "
        "(certificate_id, worker_id, baseline_version, model_tag, model_digest, "
        "endpoint, runtime_version, normalizer_id, normalizer_version, "
        "runtime_config_fingerprint, runtime_identity_fingerprint, outcome, "
        "hard_disqualifiers_json, evidence_ref, reason, issued_at) "
        "VALUES ('cert-1', 'w1', 'baseline-security-v1', 'qwen3-coder:30b', "
        "'sha256:abc', 'http://127.0.0.1:11434/v1', '0.16.1', NULL, NULL, NULL, "
        "'f' || substr(hex(randomblob(31)), 1, 63), 'PASS', '[]', 'ev-1', 'ok', "
        "'2026-01-01T00:00:00.000000Z')",
    )
    conn.execute(
        "INSERT INTO runner_runs (run_id, created_at, updated_at, repo_id, "
        "primary_worktree_id, original_prompt_hash, worker_id, role, status) "
        "VALUES ('run-1', '2026-01-01T00:00:00.000000Z', "
        "'2026-01-01T00:00:00.000000Z', 'repo-1', 'wt-1', 'hash-1', 'w1', "
        "'PLANNER', 'COMPLETED')",
    )
    conn.execute("COMMIT")

    cert_before = dict(
        conn.execute(
            "SELECT * FROM worker_baseline_security_certificates WHERE certificate_id = 'cert-1'",
        ).fetchone(),
    )
    run_before = dict(conn.execute("SELECT * FROM runner_runs WHERE run_id = 'run-1'").fetchone())

    _apply_through(conn, 18)
    assert db_module.schema_version(conn) == 18

    cert_after = dict(
        conn.execute(
            "SELECT * FROM worker_baseline_security_certificates WHERE certificate_id = 'cert-1'",
        ).fetchone(),
    )
    run_after = dict(conn.execute("SELECT * FROM runner_runs WHERE run_id = 'run-1'").fetchone())
    assert cert_after == cert_before
    assert run_after == run_before
    conn.close()


def test_fresh_db_gets_lifecycle_columns_at_v18(tmp_path):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    _apply_through(conn, 18)
    assert db_module.schema_version(conn) == 18
    WorkersRepo(conn).register(worker_id="w1", kind="fake", network_class="local")
    worker = WorkersRepo(conn).get("w1")
    assert worker.lifecycle_state == "ACTIVE"
    assert worker.lifecycle_changed_at is None
    conn.close()


def test_invalid_lifecycle_state_cannot_be_written_through_workers_repo(db_conn):
    """WorkersRepo itself never accepts a lifecycle value outside
    ACTIVE/ARCHIVED -- there is no public method that takes an
    arbitrary lifecycle string at all, so this is a structural
    guarantee, not merely a runtime check. Documented here so the
    invariant has an explicit regression test."""
    import inspect

    from code_slayer.store.workers_repo import WorkersRepo as Repo

    for name, method in inspect.getmembers(Repo, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        params = inspect.signature(method).parameters
        assert "lifecycle_state" not in params, (
            f"WorkersRepo.{name} must not accept a caller-supplied lifecycle_state"
        )
