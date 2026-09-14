"""Durable worktree lease/fencing: acquire, renew, release, takeover,
expiry, and malformed-data handling — all against a real SQLite connection."""

from __future__ import annotations

import pytest

from code_slayer.lease.manager import LeaseHandle, LeaseManager
from code_slayer.policy.engine import Decision
from code_slayer.store.lease_repo import LeaseRepo, LeaseStatus
from code_slayer.store.task_repo import TaskRepo


class FakeClock:
    """A controllable clock so expiry can be tested without real sleeping."""

    def __init__(self, start="2026-01-01T00:00:00.000000Z"):
        self._now = start

    def __call__(self) -> str:
        return self._now

    def advance(self, seconds: float) -> None:
        from datetime import UTC, datetime, timedelta

        current = datetime.strptime(self._now, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        self._now = (current + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def task_id(db_conn) -> str:
    task = TaskRepo(db_conn).create(description="d", repo_root="/r", repo_id="repo-1",
                                     worktree_id="wt-1")
    return task.task_id


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def manager(db_conn, clock):
    return LeaseManager(db_conn, ttl_seconds=60.0, now_fn=clock)


def acquire(manager, task_id, worker="w1", session="s1", worktree="wt-1"):
    return manager.acquire(
        worktree_id=worktree, task_id=task_id, worker_id=worker, worker_session_id=session,
    )


# --- acquire -----------------------------------------------------------

def test_acquire_fresh_task_succeeds_with_generation_one(manager, task_id):
    result = acquire(manager, task_id)
    assert result.decision == Decision.ALLOW
    assert result.handle.generation == 1
    assert result.handle.worktree_id == "wt-1"


def test_acquire_already_leased_denies_other_session(manager, task_id):
    acquire(manager, task_id)
    result = acquire(manager, task_id, worker="w2", session="s2")
    assert result.decision == Decision.DENY
    assert result.reason == "lease_held_and_not_expired"


def test_acquire_by_current_holder_denies_use_renew(manager, task_id):
    acquire(manager, task_id)
    result = acquire(manager, task_id)
    assert result.decision == Decision.DENY
    assert result.reason == "already_held_by_caller_use_renew"


def test_acquire_after_release_succeeds_with_incremented_generation(manager, task_id):
    first = acquire(manager, task_id)
    manager.release(first.handle)
    second = acquire(manager, task_id, worker="w2", session="s2")
    assert second.decision == Decision.ALLOW
    assert second.handle.generation == 2


def test_acquire_after_expiry_takes_over(manager, task_id, clock):
    first = acquire(manager, task_id)
    clock.advance(120)  # past the 60s ttl
    second = acquire(manager, task_id, worker="w2", session="s2")
    assert second.decision == Decision.ALLOW
    assert second.handle.generation == 2
    # The old handle is now permanently stale.
    assert not manager.is_current(first.handle)


def test_acquire_not_yet_expired_denies_takeover(manager, task_id, clock):
    first = acquire(manager, task_id)
    clock.advance(30)  # under the 60s ttl
    second = acquire(manager, task_id, worker="w2", session="s2")
    assert second.decision == Decision.DENY
    assert manager.is_current(first.handle)


@pytest.mark.parametrize("field", ["worktree_id", "task_id", "worker_id", "worker_session_id"])
def test_acquire_rejects_malformed_request(manager, task_id, field):
    kwargs = dict(worktree_id="wt-1", task_id=task_id, worker_id="w1", worker_session_id="s1")
    kwargs[field] = ""
    result = manager.acquire(**kwargs)
    assert result.decision == Decision.DENY
    assert result.reason == "malformed_lease_request"


def test_acquire_denies_on_malformed_persisted_lease(manager, db_conn, task_id):
    # Corrupt the row directly, bypassing LeaseManager entirely.
    acquire(manager, task_id)
    db_conn.execute("UPDATE worker_leases SET generation = -1 WHERE worktree_id = 'wt-1'")
    result = acquire(manager, task_id, worker="w2", session="s2")
    assert result.decision == Decision.DENY
    assert result.reason == "malformed_persisted_lease"


def test_concurrent_acquire_exactly_one_winner(tmp_path, task_id):
    """Two separate connections attempting acquire at (nearly) the same
    time: SQLite's own write-lock serializes them into one winner, one
    deterministic denial — never split ownership."""
    from code_slayer.store.db import connect, migrate

    db_path = tmp_path / "state.db"
    conn_a = connect(db_path)
    migrate(conn_a)
    TaskRepo(conn_a).create(task_id="shared-task", description="d", repo_root="/r",
                            repo_id="repo-1", worktree_id="wt-shared")
    conn_a.close()

    conn_a = connect(db_path)
    conn_b = connect(db_path)
    try:
        manager_a = LeaseManager(conn_a, ttl_seconds=60.0)
        manager_b = LeaseManager(conn_b, ttl_seconds=60.0)
        result_a = manager_a.acquire(
            worktree_id="wt-shared", task_id="shared-task", worker_id="a", worker_session_id="sa",
        )
        result_b = manager_b.acquire(
            worktree_id="wt-shared", task_id="shared-task", worker_id="b", worker_session_id="sb",
        )
        decisions = {result_a.decision, result_b.decision}
        assert decisions == {Decision.ALLOW, Decision.DENY}
        winner = result_a if result_a.decision == Decision.ALLOW else result_b
        assert winner.handle.generation == 1
        row = LeaseRepo(conn_a).get("wt-shared")
        assert (row.worker_id, row.worker_session_id) in (("a", "sa"), ("b", "sb"))
    finally:
        conn_a.close()
        conn_b.close()


# --- renew ---------------------------------------------------------------

def test_renew_current_holder_succeeds(manager, task_id, clock):
    acquired = acquire(manager, task_id)
    clock.advance(50)  # still under ttl, but close
    result = manager.renew(acquired.handle)
    assert result.decision == Decision.ALLOW
    # Renewal resets the clock: further time from here must not expire it
    # for another full ttl window.
    clock.advance(50)
    assert manager.is_current(acquired.handle)


def test_renew_stale_token_denied(manager, task_id):
    first = acquire(manager, task_id)
    manager.release(first.handle)
    second = acquire(manager, task_id, worker="w2", session="s2")
    result = manager.renew(first.handle)
    assert result.decision == Decision.DENY
    assert result.reason == "stale_fencing_token"
    assert second.decision == Decision.ALLOW  # unaffected


def test_renew_wrong_session_same_worktree_denied(manager, task_id):
    acquired = acquire(manager, task_id)
    forged = LeaseHandle("wt-1", task_id, "w1", "wrong-session", acquired.handle.generation,
                          acquired.handle.acquired_at)
    result = manager.renew(forged)
    assert result.decision == Decision.DENY
    assert result.reason == "stale_fencing_token"


def test_renew_after_expiry_and_takeover_denied(manager, task_id, clock):
    first = acquire(manager, task_id)
    clock.advance(120)
    acquire(manager, task_id, worker="w2", session="s2")
    result = manager.renew(first.handle)
    assert result.decision == Decision.DENY
    assert result.reason == "stale_fencing_token"


def test_renew_malformed_handle_denied(manager):
    result = manager.renew("not-a-handle")  # type: ignore[arg-type]
    assert result.decision == Decision.DENY
    assert result.reason == "malformed_lease_handle"


def test_renew_nonexistent_lease_denied(manager, task_id):
    handle = LeaseHandle("wt-none", task_id, "w1", "s1", 1, "2026-01-01T00:00:00.000000Z")
    result = manager.renew(handle)
    assert result.decision == Decision.DENY
    assert result.reason == "no_lease"


# --- release ---------------------------------------------------------------

def test_release_current_holder_succeeds(manager, task_id):
    acquired = acquire(manager, task_id)
    result = manager.release(acquired.handle)
    assert result.decision == Decision.ALLOW
    assert not manager.is_current(acquired.handle)


def test_release_wrong_owner_denied(manager, task_id):
    acquired = acquire(manager, task_id)
    forged = LeaseHandle("wt-1", task_id, "w9", "s9", acquired.handle.generation,
                          acquired.handle.acquired_at)
    result = manager.release(forged)
    assert result.decision == Decision.DENY
    assert result.reason == "stale_fencing_token"
    assert manager.is_current(acquired.handle)  # untouched


def test_release_stale_token_cannot_clear_newer_owner(manager, task_id):
    first = acquire(manager, task_id)
    manager.release(first.handle)
    second = acquire(manager, task_id, worker="w2", session="s2")
    result = manager.release(first.handle)  # A tries to release again, stale
    assert result.decision == Decision.DENY
    assert manager.is_current(second.handle)  # B's lease is untouched


def test_repeated_release_is_denied_not_a_silent_noop(manager, task_id):
    acquired = acquire(manager, task_id)
    manager.release(acquired.handle)
    result = manager.release(acquired.handle)
    assert result.decision == Decision.DENY
    assert result.reason == "lease_not_active"


def test_release_does_not_touch_task_state(manager, task_id, db_conn):
    acquired = acquire(manager, task_id)
    before = TaskRepo(db_conn).get(task_id).state
    manager.release(acquired.handle)
    after = TaskRepo(db_conn).get(task_id).state
    assert before == after == "CREATED"


# --- takeover / fencing invariants ------------------------------------------

def test_token_strictly_increases_across_takeovers(manager, task_id, clock):
    generations = []
    for i in range(4):
        result = manager.acquire(
            worktree_id="wt-1", task_id=task_id, worker_id=f"w{i}", worker_session_id=f"s{i}",
        )
        generations.append(result.handle.generation)
        clock.advance(120)
    assert generations == [1, 2, 3, 4]


def test_old_token_permanently_rejected_after_multiple_takeovers(manager, task_id, clock):
    first = acquire(manager, task_id)
    for i in range(2, 4):
        clock.advance(120)
        acquire(manager, task_id, worker=f"w{i}", session=f"s{i}")
    assert not manager.is_current(first.handle)
    assert manager.renew(first.handle).decision == Decision.DENY


# --- expire_if_stale ---------------------------------------------------------

def test_expire_if_stale_marks_expired_without_new_owner(manager, task_id, clock, db_conn):
    acquired = acquire(manager, task_id)
    clock.advance(120)
    result = manager.expire_if_stale("wt-1")
    assert result.decision == Decision.ALLOW
    row = LeaseRepo(db_conn).get("wt-1")
    assert row.status == LeaseStatus.EXPIRED
    assert row.worker_id == "w1"  # no new owner yet
    assert not manager.is_current(acquired.handle)


def test_expire_if_stale_denies_when_not_yet_expired(manager, task_id):
    acquire(manager, task_id)
    result = manager.expire_if_stale("wt-1")
    assert result.decision == Decision.DENY
    assert result.reason == "not_expired"


def test_expire_if_stale_no_lease_denied(manager):
    result = manager.expire_if_stale("wt-none")
    assert result.decision == Decision.DENY
    assert result.reason == "no_lease"


# --- malformed persisted data (broader) -------------------------------------

def test_is_current_false_for_malformed_row(manager, task_id, db_conn):
    acquired = acquire(manager, task_id)
    db_conn.execute(
        "UPDATE worker_leases SET heartbeat_at = 'not-a-timestamp' WHERE worktree_id = 'wt-1'",
    )
    assert not manager.is_current(acquired.handle)


def test_upsert_requires_open_transaction(db_conn):
    with pytest.raises(RuntimeError, match="open write transaction"):
        LeaseRepo(db_conn).upsert_in_transaction(
            worktree_id="wt-x", task_id="t", worker_id="w", worker_session_id="s",
            generation=1, acquired_at="2026-01-01T00:00:00.000000Z",
            heartbeat_at="2026-01-01T00:00:00.000000Z", status=LeaseStatus.ACTIVE,
            worker_pid=None, worker_pid_started_at=None, checkpoint_id=None,
        )


def test_lease_repo_rejects_unknown_status(db_conn):
    from code_slayer.store.db import transaction

    with pytest.raises(ValueError), transaction(db_conn):
        LeaseRepo(db_conn).upsert_in_transaction(
            worktree_id="wt-x", task_id="t", worker_id="w", worker_session_id="s",
            generation=1, acquired_at="2026-01-01T00:00:00.000000Z",
            heartbeat_at="2026-01-01T00:00:00.000000Z", status="BOGUS",
            worker_pid=None, worker_pid_started_at=None, checkpoint_id=None,
        )
