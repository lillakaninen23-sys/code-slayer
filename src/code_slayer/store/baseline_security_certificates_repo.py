"""Durable Baseline Security Certificates
(`worker_baseline_security_certificates` —
migrations/0011_baseline_security_certification.sql).

Thin, transactional persistence primitive only — like `store.
conformance_repo`/`store.worker_trust_repo`, this module does not decide
what makes an evaluation `PASS`/`FAIL`/`HARD_DISQUALIFIED`, nor whether a
certificate's runtime-profile binding still matches a worker's current
configuration. `code_slayer.workers.security_baseline` (recording) and
`code_slayer.workers.production_eligibility` (the production-eligibility
gate) do, under the same `store.db.transaction()` discipline every other
repository in this codebase already follows.

Fully append-only, mirroring `worker_conformance_results`/
`worker_trust_events` exactly: a certificate is durable evidence of one
evaluation and is never edited or removed after the fact. Re-evaluating
a worker always creates a new row — the current certificate for a given
runtime-profile binding is *derived* by reading the most recent matching
row, never stored as a separately mutated status column.
"""

from __future__ import annotations

import sqlite3

from code_slayer.store.models import WorkerBaselineSecurityCertificate


class BaselineSecurityCertificatesRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record_in_transaction(
        self,
        *,
        certificate_id: str,
        worker_id: str,
        baseline_version: str,
        model_tag: str,
        model_digest: str | None,
        endpoint: str | None,
        runtime_version: str | None,
        outcome: str,
        hard_disqualifiers_json: str,
        evidence_ref: str,
        reason: str,
        issued_at: str,
        normalizer_id: str | None = None,
        normalizer_version: int | None = None,
        runtime_config_fingerprint: str | None = None,
    ) -> WorkerBaselineSecurityCertificate:
        if not self._conn.in_transaction:
            raise RuntimeError(
                "baseline security certificate recording requires an open write transaction",
            )
        self._conn.execute(
            "INSERT INTO worker_baseline_security_certificates "
            "(certificate_id, worker_id, baseline_version, model_tag, model_digest, "
            "endpoint, runtime_version, normalizer_id, normalizer_version, "
            "runtime_config_fingerprint, outcome, "
            "hard_disqualifiers_json, evidence_ref, reason, issued_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                certificate_id,
                worker_id,
                baseline_version,
                model_tag,
                model_digest,
                endpoint,
                runtime_version,
                normalizer_id,
                normalizer_version,
                runtime_config_fingerprint,
                outcome,
                hard_disqualifiers_json,
                evidence_ref,
                reason,
                issued_at,
            ),
        )
        certificate = self.get(certificate_id)
        assert certificate is not None
        return certificate

    def get(self, certificate_id: str) -> WorkerBaselineSecurityCertificate | None:
        row = self._conn.execute(
            "SELECT * FROM worker_baseline_security_certificates WHERE certificate_id = ?",
            (certificate_id,),
        ).fetchone()
        return _row_to_certificate(row) if row is not None else None

    def list_for_worker(self, worker_id: str) -> list[WorkerBaselineSecurityCertificate]:
        """Every certificate ever recorded for `worker_id`, most recent
        first (`issued_at` DESC, then `rowid` DESC to break exact-
        timestamp ties deterministically) — the order `code_slayer.
        workers.production_eligibility` relies on to find the current
        one for a given runtime-profile binding."""
        rows = self._conn.execute(
            "SELECT * FROM worker_baseline_security_certificates WHERE worker_id = ? "
            "ORDER BY issued_at DESC, rowid DESC",
            (worker_id,),
        ).fetchall()
        return [_row_to_certificate(row) for row in rows]


def _row_to_certificate(row: sqlite3.Row) -> WorkerBaselineSecurityCertificate:
    return WorkerBaselineSecurityCertificate(
        certificate_id=row["certificate_id"],
        worker_id=row["worker_id"],
        baseline_version=row["baseline_version"],
        model_tag=row["model_tag"],
        model_digest=row["model_digest"],
        endpoint=row["endpoint"],
        runtime_version=row["runtime_version"],
        outcome=row["outcome"],
        hard_disqualifiers_json=row["hard_disqualifiers_json"],
        evidence_ref=row["evidence_ref"],
        reason=row["reason"],
        issued_at=row["issued_at"],
        normalizer_id=row["normalizer_id"],
        normalizer_version=row["normalizer_version"],
        runtime_config_fingerprint=row["runtime_config_fingerprint"],
    )
