"""Durable worker/model trust (Phase 7.2): LOCKED/GUARDED/AUTO derived
from append-only history, never a mutable status column — absence of
evidence always means LOCKED, and no path in this phase can ever grant
AUTO."""

from __future__ import annotations

import sqlite3

import pytest

from code_slayer.store.worker_trust_repo import TrustLevel, WorkerTrustRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.trust import WorkerTrustManager, _is_allowed_transition


@pytest.fixture
def registered_worker(db_conn) -> str:
    WorkersRepo(db_conn).register(worker_id="w1", kind="local-llm", network_class="local")
    return "w1"


@pytest.fixture
def manager(db_conn) -> WorkerTrustManager:
    return WorkerTrustManager(db_conn)


# --- 1. absence of history -> LOCKED ---------------------------------------

def test_worker_with_no_trust_history_is_locked(manager, registered_worker):
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.LOCKED


def test_unregistered_worker_is_also_locked_by_derivation(manager):
    """current_trust() is pure derivation over history -- it never
    itself checks whether the worker is registered (only the write path
    does); a nonexistent worker with no history is LOCKED same as any
    other absence of evidence."""
    assert manager.current_trust("never-registered", "coder") == TrustLevel.LOCKED


# --- 2/3/4. LOCKED -> GUARDED: evidence required ---------------------------

def test_exact_scope_locked_to_guarded_with_evidence_succeeds(manager, registered_worker):
    result = manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="conformance_pass",
        evidence_ref="conformance-run-1",
    )
    assert result.ok
    assert result.level == TrustLevel.GUARDED
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.GUARDED


def test_promote_without_evidence_rejected(manager, registered_worker):
    result = manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="conformance_pass",
        evidence_ref=None,  # type: ignore[arg-type]
    )
    assert not result.ok
    assert result.reason == "missing_evidence_reference"
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.LOCKED


def test_promote_with_blank_evidence_rejected(manager, registered_worker):
    result = manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="conformance_pass",
        evidence_ref="   ",
    )
    assert not result.ok
    assert result.reason == "missing_evidence_reference"
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.LOCKED


# --- 5/6. GUARDED -> LOCKED: reason required, no evidence needed ----------

def test_guarded_to_locked_downgrade(manager, registered_worker):
    manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="pass", evidence_ref="run-1",
    )
    result = manager.downgrade_to_locked(
        worker_id=registered_worker, role="coder", reason="malformed_tool_call",
    )
    assert result.ok
    assert result.level == TrustLevel.LOCKED
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.LOCKED


def test_downgrade_requires_a_reason(manager, registered_worker):
    manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="pass", evidence_ref="run-1",
    )
    result = manager.downgrade_to_locked(
        worker_id=registered_worker, role="coder", reason="   ",
    )
    assert not result.ok
    assert result.reason == "malformed_trust_request"
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.GUARDED


def test_downgrade_needs_no_evidence_reference(manager, registered_worker):
    """Losing trust is deliberately easier than gaining it."""
    manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="pass", evidence_ref="run-1",
    )
    result = manager.downgrade_to_locked(
        worker_id=registered_worker, role="coder", reason="malformed_tool_call",
        evidence_ref=None,
    )
    assert result.ok


# --- 7/8. append-only history remains fully queryable ----------------------

def test_history_remains_append_only_at_the_database_level(db_conn, registered_worker):
    repo = WorkerTrustRepo(db_conn)
    from code_slayer.store.db import transaction

    with transaction(db_conn):
        event = repo.append_in_transaction(
            worker_id=registered_worker, role="coder", capability=None,
            from_level=TrustLevel.LOCKED.value, to_level=TrustLevel.GUARDED.value,
            reason="pass", evidence_ref="run-1", occurred_at="2026-01-01T00:00:00.000000Z",
        )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "UPDATE worker_trust_events SET to_level = 'AUTO' WHERE id = ?", (event.id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute("DELETE FROM worker_trust_events WHERE id = ?", (event.id,))


def test_previous_trust_events_remain_queryable_after_later_transition(manager, registered_worker):
    manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="pass", evidence_ref="run-1",
    )
    manager.downgrade_to_locked(
        worker_id=registered_worker, role="coder", reason="malformed_tool_call",
    )
    history = manager.history(registered_worker, "coder")
    assert len(history) == 2
    assert (history[0].from_level, history[0].to_level) == ("LOCKED", "GUARDED")
    assert (history[1].from_level, history[1].to_level) == ("GUARDED", "LOCKED")
    assert history[0].evidence_ref == "run-1"
    assert history[1].evidence_ref is None


# --- 9/10. exact-scope semantics: no accidental generalization -------------

def test_capability_scope_a_does_not_grant_capability_scope_b(manager, registered_worker):
    manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", capability="read_file",
        reason="pass", evidence_ref="run-1",
    )
    assert manager.current_trust(registered_worker, "coder", "read_file") == TrustLevel.GUARDED
    assert manager.current_trust(registered_worker, "coder", "write_file") == TrustLevel.LOCKED
    assert manager.current_trust(registered_worker, "coder", None) == TrustLevel.LOCKED


def test_role_a_does_not_grant_role_b(manager, registered_worker):
    manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="pass", evidence_ref="run-1",
    )
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.GUARDED
    assert manager.current_trust(registered_worker, "reviewer") == TrustLevel.LOCKED


def test_capability_scoped_grant_does_not_leak_into_role_wide_scope(manager, registered_worker):
    manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", capability="read_file",
        reason="pass", evidence_ref="run-1",
    )
    # The role-wide (capability=None) scope is a *different* exact scope,
    # not a union of everything granted under it.
    assert manager.current_trust(registered_worker, "coder", None) == TrustLevel.LOCKED


# --- 11/12. no path to AUTO in Phase 7.2 ------------------------------------

def test_direct_locked_to_auto_rejected():
    assert not _is_allowed_transition(TrustLevel.LOCKED, TrustLevel.AUTO)


def test_direct_guarded_to_auto_rejected():
    assert not _is_allowed_transition(TrustLevel.GUARDED, TrustLevel.AUTO)


def test_public_api_has_no_auto_granting_method():
    public_methods = {name for name in dir(WorkerTrustManager) if not name.startswith("_")}
    assert "promote_to_auto" not in public_methods
    assert "grant_auto" not in public_methods
    # Every public promotion method's own transition table entry is
    # exact-checked too, so this isn't just a naming convention:
    assert _is_allowed_transition(TrustLevel.AUTO, TrustLevel.LOCKED)
    assert _is_allowed_transition(TrustLevel.AUTO, TrustLevel.GUARDED)


def test_promote_to_guarded_when_already_guarded_is_rejected(manager, registered_worker):
    manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="pass", evidence_ref="run-1",
    )
    result = manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="pass-again", evidence_ref="run-2",
    )
    assert not result.ok
    assert result.reason == "trust_transition_not_eligible_from_guarded"
    # No second event was written -- history has exactly the first grant.
    assert len(manager.history(registered_worker, "coder")) == 1


def test_downgrade_to_guarded_from_auto_directly_via_repo(db_conn, registered_worker, manager):
    """AUTO -> GUARDED is implemented for forward compatibility even
    though nothing in Phase 7.2's public API can ever produce an AUTO
    row -- proven here by seeding one directly through the repo, bypassing
    the manager entirely, then exercising the real downgrade method."""
    from code_slayer.store.db import transaction

    repo = WorkerTrustRepo(db_conn)
    with transaction(db_conn):
        repo.append_in_transaction(
            worker_id=registered_worker, role="coder", capability=None,
            from_level=TrustLevel.GUARDED.value, to_level=TrustLevel.AUTO.value,
            reason="seeded_for_test", evidence_ref=None,
            occurred_at="2026-01-01T00:00:00.000000Z",
        )
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.AUTO
    result = manager.downgrade_to_guarded(
        worker_id=registered_worker, role="coder", reason="policy_review",
    )
    assert result.ok
    assert result.level == TrustLevel.GUARDED
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.GUARDED


# --- 13. invalid trust value rejected at the persistence layer -------------

def test_invalid_trust_value_rejected_by_repo(db_conn, registered_worker):
    from code_slayer.store.db import transaction

    repo = WorkerTrustRepo(db_conn)
    with pytest.raises(RuntimeError):
        repo.append_in_transaction(
            worker_id=registered_worker, role="coder", capability=None,
            from_level=TrustLevel.LOCKED.value, to_level="SUPER_TRUSTED",
            reason="x", evidence_ref="y", occurred_at="2026-01-01T00:00:00.000000Z",
        )  # not even in a transaction yet -- also proves that guard
    with transaction(db_conn):
        with pytest.raises(ValueError):
            repo.append_in_transaction(
                worker_id=registered_worker, role="coder", capability=None,
                from_level=TrustLevel.LOCKED.value, to_level="SUPER_TRUSTED",
                reason="x", evidence_ref="y", occurred_at="2026-01-01T00:00:00.000000Z",
            )


# --- 14. missing/unknown worker follows existing FK/repository conventions -

def test_promote_for_unknown_worker_rejected_by_manager(manager):
    result = manager.promote_to_guarded(
        worker_id="ghost-worker", role="coder", reason="pass", evidence_ref="run-1",
    )
    assert not result.ok
    assert result.reason == "unknown_worker"


def test_repo_level_insert_for_unknown_worker_violates_foreign_key(db_conn):
    """Below the manager's own proactive check, the schema itself refuses
    an unknown worker_id via its REFERENCES workers(worker_id) — the same
    fail-closed-by-constraint convention this codebase already uses
    elsewhere (e.g. tasks(task_id) FKs)."""
    from code_slayer.store.db import transaction

    repo = WorkerTrustRepo(db_conn)
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(db_conn):
            repo.append_in_transaction(
                worker_id="no-such-worker", role="coder", capability=None,
                from_level=TrustLevel.LOCKED.value, to_level=TrustLevel.GUARDED.value,
                reason="x", evidence_ref="y", occurred_at="2026-01-01T00:00:00.000000Z",
            )


# --- 15. current trust derives from latest exact-scope event ---------------

def test_current_trust_derives_from_latest_exact_scope_event(manager, registered_worker):
    manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="pass", evidence_ref="run-1",
    )
    manager.downgrade_to_locked(worker_id=registered_worker, role="coder", reason="bad_call")
    manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="pass_again", evidence_ref="run-2",
    )
    assert manager.current_trust(registered_worker, "coder") == TrustLevel.GUARDED
    assert len(manager.history(registered_worker, "coder")) == 3


# --- concurrency: two connections racing for the same scope ---------------

def test_concurrent_promote_exactly_one_winner(tmp_path):
    """Two independent connections attempting to promote the same exact
    scope: the in-transaction re-check of current_trust() means the
    second one sees the first's already-committed GUARDED and is denied
    -- never a second GUARDED event, never split/contradictory history.
    Mirrors `tests/unit/test_lease_manager.py::
    test_concurrent_acquire_exactly_one_winner`'s established two-real-
    connection pattern."""
    from code_slayer.store.db import connect, migrate

    db_path = tmp_path / "state.db"
    conn_a = connect(db_path)
    migrate(conn_a)
    WorkersRepo(conn_a).register(worker_id="w1", kind="local-llm", network_class="local")
    conn_a.close()

    conn_a = connect(db_path)
    conn_b = connect(db_path)
    try:
        manager_a = WorkerTrustManager(conn_a)
        manager_b = WorkerTrustManager(conn_b)
        result_a = manager_a.promote_to_guarded(
            worker_id="w1", role="coder", reason="pass", evidence_ref="run-a",
        )
        result_b = manager_b.promote_to_guarded(
            worker_id="w1", role="coder", reason="pass", evidence_ref="run-b",
        )
        outcomes = {result_a.ok, result_b.ok}
        assert outcomes == {True, False}
        history = WorkerTrustRepo(conn_a).history_for_scope("w1", "coder", None)
        assert len(history) == 1  # exactly one GUARDED event, never two
        assert history[0].to_level == "GUARDED"
    finally:
        conn_a.close()
        conn_b.close()


# --- audit: exactly one appropriate audit record per successful transition -

def test_audit_event_emitted_exactly_once_per_successful_transition(
    db_conn, registered_worker, manager,
):
    from code_slayer.audit.events import EventType

    manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="pass", evidence_ref="run-1",
    )
    rows = db_conn.execute(
        "SELECT * FROM audit_events WHERE event_type = ?",
        (EventType.WORKER_TRUST_CHANGED.value,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_id"] == registered_worker


def test_no_audit_event_written_for_a_denied_transition(db_conn, registered_worker, manager):
    from code_slayer.audit.events import EventType

    manager.promote_to_guarded(
        worker_id=registered_worker, role="coder", reason="pass", evidence_ref=None,
    )
    rows = db_conn.execute(
        "SELECT * FROM audit_events WHERE event_type = ?",
        (EventType.WORKER_TRUST_CHANGED.value,),
    ).fetchall()
    assert len(rows) == 0


# --- WorkersRepo: minimal registration -------------------------------------

def test_workers_repo_register_is_idempotent(db_conn):
    repo = WorkersRepo(db_conn)
    first = repo.register(worker_id="w2", kind="local-llm", network_class="local")
    second = repo.register(worker_id="w2", kind="local-llm", network_class="local")
    assert first == second
    assert repo.get("w2") is not None


def test_workers_repo_rejects_unknown_network_class(db_conn):
    with pytest.raises(ValueError):
        WorkersRepo(db_conn).register(worker_id="w3", kind="local-llm", network_class="satellite")
