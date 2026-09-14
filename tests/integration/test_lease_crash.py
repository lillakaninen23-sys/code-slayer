"""Real subprocess crashes around lease acquire/renew/release/takeover.

Every `LeaseManager` write is one `BEGIN IMMEDIATE` transaction with no
external side effect (unlike Phase 4/5 tool operations, a lease is purely
Code Slayer's own SQLite state) — so there is no separate journal to
reconcile here: a crash mid-transaction rolls back entirely, and reopening
the database must show either the exact pre-transaction state or the
complete post-transaction state, never anything in between, and never a
resurrected stale holder.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from code_slayer.lease.manager import LeaseManager
from code_slayer.policy.engine import Decision
from code_slayer.store.db import connect, migrate
from code_slayer.store.lease_repo import LeaseRepo
from code_slayer.store.task_repo import TaskRepo

_SRC = str(Path(__file__).resolve().parents[2] / "src")


def _run(script: str, db_path: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script, _SRC, str(db_path), *extra],
        capture_output=True, text=True, timeout=15,
    )


_CRASH_DURING_ACQUIRE = """
import os, sys
sys.path.insert(0, sys.argv[1])
from code_slayer.store.db import connect
from code_slayer.lease.manager import LeaseManager
from code_slayer.store import lease_repo
conn = connect(sys.argv[2])
real = lease_repo.LeaseRepo.upsert_in_transaction
def crash(self, **kwargs):
    os._exit(74)
lease_repo.LeaseRepo.upsert_in_transaction = crash
LeaseManager(conn).acquire(
    worktree_id=sys.argv[3], task_id=sys.argv[4], worker_id="w", worker_session_id="s",
)
os._exit(1)
"""

_CRASH_DURING_RENEW = """
import os, sys
sys.path.insert(0, sys.argv[1])
from code_slayer.store.db import connect
from code_slayer.lease.manager import LeaseHandle, LeaseManager
from code_slayer.store import lease_repo
conn = connect(sys.argv[2])
real = lease_repo.LeaseRepo.update_status_in_transaction
def crash(self, *a, **kw):
    os._exit(74)
lease_repo.LeaseRepo.update_status_in_transaction = crash
handle = LeaseHandle(sys.argv[3], sys.argv[4], "w1", "s1", int(sys.argv[5]), "")
LeaseManager(conn).renew(handle)
os._exit(1)
"""

_CRASH_DURING_RELEASE = """
import os, sys
sys.path.insert(0, sys.argv[1])
from code_slayer.store.db import connect
from code_slayer.lease.manager import LeaseHandle, LeaseManager
from code_slayer.store import lease_repo
conn = connect(sys.argv[2])
real = lease_repo.LeaseRepo.update_status_in_transaction
def crash(self, *a, **kw):
    os._exit(74)
lease_repo.LeaseRepo.update_status_in_transaction = crash
handle = LeaseHandle(sys.argv[3], sys.argv[4], "w1", "s1", int(sys.argv[5]), "")
LeaseManager(conn).release(handle)
os._exit(1)
"""

_CRASH_DURING_TAKEOVER = """
import os, sys
sys.path.insert(0, sys.argv[1])
from code_slayer.store.db import connect
from code_slayer.lease.manager import LeaseManager
from code_slayer.store import lease_repo
conn = connect(sys.argv[2])
real = lease_repo.LeaseRepo.upsert_in_transaction
def crash(self, **kwargs):
    os._exit(74)
lease_repo.LeaseRepo.upsert_in_transaction = crash
LeaseManager(conn).acquire(
    worktree_id=sys.argv[3], task_id=sys.argv[4], worker_id="taker", worker_session_id="t",
)
os._exit(1)
"""


def _task(conn, worktree_id="wt-1"):
    return TaskRepo(conn).create(
        description="d", repo_root="/r", repo_id="repo-1", worktree_id=worktree_id,
    )


def test_crash_during_fresh_acquire_leaves_no_lease_row(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    migrate(conn)
    task = _task(conn)
    conn.close()

    result = _run(_CRASH_DURING_ACQUIRE, db_path, "wt-1", task.task_id)
    assert result.returncode == 74, result.stderr

    reopened = connect(db_path)
    try:
        assert LeaseRepo(reopened).get("wt-1") is None
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # A subsequent acquire proceeds normally, as if nothing happened.
        fresh = LeaseManager(reopened).acquire(
            worktree_id="wt-1", task_id=task.task_id, worker_id="w2", worker_session_id="s2",
        )
        assert fresh.decision == Decision.ALLOW
        assert fresh.handle.generation == 1
    finally:
        reopened.close()


def test_crash_during_takeover_leaves_original_lease_intact(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    migrate(conn)
    task = _task(conn)
    manager = LeaseManager(conn)
    first = manager.acquire(
        worktree_id="wt-1", task_id=task.task_id, worker_id="w1", worker_session_id="s1",
    )
    manager.release(first.handle)  # eligible for takeover
    conn.close()

    result = _run(_CRASH_DURING_TAKEOVER, db_path, "wt-1", task.task_id)
    assert result.returncode == 74, result.stderr

    reopened = connect(db_path)
    try:
        row = LeaseRepo(reopened).get("wt-1")
        # The takeover transaction rolled back entirely: the row is
        # exactly as the (released) first session left it, never a
        # half-written "taker" row.
        assert row.worker_id == "w1"
        assert row.generation == 1
        assert row.status == "RELEASED"
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # A fresh takeover attempt now succeeds cleanly.
        retry = LeaseManager(reopened).acquire(
            worktree_id="wt-1", task_id=task.task_id, worker_id="taker", worker_session_id="t",
        )
        assert retry.decision == Decision.ALLOW
        assert retry.handle.generation == 2
    finally:
        reopened.close()


def test_crash_during_renew_leaves_lease_at_its_prior_heartbeat(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    migrate(conn)
    task = _task(conn)
    acquired = LeaseManager(conn).acquire(
        worktree_id="wt-1", task_id=task.task_id, worker_id="w1", worker_session_id="s1",
    )
    before = LeaseRepo(conn).get("wt-1")
    conn.close()

    result = _run(
        _CRASH_DURING_RENEW, db_path, "wt-1", task.task_id, str(acquired.handle.generation),
    )
    assert result.returncode == 74, result.stderr

    reopened = connect(db_path)
    try:
        row = LeaseRepo(reopened).get("wt-1")
        assert row.heartbeat_at == before.heartbeat_at  # renew never committed
        assert row.status == "ACTIVE"
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # The holder's own handle is still exactly current -- a crashed
        # renew does not revoke authority (generation is unchanged).
        assert LeaseManager(reopened).is_current(acquired.handle)
    finally:
        reopened.close()


def test_crash_during_release_leaves_lease_active(tmp_path):
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    migrate(conn)
    task = _task(conn)
    acquired = LeaseManager(conn).acquire(
        worktree_id="wt-1", task_id=task.task_id, worker_id="w1", worker_session_id="s1",
    )
    conn.close()

    result = _run(
        _CRASH_DURING_RELEASE, db_path, "wt-1", task.task_id, str(acquired.handle.generation),
    )
    assert result.returncode == 74, result.stderr

    reopened = connect(db_path)
    try:
        row = LeaseRepo(reopened).get("wt-1")
        assert row.status == "ACTIVE"  # release never committed
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert LeaseManager(reopened).is_current(acquired.handle)
        # A takeover attempt by someone else must still be refused.
        other = LeaseManager(reopened).acquire(
            worktree_id="wt-1", task_id=task.task_id, worker_id="w2", worker_session_id="s2",
        )
        assert other.decision == Decision.DENY
    finally:
        reopened.close()
