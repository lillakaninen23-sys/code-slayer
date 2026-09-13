"""Durable tool-operation journal: storage and repository API (Foundation
Plan §04/§10/§12/§15, Revision 2.1).

This is the write-ahead journal for every side effect SQLite cannot make
atomic with itself. Phase 1 implements only the storage and repository API
— no real mutating tool runs through it yet (no tool layer exists). The
shape here is what that future layer builds on without a schema change:
`lease_generation`/`child_pid` are already present, even though nothing
yet populates them with a real value (Phase 1 leaves `lease_generation`
nullable — see `migrations/0001_init.sql`).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from code_slayer.audit.canonical import canonical_json
from code_slayer.store.db import transaction
from code_slayer.store.models import ToolOperation


class OperationStatus:
    STARTED = "STARTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

    ALL = frozenset({STARTED, SUCCEEDED, FAILED, UNKNOWN})
    UNRESOLVED = frozenset({STARTED, UNKNOWN})


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def compute_request_hash(tool_name: str, params: dict[str, Any]) -> str:
    """A deterministic hash of a tool call's canonical input: the same
    `(tool_name, params)` always hashes the same, regardless of dict
    insertion order."""
    import hashlib

    return hashlib.sha256(
        canonical_json({"tool": tool_name, "params": params}).encode("utf-8")
    ).hexdigest()


class ToolOperationsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def start(self, **kwargs) -> ToolOperation:
        """Durably record STARTED before any external side effect."""
        with transaction(self._conn):
            return self.start_in_transaction(**kwargs)

    def start_in_transaction(
        self,
        *,
        task_id: str,
        worktree_id: str,
        worker_id: str,
        worker_session_id: str,
        tool_name: str,
        risk_class: str,
        request_hash: str,
        target_resource: str,
        lease_generation: int | None = None,
        before_evidence: str | None = None,
        operation_id: str | None = None,
    ) -> ToolOperation:
        """Durably record STARTED *before* the actual side effect runs."""
        operation_id = operation_id or str(uuid.uuid4())
        started_at = utcnow_iso()
        if not self._conn.in_transaction:
            raise RuntimeError("operation start requires an open write transaction")
        self._conn.execute(
            "INSERT INTO tool_operations "
            "(operation_id, task_id, worktree_id, worker_id, worker_session_id, "
            " lease_generation, tool_name, risk_class, request_hash, "
            " target_resource, started_at, status, before_evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id, task_id, worktree_id, worker_id, worker_session_id,
                lease_generation, tool_name, risk_class, request_hash,
                target_resource, started_at, OperationStatus.STARTED, before_evidence,
            ),
        )
        return self.get(operation_id)

    def record_child_pid(self, operation_id: str, pid: int, pid_started_at: str) -> ToolOperation:
        """Record the OS pid of a subprocess this operation spawned, so a
        future `QUIESCING` reconciliation (§12) can find and check it."""
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE tool_operations SET child_pid = ?, child_pid_started_at = ? "
                "WHERE operation_id = ?",
                (pid, pid_started_at, operation_id),
            )
        return self.get(operation_id)

    def finish(self, operation_id: str, **kwargs) -> ToolOperation:
        """Resolve the journal entry in its own transaction."""
        with transaction(self._conn):
            return self.finish_in_transaction(operation_id, **kwargs)

    def finish_in_transaction(
        self,
        operation_id: str,
        *,
        status: str,
        after_evidence: str | None = None,
        result: dict[str, Any] | None = None,
    ) -> ToolOperation:
        """Resolve a STARTED (or UNKNOWN) row to a terminal status."""
        if status not in OperationStatus.ALL:
            raise ValueError(f"unknown status: {status!r}")
        finished_at = utcnow_iso()
        if not self._conn.in_transaction:
            raise RuntimeError("operation finish requires an open write transaction")
        self._conn.execute(
            "UPDATE tool_operations SET status = ?, finished_at = ?, "
            "after_evidence = ?, result_json = ? WHERE operation_id = ?",
            (
                status, finished_at, after_evidence,
                json.dumps(result) if result is not None else None,
                operation_id,
            ),
        )
        return self.get(operation_id)

    def mark_unknown(self, operation_id: str) -> ToolOperation:
        """Flip a STARTED row to UNKNOWN — used when a crash is what
        triggered the resume that found this row still STARTED, before its
        real outcome has been reconciled against actual state."""
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE tool_operations SET status = ? "
                "WHERE operation_id = ? AND status = ?",
                (OperationStatus.UNKNOWN, operation_id, OperationStatus.STARTED),
            )
        return self.get(operation_id)

    def get(self, operation_id: str) -> ToolOperation:
        row = self._conn.execute(
            "SELECT * FROM tool_operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return _row_to_operation(row)

    def list_unresolved(self, *, task_id: str | None = None) -> list[ToolOperation]:
        """STARTED or UNKNOWN rows — candidates for crash reconciliation
        (Foundation Plan §15)."""
        if task_id is not None:
            rows = self._conn.execute(
                "SELECT * FROM tool_operations WHERE task_id = ? "
                "AND status IN ('STARTED', 'UNKNOWN') ORDER BY started_at ASC",
                (task_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tool_operations "
                "WHERE status IN ('STARTED', 'UNKNOWN') ORDER BY started_at ASC"
            ).fetchall()
        return [_row_to_operation(row) for row in rows]


def _row_to_operation(row: sqlite3.Row) -> ToolOperation:
    return ToolOperation(
        operation_id=row["operation_id"],
        task_id=row["task_id"],
        worktree_id=row["worktree_id"],
        worker_id=row["worker_id"],
        worker_session_id=row["worker_session_id"],
        lease_generation=row["lease_generation"],
        tool_name=row["tool_name"],
        risk_class=row["risk_class"],
        request_hash=row["request_hash"],
        target_resource=row["target_resource"],
        child_pid=row["child_pid"],
        child_pid_started_at=row["child_pid_started_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        status=row["status"],
        before_evidence=row["before_evidence"],
        after_evidence=row["after_evidence"],
        result_json=row["result_json"],
    )
