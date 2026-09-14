"""Git fixtures may mutate temporary repositories; inspection runtime may not."""

import hashlib
import os
import subprocess

from code_slayer.lease.manager import LeaseHandle, LeaseManager


def git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(repo, message="fixture"):
    git(repo, "add", "--all")
    git(repo, "commit", "-qm", message)


def filesystem_snapshot(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            result[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            result[relative] = (path.stat().st_mode, hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def acquire_lease(
    conn, task, *, worker_id="test-worker", worker_session_id="test-session",
) -> LeaseHandle:
    """A valid, current lease for `task`'s worktree — Phase 4/5 executors
    require one; tests acquire it explicitly rather than fabricating a
    handle, so the same fencing checks production code runs are exercised."""
    result = LeaseManager(conn).acquire(
        worktree_id=task.worktree_id, task_id=task.task_id,
        worker_id=worker_id, worker_session_id=worker_session_id,
    )
    assert result.handle is not None, result.reason
    return result.handle
