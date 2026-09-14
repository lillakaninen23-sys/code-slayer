"""Integration-level proof that lease liveness/quiescence gating holds
against *real* process evidence — not the deterministic fakes used in
`tests/unit/test_lease_manager.py`. Covers a genuinely separate OS
process (not just a different logical `worker_id`/session under this
test process's own pid), the real `/proc`-derived start-time comparison
defeating a simulated pid reuse, and quiescence surviving a reconnect (a
fresh `LeaseManager`/connection standing in for a process restart) —
all using the production, uninjected `liveness_fn`."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from code_slayer.lease.liveness import Liveness, check_process_liveness, process_start_time
from code_slayer.lease.manager import LeaseManager
from code_slayer.policy.engine import Decision
from code_slayer.store import db as db_module
from code_slayer.store.lease_repo import LeaseRepo, LeaseStatus
from code_slayer.store.task_repo import TaskRepo

pytestmark = pytest.mark.skipif(not os.path.isdir("/proc"), reason="requires /proc (Linux)")

_TTL = 0.05
_PAST_TTL_SLEEP = 0.3


@pytest.fixture
def task(db_conn):
    return TaskRepo(db_conn).create(
        description="d", repo_root="/r", repo_id="repo-1", worktree_id="wt-real",
    )


# --- liveness.py against real processes, no fakes ---------------------

def test_process_start_time_of_self_is_recoverable_and_stable():
    a = process_start_time(os.getpid())
    b = process_start_time(os.getpid())
    assert a is not None
    assert a == b


def test_check_process_liveness_alive_for_self():
    pid = os.getpid()
    assert check_process_liveness(pid, process_start_time(pid)) == Liveness.ALIVE


def test_check_process_liveness_unknown_without_a_recorded_start_time():
    assert check_process_liveness(os.getpid(), None) == Liveness.UNKNOWN


def test_check_process_liveness_gone_once_a_real_subprocess_has_exited():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    started = process_start_time(proc.pid)
    proc.wait(timeout=5)
    assert check_process_liveness(proc.pid, started) == Liveness.GONE


def test_check_process_liveness_detects_simulated_pid_reuse():
    """A currently-alive pid whose *recorded* start time does not match
    its *current* one must never be reported ALIVE — this is exactly
    what defeats pid reuse: a bare pid number the OS may have reassigned
    means nothing without also matching its start time."""
    pid = os.getpid()
    real_start = process_start_time(pid)
    assert real_start is not None
    fabricated_earlier_start = "2000-01-01T00:00:00.000000Z"  # a different, older "process"
    assert check_process_liveness(pid, fabricated_earlier_start) == Liveness.GONE


# --- LeaseManager against a real, separate, now-exited OS process -----

_ACQUIRE_SCRIPT = """
import sys
from code_slayer.lease.manager import LeaseManager
from code_slayer.store import db as db_module

conn = db_module.connect(sys.argv[1])
result = LeaseManager(conn, ttl_seconds=0.05).acquire(
    worktree_id=sys.argv[2], task_id=sys.argv[3],
    worker_id="child-process", worker_session_id="child-session",
)
assert result.decision.name == "ALLOW", result.reason
"""


def test_acquire_reclaims_from_a_real_process_proven_gone_via_proc(db_conn, task, tmp_path):
    """The strongest available proof of the QUIESCING cascade: the "old
    owner" here is a genuinely separate OS process (not a fake, and not
    merely a different `worker_id` under this test process's own pid).
    Once it has actually exited, a fresh `acquire()` using the *real*,
    uninjected liveness check must reclaim the lease — entering and
    resolving `QUIESCING` against real `/proc` evidence, not a
    deterministic stand-in."""
    db_path = tmp_path / "state.db"
    # Run the acquisition in a real, separate process against the same
    # database file, so its recorded worker_pid is a pid this test
    # process never held. The child needs `code_slayer` importable even
    # when this interpreter's own sys.path insertion (whatever provides
    # it for the parent) is not inherited by a bare subprocess.
    src_dir = str(Path(__file__).resolve().parents[2] / "src")
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = os.pathsep.join(
        [src_dir, *([child_env["PYTHONPATH"]] if child_env.get("PYTHONPATH") else [])],
    )
    subprocess.run(
        [sys.executable, "-c", _ACQUIRE_SCRIPT, str(db_path), task.worktree_id, task.task_id],
        check=True, timeout=10, env=child_env,
    )
    lease_row = LeaseRepo(db_conn).get(task.worktree_id)
    assert lease_row is not None
    assert lease_row.status == LeaseStatus.ACTIVE
    assert lease_row.worker_id == "child-process"
    child_pid = lease_row.worker_pid

    # The child process has already exited (subprocess.run waited for
    # it); give /proc a brief moment in case teardown lags reaping.
    for _ in range(50):
        if check_process_liveness(child_pid, lease_row.worker_pid_started_at) == Liveness.GONE:
            break
        time.sleep(0.05)
    else:
        pytest.fail("child process pid never became provably GONE via /proc")

    # The child's own heartbeat_at was stamped part-way through its (very
    # short-lived) run; make sure real elapsed time has since exceeded
    # the tiny TTL regardless of how fast that run happened to be.
    time.sleep(_PAST_TTL_SLEEP)

    # A real, uninjected LeaseManager — the default liveness_fn, exactly
    # as production code uses it — must now reclaim the lease in one
    # acquire() call: ACTIVE(expired) -> QUIESCING -> EXPIRED -> ACTIVE(2).
    manager = LeaseManager(db_conn, ttl_seconds=_TTL)
    result = manager.acquire(
        worktree_id=task.worktree_id, task_id=task.task_id,
        worker_id="parent-process", worker_session_id="parent-session",
    )
    assert result.decision == Decision.ALLOW, result.reason
    assert result.handle.generation == 2


def test_acquire_denies_and_quiesces_against_a_still_alive_real_process(db_conn, task):
    """The mirror case, using this test process's own very real, very
    alive pid as the recorded owner (no fake `liveness_fn` at all): a
    second session's acquire() attempt after TTL expiry must deny and
    park in QUIESCING, never take over — because the real check
    correctly reports the recorded pid as ALIVE."""
    manager = LeaseManager(db_conn, ttl_seconds=_TTL)
    first = manager.acquire(
        worktree_id=task.worktree_id, task_id=task.task_id,
        worker_id="a", worker_session_id="sa",
    )
    assert first.decision == Decision.ALLOW
    time.sleep(_PAST_TTL_SLEEP)
    result = manager.acquire(
        worktree_id=task.worktree_id, task_id=task.task_id,
        worker_id="b", worker_session_id="sb",
    )
    assert result.decision == Decision.DENY
    assert result.reason == "quiescing_owner_still_alive"
    lease_row = LeaseRepo(db_conn).get(task.worktree_id)
    assert lease_row.status == LeaseStatus.QUIESCING
    assert lease_row.generation == 1  # no new epoch minted


def test_restart_while_quiescing_resumes_from_durable_state_against_real_process(
    db_conn, task, tmp_path,
):
    """Integration-level restart proof: a fresh connection (standing in
    for a process restart) reopening the same durable state after
    quiescence began must resume from exactly that QUIESCING state —
    never resurrect the old epoch's authority, and never need the
    cascade to restart from ACTIVE."""
    manager = LeaseManager(db_conn, ttl_seconds=_TTL)
    manager.acquire(
        worktree_id=task.worktree_id, task_id=task.task_id,
        worker_id="a", worker_session_id="sa",
    )
    time.sleep(_PAST_TTL_SLEEP)
    denied = manager.acquire(
        worktree_id=task.worktree_id, task_id=task.task_id,
        worker_id="b", worker_session_id="sb",
    )
    assert denied.decision == Decision.DENY
    lease_row = LeaseRepo(db_conn).get(task.worktree_id)
    assert lease_row.status == LeaseStatus.QUIESCING

    reopened = db_module.connect(tmp_path / "state.db")
    try:
        resumed = LeaseRepo(reopened).get(task.worktree_id)
        assert resumed.status == LeaseStatus.QUIESCING
        assert resumed.generation == 1
    finally:
        reopened.close()
