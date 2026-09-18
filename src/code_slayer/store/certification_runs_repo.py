"""Durable Certification Center runs (schema v16).

The repository records state; `security.certification_service` decides
legality. Terminal rows cannot be reopened (SQL trigger). Queue and
claim are atomic `UPDATE ... WHERE state = ...` so a race cannot start
two attempts from one READY preflight.
"""

from __future__ import annotations

import sqlite3

from code_slayer.store.models import CertificationRunRow

ACTIVE_STATES = ("READY", "QUEUED", "RUNNING")
TERMINAL_STATES = ("PASS", "FAIL", "HARD_DISQUALIFIED", "INCOMPLETE")
CLAIMABLE_STATES = ("QUEUED",)


class CertificationRunsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_in_transaction(
        self,
        *,
        run_id: str,
        worker_id: str,
        kind: str,
        environment: str,
        state: str,
        created_at: str,
        preflight_json: str = "[]",
        reason: str | None = None,
        expected_runtime_identity_fingerprint: str | None = None,
        model_tag: str | None = None,
        model_digest: str | None = None,
        ollama_root: str | None = None,
    ) -> CertificationRunRow:
        if not self._conn.in_transaction:
            raise RuntimeError("certification run creation requires an open write transaction")
        self._conn.execute(
            "INSERT INTO certification_runs ("
            "run_id, worker_id, kind, environment, state, reason, preflight_json, "
            "expected_runtime_identity_fingerprint, model_tag, model_digest, ollama_root, "
            "hard_disqualifiers_json, created_at, updated_at, attempt, owner_generation"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, 0, 0)",
            (
                run_id, worker_id, kind, environment, state, reason, preflight_json,
                expected_runtime_identity_fingerprint, model_tag, model_digest, ollama_root,
                created_at, created_at,
            ),
        )
        return self.get(run_id)

    def get(self, run_id: str) -> CertificationRunRow:
        row = self._conn.execute(
            "SELECT * FROM certification_runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _row(row)

    def get_or_none(self, run_id: str) -> CertificationRunRow | None:
        row = self._conn.execute(
            "SELECT * FROM certification_runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        return _row(row) if row is not None else None

    def list_for_worker(
        self, worker_id: str, *, limit: int = 100, kind: str | None = None,
    ) -> list[CertificationRunRow]:
        """Every run for `worker_id`, most recent first. `kind=None`
        (the default, unchanged from before this parameter existed)
        returns every kind mixed together -- what `history()` wants.
        A caller that needs "the latest run of exactly this kind"
        (e.g. one certification track's own last-preflight display)
        passes `kind` explicitly."""
        if kind is None:
            rows = self._conn.execute(
                "SELECT * FROM certification_runs WHERE worker_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (worker_id, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM certification_runs WHERE worker_id = ? AND kind = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (worker_id, kind, limit),
            ).fetchall()
        return [_row(row) for row in rows]

    def active_for_worker(self, worker_id: str, *, kind: str) -> CertificationRunRow | None:
        row = self._conn.execute(
            "SELECT * FROM certification_runs WHERE worker_id = ? AND kind = ? "
            "AND state IN ('READY', 'QUEUED', 'RUNNING') "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (worker_id, kind),
        ).fetchone()
        return _row(row) if row is not None else None

    def latest_ready(self, worker_id: str, *, kind: str) -> CertificationRunRow | None:
        row = self._conn.execute(
            "SELECT * FROM certification_runs WHERE worker_id = ? AND kind = ? "
            "AND state = 'READY' ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (worker_id, kind),
        ).fetchone()
        return _row(row) if row is not None else None

    def supersede_ready_in_transaction(
        self, worker_id: str, *, kind: str, now: str, except_run_id: str | None = None,
    ) -> None:
        if not self._conn.in_transaction:
            raise RuntimeError("supersede requires an open write transaction")
        self._conn.execute(
            "UPDATE certification_runs SET state = 'INCOMPLETE', reason = ?, "
            "updated_at = ?, finished_at = ? WHERE worker_id = ? AND kind = ? "
            "AND state = 'READY' AND run_id != COALESCE(?, '')",
            ("preflight_superseded", now, now, worker_id, kind, except_run_id),
        )

    def claimable_ids(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT run_id FROM certification_runs WHERE state = 'QUEUED' "
            "ORDER BY created_at ASC, rowid ASC",
        ).fetchall()
        return [row["run_id"] for row in rows]

    def queue_in_transaction(self, run_id: str, *, now: str) -> CertificationRunRow:
        if not self._conn.in_transaction:
            raise RuntimeError("certification run queue requires an open write transaction")
        cursor = self._conn.execute(
            "UPDATE certification_runs SET state = 'QUEUED', updated_at = ? "
            "WHERE run_id = ? AND state = 'READY'",
            (now, run_id),
        )
        if cursor.rowcount != 1:
            raise KeyError(run_id)
        return self.get(run_id)

    def claim_in_transaction(
        self, run_id: str, *, owner_pid: int, owner_pid_started_at: str | None, now: str,
    ) -> CertificationRunRow | None:
        if not self._conn.in_transaction:
            raise RuntimeError("certification run claim requires an open write transaction")
        current = self.get_or_none(run_id)
        if current is None or current.state != "QUEUED":
            return None
        cursor = self._conn.execute(
            "UPDATE certification_runs SET state = 'RUNNING', updated_at = ?, "
            "attempt = ?, owner_pid = ?, owner_pid_started_at = ?, owner_generation = ?, "
            "started_at = ? WHERE run_id = ? AND state = 'QUEUED'",
            (
                now, current.attempt + 1, owner_pid, owner_pid_started_at,
                current.owner_generation + 1, now, run_id,
            ),
        )
        if cursor.rowcount != 1:
            return None
        return self.get(run_id)

    def finish_in_transaction(
        self,
        run_id: str,
        *,
        state: str,
        expected_generation: int,
        now: str,
        reason: str | None = None,
        certificate_id: str | None = None,
        evidence_ref: str | None = None,
        hard_disqualifiers_json: str = "[]",
    ) -> CertificationRunRow | None:
        if not self._conn.in_transaction:
            raise RuntimeError("certification run finish requires an open write transaction")
        current = self.get_or_none(run_id)
        if current is None or current.owner_generation != expected_generation:
            return None
        if current.state != "RUNNING":
            return None
        self._conn.execute(
            "UPDATE certification_runs SET state = ?, reason = ?, certificate_id = ?, "
            "evidence_ref = ?, hard_disqualifiers_json = ?, updated_at = ?, finished_at = ? "
            "WHERE run_id = ?",
            (
                state, reason, certificate_id, evidence_ref, hard_disqualifiers_json,
                now, now, run_id,
            ),
        )
        return self.get(run_id)


def _row(row: sqlite3.Row) -> CertificationRunRow:
    return CertificationRunRow(
        run_id=row["run_id"],
        worker_id=row["worker_id"],
        kind=row["kind"],
        environment=row["environment"],
        state=row["state"],
        reason=row["reason"],
        preflight_json=row["preflight_json"],
        expected_runtime_identity_fingerprint=row["expected_runtime_identity_fingerprint"],
        model_tag=row["model_tag"],
        model_digest=row["model_digest"],
        ollama_root=row["ollama_root"],
        certificate_id=row["certificate_id"],
        evidence_ref=row["evidence_ref"],
        hard_disqualifiers_json=row["hard_disqualifiers_json"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        attempt=row["attempt"],
        owner_pid=row["owner_pid"],
        owner_pid_started_at=row["owner_pid_started_at"],
        owner_generation=row["owner_generation"],
    )
