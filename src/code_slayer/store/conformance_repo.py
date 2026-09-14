"""Durable worker conformance runs and results (Phase 7.3,
`worker_conformance_runs`/`worker_conformance_results` —
migrations/0003_worker_conformance.sql).

Thin, transactional persistence primitive only — like `store.lease_repo`
and `store.worker_trust_repo`, it does not decide what makes a run pass
or whether a run's evidence justifies trust promotion.
`workers.conformance` (orchestration) and `workers.promotion`
(conformance-gated trust) do, under the same `store.db.transaction()`
discipline every other repository in this codebase already follows.

A run finalizes exactly once: `finalize_run_in_transaction()` only
succeeds while the row is still `RUNNING` (enforced both by this
method's own `WHERE status = 'RUNNING'` and, independently, by the
database's own `worker_conformance_runs_no_mutate_finalized` trigger —
defense in depth, the same posture `audit_events`/`worker_trust_events`
already take toward their own immutability). Results are fully
append-only, and `UNIQUE(run_id, case_name)` makes a duplicate result
for one case impossible to record at all, not merely discouraged.
"""

from __future__ import annotations

import sqlite3

from code_slayer.store.models import WorkerConformanceResult, WorkerConformanceRun


class ConformanceRunStatus:
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    FAILED = "FAILED"

    ALL = frozenset({RUNNING, PASSED, FAILED})
    TERMINAL = frozenset({PASSED, FAILED})


class DuplicateConformanceResultError(RuntimeError):
    """Raised when a case result for a `(run_id, case_name)` pair that
    already has one is attempted — the `UNIQUE(run_id, case_name)`
    constraint makes this a schema-level impossibility; this is just a
    clearer exception than a raw `sqlite3.IntegrityError` for callers."""


class ConformanceRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- runs -------------------------------------------------------------

    def start_run_in_transaction(
        self, *, run_id: str, worker_id: str, role: str, suite_version: str, started_at: str,
    ) -> WorkerConformanceRun:
        """Durably record `RUNNING` before any case is executed — the
        same write-ahead discipline `tool_operations`/leases already use:
        a crash after this commits but before finalization leaves an
        honest `RUNNING` row, never a fabricated verdict."""
        if not self._conn.in_transaction:
            raise RuntimeError("conformance run start requires an open write transaction")
        self._conn.execute(
            "INSERT INTO worker_conformance_runs "
            "(run_id, worker_id, role, suite_version, started_at, completed_at, status) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?)",
            (run_id, worker_id, role, suite_version, started_at, ConformanceRunStatus.RUNNING),
        )
        return self.get_run(run_id)

    def finalize_run_in_transaction(
        self, run_id: str, *, status: str, completed_at: str,
    ) -> WorkerConformanceRun:
        """Resolve a `RUNNING` run to a terminal verdict. Succeeds only
        once per run: the `WHERE status = 'RUNNING'` clause here, and the
        database's own finalized-run trigger independently, both refuse
        a second finalization of the same row."""
        if status not in ConformanceRunStatus.TERMINAL:
            raise ValueError(f"not a terminal conformance run status: {status!r}")
        if not self._conn.in_transaction:
            raise RuntimeError("conformance run finalize requires an open write transaction")
        cur = self._conn.execute(
            "UPDATE worker_conformance_runs SET status = ?, completed_at = ? "
            "WHERE run_id = ? AND status = ?",
            (status, completed_at, run_id, ConformanceRunStatus.RUNNING),
        )
        if cur.rowcount == 0:
            raise RuntimeError(f"conformance run {run_id!r} was not RUNNING; cannot finalize")
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> WorkerConformanceRun | None:
        row = self._conn.execute(
            "SELECT * FROM worker_conformance_runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        return _row_to_run(row) if row is not None else None

    # -- results ------------------------------------------------------------

    def record_result_in_transaction(
        self, *, run_id: str, case_name: str, passed: bool, reason: str,
        detail_content_hash: str | None, occurred_at: str,
    ) -> WorkerConformanceResult:
        if not self._conn.in_transaction:
            raise RuntimeError("conformance result recording requires an open write transaction")
        try:
            cur = self._conn.execute(
                "INSERT INTO worker_conformance_results "
                "(run_id, case_name, passed, reason, detail_content_hash, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, case_name, int(passed), reason, detail_content_hash, occurred_at),
            )
        except sqlite3.IntegrityError as exc:
            raise DuplicateConformanceResultError(
                f"a result for case {case_name!r} already exists under run {run_id!r}"
            ) from exc
        assert cur.lastrowid is not None
        return self.get_result(cur.lastrowid)

    def get_result(self, result_id: int) -> WorkerConformanceResult:
        row = self._conn.execute(
            "SELECT * FROM worker_conformance_results WHERE id = ?", (result_id,),
        ).fetchone()
        if row is None:
            raise KeyError(result_id)
        return _row_to_result(row)

    def list_results(self, run_id: str) -> list[WorkerConformanceResult]:
        rows = self._conn.execute(
            "SELECT * FROM worker_conformance_results WHERE run_id = ? ORDER BY id ASC",
            (run_id,),
        ).fetchall()
        return [_row_to_result(row) for row in rows]


def _row_to_run(row: sqlite3.Row) -> WorkerConformanceRun:
    return WorkerConformanceRun(
        run_id=row["run_id"],
        worker_id=row["worker_id"],
        role=row["role"],
        suite_version=row["suite_version"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        status=row["status"],
    )


def _row_to_result(row: sqlite3.Row) -> WorkerConformanceResult:
    return WorkerConformanceResult(
        id=row["id"],
        run_id=row["run_id"],
        case_name=row["case_name"],
        passed=bool(row["passed"]),
        reason=row["reason"],
        detail_content_hash=row["detail_content_hash"],
        occurred_at=row["occurred_at"],
    )
