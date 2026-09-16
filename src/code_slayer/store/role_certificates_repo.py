"""Durable Role Qualification Certificates (`worker_role_certificates` —
migrations/0012_role_qualification_certification.sql).

Thin, transactional persistence primitive only — like `store.
baseline_security_certificates_repo`, this module does not decide what
makes an evaluation `PASS`/`FAIL`, nor whether a certificate's
runtime-profile binding still matches a worker's current configuration.
`code_slayer.workers.role_qualification` (the generic recording
primitive), each role's own certification boundary (e.g. `planning.
planner_certification` for PLANNER), and `code_slayer.workers.
production_eligibility` (the production-eligibility gate) do, under the
same `store.db.transaction()` discipline every other repository in this
codebase already follows.

Fully append-only, mirroring `store.baseline_security_certificates_repo`
exactly: a certificate is durable evidence of one certification decision
and is never edited or removed after the fact. Re-evaluating a worker
for a role always creates a new row — the current certificate for a
given `(worker_id, role, runtime profile)` binding is *derived* by
reading the most recent matching row, never stored as a separately
mutated status column.
"""

from __future__ import annotations

import sqlite3

from code_slayer.store.models import WorkerRoleCertificate


class RoleCertificatesRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record_in_transaction(
        self, *, certificate_id: str, worker_id: str, role: str, policy_version: str,
        model_tag: str, model_digest: str | None, endpoint: str | None,
        runtime_version: str | None, outcome: str, classification: str,
        evidence_ref: str, reason: str, issued_at: str,
    ) -> WorkerRoleCertificate:
        if not self._conn.in_transaction:
            raise RuntimeError(
                "role certificate recording requires an open write transaction",
            )
        self._conn.execute(
            "INSERT INTO worker_role_certificates "
            "(certificate_id, worker_id, role, policy_version, model_tag, model_digest, "
            "endpoint, runtime_version, outcome, classification, evidence_ref, reason, "
            "issued_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                certificate_id, worker_id, role, policy_version, model_tag, model_digest,
                endpoint, runtime_version, outcome, classification, evidence_ref, reason,
                issued_at,
            ),
        )
        certificate = self.get(certificate_id)
        assert certificate is not None
        return certificate

    def get(self, certificate_id: str) -> WorkerRoleCertificate | None:
        row = self._conn.execute(
            "SELECT * FROM worker_role_certificates WHERE certificate_id = ?",
            (certificate_id,),
        ).fetchone()
        return _row_to_certificate(row) if row is not None else None

    def list_for_worker_role(self, worker_id: str, role: str) -> list[WorkerRoleCertificate]:
        """Every certificate ever recorded for exactly `(worker_id,
        role)`, most recent first (`issued_at` DESC, then `rowid` DESC
        to break exact-timestamp ties deterministically) — the order
        `code_slayer.workers.production_eligibility` relies on to find
        the current one for a given runtime-profile binding. A
        certificate for a DIFFERENT role is never returned, structurally
        — there is no code path by which one role's evidence could ever
        be consulted while resolving another's."""
        rows = self._conn.execute(
            "SELECT * FROM worker_role_certificates WHERE worker_id = ? AND role = ? "
            "ORDER BY issued_at DESC, rowid DESC",
            (worker_id, role),
        ).fetchall()
        return [_row_to_certificate(row) for row in rows]


def _row_to_certificate(row: sqlite3.Row) -> WorkerRoleCertificate:
    return WorkerRoleCertificate(
        certificate_id=row["certificate_id"],
        worker_id=row["worker_id"],
        role=row["role"],
        policy_version=row["policy_version"],
        model_tag=row["model_tag"],
        model_digest=row["model_digest"],
        endpoint=row["endpoint"],
        runtime_version=row["runtime_version"],
        outcome=row["outcome"],
        classification=row["classification"],
        evidence_ref=row["evidence_ref"],
        reason=row["reason"],
        issued_at=row["issued_at"],
    )
