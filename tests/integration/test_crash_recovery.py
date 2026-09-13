"""Subprocess-based crash test (Foundation Plan §15 requirement).

A process dying mid-transaction must never leave the database partially
applied: after reopening, the result must be fully old (nothing
committed) or fully new (everything committed) — never a partial task row
with no matching audit event, or vice versa.

The crashing process runs as a real, separate OS process (`os._exit`
skips Python and C cleanup entirely, which an in-process
`unittest.mock`-style simulation cannot faithfully reproduce) using the
same interpreter running pytest, with `src/` on `PYTHONPATH` so it needs
no installed package.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from code_slayer.store import db as db_module

_SRC = str(Path(__file__).resolve().parent.parent.parent / "src")

_SCRIPT = """
import sys
sys.path.insert(0, {src!r})
import os

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.store import db

conn = db.connect({db_path!r})
db.migrate(conn)

conn.execute("BEGIN IMMEDIATE")
conn.execute(
    "INSERT INTO tasks (task_id, description, repo_root, repo_id, worktree_id, "
    "created_at, updated_at, state) VALUES "
    "('crash-task', 'd', '/r', 'repo-1', 'wt-1', 'now', 'now', 'CREATED')"
)
AuditWriter(conn).append(
    task_id='crash-task', event_type=EventType.TASK_CREATED,
    actor_type='system', actor_id=None, payload={{}},
)

if {crash_after_commit}:
    conn.execute("COMMIT")

os._exit(1)  # no Python/C cleanup, no implicit commit, no atexit handlers
"""


def _run_crashing_subprocess(db_path: Path, *, crash_after_commit: bool) -> None:
    script = _SCRIPT.format(src=_SRC, db_path=str(db_path), crash_after_commit=crash_after_commit)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 1, (result.stdout, result.stderr)


def test_crash_before_commit_leaves_fully_old_state(tmp_path):
    db_path = tmp_path / "state.db"
    conn = db_module.connect(db_path)
    db_module.migrate(conn)
    conn.close()

    _run_crashing_subprocess(db_path, crash_after_commit=False)

    reopened = db_module.connect(db_path)
    try:
        task_row = reopened.execute(
            "SELECT * FROM tasks WHERE task_id = 'crash-task'"
        ).fetchone()
        audit_row = reopened.execute(
            "SELECT * FROM audit_events WHERE task_id = 'crash-task'"
        ).fetchone()
        assert task_row is None, "an uncommitted task must not be visible after crash"
        assert audit_row is None, "an uncommitted audit event must not be visible after crash"
    finally:
        reopened.close()


def test_crash_after_commit_leaves_fully_new_state(tmp_path):
    db_path = tmp_path / "state.db"
    conn = db_module.connect(db_path)
    db_module.migrate(conn)
    conn.close()

    _run_crashing_subprocess(db_path, crash_after_commit=True)

    reopened = db_module.connect(db_path)
    try:
        task_row = reopened.execute(
            "SELECT * FROM tasks WHERE task_id = 'crash-task'"
        ).fetchone()
        audit_row = reopened.execute(
            "SELECT * FROM audit_events WHERE task_id = 'crash-task'"
        ).fetchone()
        assert task_row is not None, "a committed task must survive the crash"
        assert audit_row is not None, "a committed audit event must survive the crash"
        assert task_row["state"] == "CREATED"
    finally:
        reopened.close()


@pytest.mark.parametrize("crash_after_commit", [False, True])
def test_reopened_database_is_still_fully_usable_after_a_crash(tmp_path, crash_after_commit):
    """A crash of any other process must never corrupt the file for the
    next process that opens it — migration must still be a no-op, and
    ordinary writes must still work."""
    db_path = tmp_path / "state.db"
    conn = db_module.connect(db_path)
    db_module.migrate(conn)
    conn.close()

    _run_crashing_subprocess(db_path, crash_after_commit=crash_after_commit)

    reopened = db_module.connect(db_path)
    try:
        assert db_module.migrate(reopened) == db_module.known_schema_version()
        with db_module.transaction(reopened):
            reopened.execute(
                "INSERT INTO tasks (task_id, description, repo_root, repo_id, "
                "worktree_id, created_at, updated_at, state) VALUES "
                "('after-crash-task', 'd', '/r', 'repo-1', 'wt-2', 'now', 'now', 'CREATED')"
            )
        row = reopened.execute(
            "SELECT * FROM tasks WHERE task_id = 'after-crash-task'"
        ).fetchone()
        assert row is not None
    finally:
        reopened.close()
