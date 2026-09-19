"""H.3 Stage 5: archive/reactivate preserve every other durable table.

Seeds one of each kind of historical/authority row this worker's
lifecycle must never touch -- a Baseline Security certificate, a role
certificate, a trust event, a runner_runs history row, an evidence
blob, and (not worker-scoped, but in the same production DB) a
permission request/grant/revocation -- snapshots each row exactly, and
proves byte-for-byte equality after an archive + reactivate round
trip. `audit_events` is verified differently: existing rows must be
byte-identical (append-only, never rewritten), and exactly two new
rows (WORKER_ARCHIVED, WORKER_REACTIVATED) are the only addition.
"""

from __future__ import annotations

import pytest

from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.permissions_repo import PermissionsRepo
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.runner_repo import RunnerRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.lifecycle import archive_worker, reactivate_worker
from code_slayer.workers.role_qualification import (
    ProductionRole,
    RoleQualificationOutcome,
    record_role_certificate,
    role_evaluation_identity_from_config,
)
from code_slayer.workers.security_baseline import (
    SecurityBaselineOutcome,
    record_baseline_certificate,
    runtime_profile_identity_from_config,
)
from code_slayer.workers.trust import TrustLevel, WorkerTrustManager

WORKER = "preservation-worker"
POLICY_VERSION = "planner-certification-v1"


@pytest.fixture
def profile():
    return runtime_profile_identity_from_config(
        model_tag="qwen3-coder:30b", model_digest="sha256:abc",
        endpoint="http://127.0.0.1:11434/v1", runtime_version="0.16.1",
        effective_context_tokens=16384, temperature=0.0,
        normalizer_id=None, normalizer_version=None,
    )


@pytest.fixture
def seeded(db_conn, profile, tmp_path):
    """Registers WORKER and creates one row in every table H.3 must
    preserve untouched, returning enough identifiers to re-fetch each
    one after the archive/reactivate round trip."""
    WorkersRepo(db_conn).register(worker_id=WORKER, kind="fake", network_class="local")

    baseline = record_baseline_certificate(
        db_conn, worker_id=WORKER, runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS, evidence_ref="baseline-ev", reason="ok",
    )
    assert baseline.ok, baseline.reason

    role_eval = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        output_token_budget=4096, tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
        execution_timeout_seconds=45.0,
        policy_version=POLICY_VERSION,
    )
    role = record_role_certificate(
        db_conn, worker_id=WORKER, role=ProductionRole.PLANNER, runtime_profile=profile,
        policy_version=POLICY_VERSION, outcome=RoleQualificationOutcome.PASS,
        classification="PASS_FIRST_TRY", evidence_ref="role-ev", reason="ok",
        role_evaluation=role_eval,
    )
    assert role.ok, role.reason

    trust = WorkerTrustManager(db_conn)
    trust_result = trust.promote_to_guarded(
        worker_id=WORKER, role="coder", capability="read_file",
        reason="test_fixture_grant", evidence_ref="trust-ev",
    )
    assert trust_result.ok, trust_result.reason
    assert trust.current_trust(WORKER, "coder", "read_file") == TrustLevel.GUARDED

    now = utcnow_iso()
    with transaction(db_conn):
        RunnerRepo(db_conn).create_in_transaction(
            run_id="run-history-1", created_at=now, repo_id="repo-1",
            primary_worktree_id="wt-1", original_prompt_hash="hash-1",
            worker_id=WORKER, role="coder", requires_mutation=False, status="COMPLETED",
        )

    store = ContentStore(db_conn, tmp_path / "blobs")
    blob = store.put(
        b"evidence content", media_type="text/plain",
        source_kind="original_prompt", exportable=False,
    )

    permissions = PermissionsRepo(db_conn)
    with transaction(db_conn):
        permissions.create_request_in_transaction(
            request_id="perm-req-1", created_at=now, repo_id="repo-1", worktree_id="wt-1",
            permission_key="network.discovery.local", semantic_version="1",
            resource=None, purpose="test", requesting_subsystem="test",
        )
        permissions.create_decision_in_transaction(
            request_id="perm-req-1", decision="ALLOW", decided_at=now,
        )
        permissions.create_grant_in_transaction(
            grant_id="perm-grant-1", request_id="perm-req-1",
            permission_key="network.discovery.local", semantic_version="1",
            resource=None, authority_origin="USER_EXPLICIT", granted_at=now, expiry=None,
        )
        permissions.create_revocation_in_transaction(grant_id="perm-grant-1", revoked_at=now)

    return {
        "certificate_id": baseline.certificate.certificate_id,
        "role_certificate_id": role.certificate.certificate_id,
        "blob_hash": blob.content_hash,
    }


def _row(conn, table, key_col, key):
    row = conn.execute(f"SELECT * FROM {table} WHERE {key_col} = ?", (key,)).fetchone()  # noqa: S608
    return dict(row) if row is not None else None


def _all_rows(conn, table, order_col="rowid"):
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_col}").fetchall()  # noqa: S608
    return [dict(r) for r in rows]


def test_archive_reactivate_round_trip_preserves_every_other_table(db_conn, seeded, tmp_path):
    tables_before = {
        "worker_baseline_security_certificates": _row(
            db_conn, "worker_baseline_security_certificates", "certificate_id",
            seeded["certificate_id"],
        ),
        "worker_role_certificates": _row(
            db_conn, "worker_role_certificates", "certificate_id",
            seeded["role_certificate_id"],
        ),
        "worker_trust_events": _all_rows(db_conn, "worker_trust_events"),
        "runner_runs": _row(db_conn, "runner_runs", "run_id", "run-history-1"),
        "permission_requests": _row(db_conn, "permission_requests", "request_id", "perm-req-1"),
        "permission_grants": _row(db_conn, "permission_grants", "grant_id", "perm-grant-1"),
        "permission_revocations": _row(
            db_conn, "permission_revocations", "grant_id", "perm-grant-1",
        ),
        "content_blobs": _row(db_conn, "content_blobs", "content_hash", seeded["blob_hash"]),
    }
    audit_before = _all_rows(db_conn, "audit_events")
    blob_bytes_before = ContentStore(db_conn, tmp_path / "blobs").read(seeded["blob_hash"])

    archived = archive_worker(db_conn, worker_id=WORKER)
    assert archived.ok and archived.changed
    reactivated = reactivate_worker(db_conn, worker_id=WORKER)
    assert reactivated.ok and reactivated.changed

    tables_after = {
        "worker_baseline_security_certificates": _row(
            db_conn, "worker_baseline_security_certificates", "certificate_id",
            seeded["certificate_id"],
        ),
        "worker_role_certificates": _row(
            db_conn, "worker_role_certificates", "certificate_id",
            seeded["role_certificate_id"],
        ),
        "worker_trust_events": _all_rows(db_conn, "worker_trust_events"),
        "runner_runs": _row(db_conn, "runner_runs", "run_id", "run-history-1"),
        "permission_requests": _row(db_conn, "permission_requests", "request_id", "perm-req-1"),
        "permission_grants": _row(db_conn, "permission_grants", "grant_id", "perm-grant-1"),
        "permission_revocations": _row(
            db_conn, "permission_revocations", "grant_id", "perm-grant-1",
        ),
        "content_blobs": _row(db_conn, "content_blobs", "content_hash", seeded["blob_hash"]),
    }
    for table, before in tables_before.items():
        assert tables_after[table] == before, f"{table} changed"

    blob_bytes_after = ContentStore(db_conn, tmp_path / "blobs").read(seeded["blob_hash"])
    assert blob_bytes_after == blob_bytes_before == b"evidence content"

    audit_after = _all_rows(db_conn, "audit_events")
    assert audit_after[: len(audit_before)] == audit_before
    new_events = audit_after[len(audit_before):]
    assert [e["event_type"] for e in new_events] == ["WORKER_ARCHIVED", "WORKER_REACTIVATED"]


def test_archive_alone_preserves_certificates_and_history(db_conn, seeded):
    """A narrower check against just the certificate/history rows most
    directly relevant to eligibility -- archive only, no reactivate."""
    baseline_before = BaselineSecurityCertificatesRepo(db_conn).get(seeded["certificate_id"])
    role_before = RoleCertificatesRepo(db_conn).get(seeded["role_certificate_id"])

    archive_worker(db_conn, worker_id=WORKER)

    baseline_after = BaselineSecurityCertificatesRepo(db_conn).get(seeded["certificate_id"])
    role_after = RoleCertificatesRepo(db_conn).get(seeded["role_certificate_id"])
    assert baseline_after == baseline_before
    assert role_after == role_before
