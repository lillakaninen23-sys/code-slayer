"""Durable worktree lease records (Phase 6, schema v1's pre-existing
`worker_leases` table — Foundation Plan §12, schema-only since Phase 1).

`worktree_id` is the table's primary key: the lease is the unit of
mutation ownership Phase 1's own `tasks.worktree_id` comment already
named ("the unit of mutation ownership (future lease manager)"), matching
Foundation invariant INV-2 (at most one non-terminal task per worktree) —
one worktree, one current lease, one current ownership epoch.

This module is a thin, transactional persistence primitive only. It does
not decide acquire/renew/release/takeover legality — `lease.manager.
LeaseManager` does, using the CAS-style helpers here under the same
`store.db.transaction()` discipline every other repository in this
codebase already follows.
"""

from __future__ import annotations

import sqlite3

from code_slayer.store.models import WorkerLease


class LeaseStatus:
    ACTIVE = "ACTIVE"
    QUIESCING = "QUIESCING"
    EXPIRED = "EXPIRED"
    RELEASED = "RELEASED"

    ALL = frozenset({ACTIVE, QUIESCING, EXPIRED, RELEASED})


class LeaseRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, worktree_id: str) -> WorkerLease | None:
        row = self._conn.execute(
            "SELECT * FROM worker_leases WHERE worktree_id = ?", (worktree_id,),
        ).fetchone()
        return _row_to_lease(row) if row is not None else None

    def upsert_in_transaction(
        self,
        *,
        worktree_id: str,
        task_id: str,
        worker_id: str,
        worker_session_id: str,
        generation: int,
        acquired_at: str,
        heartbeat_at: str,
        status: str,
        worker_pid: int | None,
        worker_pid_started_at: str | None,
        checkpoint_id: str | None,
    ) -> WorkerLease:
        """Replace (or create) the one lease row for `worktree_id`.

        Internal persistence primitive: the caller (`LeaseManager`) has
        already decided, under an open write transaction, that this
        exact write is legal — a fresh acquisition, a takeover, or a
        status change to an already-verified-current row. This method
        does not itself re-check ownership.
        """
        if not self._conn.in_transaction:
            raise RuntimeError("lease persistence requires an open write transaction")
        if status not in LeaseStatus.ALL:
            raise ValueError(f"unknown lease status: {status!r}")
        self._conn.execute(
            "INSERT INTO worker_leases "
            "(worktree_id, task_id, worker_id, worker_session_id, generation, "
            " acquired_at, heartbeat_at, status, worker_pid, worker_pid_started_at, "
            " checkpoint_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(worktree_id) DO UPDATE SET "
            "task_id = excluded.task_id, worker_id = excluded.worker_id, "
            "worker_session_id = excluded.worker_session_id, "
            "generation = excluded.generation, acquired_at = excluded.acquired_at, "
            "heartbeat_at = excluded.heartbeat_at, status = excluded.status, "
            "worker_pid = excluded.worker_pid, "
            "worker_pid_started_at = excluded.worker_pid_started_at, "
            "checkpoint_id = excluded.checkpoint_id",
            (
                worktree_id, task_id, worker_id, worker_session_id, generation,
                acquired_at, heartbeat_at, status, worker_pid, worker_pid_started_at,
                checkpoint_id,
            ),
        )
        return self.get(worktree_id)

    def update_status_in_transaction(
        self, worktree_id: str, *, status: str, heartbeat_at: str | None = None,
    ) -> WorkerLease:
        """Change only `status` (and optionally `heartbeat_at`) of the
        existing row — used for `renew`/`release`/`expire`, never for a
        new ownership epoch (see `upsert_in_transaction`)."""
        if not self._conn.in_transaction:
            raise RuntimeError("lease persistence requires an open write transaction")
        if status not in LeaseStatus.ALL:
            raise ValueError(f"unknown lease status: {status!r}")
        if heartbeat_at is not None:
            self._conn.execute(
                "UPDATE worker_leases SET status = ?, heartbeat_at = ? WHERE worktree_id = ?",
                (status, heartbeat_at, worktree_id),
            )
        else:
            self._conn.execute(
                "UPDATE worker_leases SET status = ? WHERE worktree_id = ?",
                (status, worktree_id),
            )
        lease = self.get(worktree_id)
        if lease is None:
            raise KeyError(worktree_id)
        return lease


def _row_to_lease(row: sqlite3.Row) -> WorkerLease:
    return WorkerLease(
        worktree_id=row["worktree_id"],
        task_id=row["task_id"],
        worker_id=row["worker_id"],
        worker_session_id=row["worker_session_id"],
        generation=row["generation"],
        acquired_at=row["acquired_at"],
        heartbeat_at=row["heartbeat_at"],
        status=row["status"],
        worker_pid=row["worker_pid"],
        worker_pid_started_at=row["worker_pid_started_at"],
        checkpoint_id=row["checkpoint_id"],
    )
