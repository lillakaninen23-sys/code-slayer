"""Schema v17: `promoted_from_validation_certificate_id` and its
partial UNIQUE index (`ux_worker_baseline_security_certificates_
promotion_provenance`).

Proves the migration is safe against pre-existing duplicate data, that
ordinary VALIDATION certificate recording remains completely
unrestricted, and that the partial index is exactly and only a
PRODUCTION-promotion-authority invariant: it constrains rows sharing a
non-NULL `promoted_from_validation_certificate_id`, never anything
else. No live Ollama contact; no trust/permission/role-certificate
mutation anywhere in this file.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from code_slayer.store import db as db_module
from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.db import utcnow_iso
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.security_baseline import (
    RuntimeProfileIdentity,
    SecurityBaselineOutcome,
    record_baseline_certificate,
)


def _apply_through(conn: sqlite3.Connection, version: int) -> None:
    """Mirrors `test_runtime_identity_separation._apply_through`: apply
    every known migration up to (and including) `version` only, so a
    test can construct a database frozen at a specific historical
    schema state before exercising a later migration against it."""
    current = db_module.schema_version(conn)
    for mig_version, _name, sql in db_module._discover_migrations():
        if mig_version <= current or mig_version > version:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (mig_version, utcnow_iso()),
        )
        conn.execute("COMMIT")


def _insert_v16_certificate(conn, *, certificate_id, worker_id, evidence_ref, fingerprint):
    """Raw INSERT matching the schema-v16 column set (no
    `promoted_from_validation_certificate_id` -- that column does not
    exist yet at this schema version)."""
    conn.execute(
        "INSERT INTO worker_baseline_security_certificates "
        "(certificate_id, worker_id, baseline_version, model_tag, model_digest, "
        "endpoint, runtime_version, normalizer_id, normalizer_version, "
        "runtime_config_fingerprint, runtime_identity_fingerprint, outcome, "
        "hard_disqualifiers_json, evidence_ref, reason, issued_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            certificate_id,
            worker_id,
            "baseline-security-v1",
            "qwen3-coder:30b",
            "sha256:abc",
            "http://127.0.0.1:11434/v1",
            "0.16.1",
            None,
            None,
            None,
            fingerprint,
            "PASS",
            "[]",
            evidence_ref,
            "ok",
            "2026-01-01T00:00:00.000000Z",
        ),
    )


# -- 1. upgrade against a database that already holds duplicate --------------
# --    (worker_id, runtime_identity_fingerprint, evidence_ref) triples ------


def test_upgrade_succeeds_with_duplicate_historical_triples(tmp_path):
    path = tmp_path / "state.db"
    conn = db_module.connect(path)
    _apply_through(conn, 16)
    assert db_module.schema_version(conn) == 16
    WorkersRepo(conn).register(worker_id="w1", kind="fake", network_class="local")

    fingerprint = "a" * 64
    conn.execute("BEGIN IMMEDIATE")
    _insert_v16_certificate(
        conn, certificate_id="dup-1", worker_id="w1",
        evidence_ref="same-evidence", fingerprint=fingerprint,
    )
    _insert_v16_certificate(
        conn, certificate_id="dup-2", worker_id="w1",
        evidence_ref="same-evidence", fingerprint=fingerprint,
    )
    conn.execute("COMMIT")
    rows_before = conn.execute(
        "SELECT certificate_id FROM worker_baseline_security_certificates "
        "WHERE worker_id = 'w1' ORDER BY certificate_id",
    ).fetchall()
    assert [r["certificate_id"] for r in rows_before] == ["dup-1", "dup-2"]

    # The exact scenario the earlier, broader
    # UNIQUE(worker_id, runtime_identity_fingerprint, evidence_ref)
    # invariant would have failed an upgrade against: two historical
    # rows sharing that triple. This migration must not.
    assert db_module.migrate(conn) == 17

    rows_after = conn.execute(
        "SELECT certificate_id, promoted_from_validation_certificate_id "
        "FROM worker_baseline_security_certificates WHERE worker_id = 'w1' "
        "ORDER BY certificate_id",
    ).fetchall()
    assert [r["certificate_id"] for r in rows_after] == ["dup-1", "dup-2"]
    assert all(r["promoted_from_validation_certificate_id"] is None for r in rows_after)
    conn.close()


# -- 2. ordinary VALIDATION recording remains completely unrestricted --------


@pytest.fixture
def registered_worker(db_conn) -> str:
    WorkersRepo(db_conn).register(worker_id="w1", kind="fake", network_class="local")
    return "w1"


@pytest.fixture
def profile() -> RuntimeProfileIdentity:
    return RuntimeProfileIdentity(
        model_tag="qwen3-coder:30b",
        model_digest="sha256:abc",
        endpoint="http://127.0.0.1:11434/v1",
        runtime_version="0.16.1",
    )


def test_ordinary_recording_can_still_share_a_triple_after_migration(
    db_conn, registered_worker, profile,
):
    """Two ordinary (non-promotion) certificates for the exact same
    worker, runtime profile, and evidence_ref must both record
    successfully post-migration -- `promoted_from_validation_
    certificate_id` is NULL on both, so the partial index never even
    considers them."""
    first = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="shared-evidence",
        reason="ok",
    )
    second = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="shared-evidence",
        reason="ok-again",
    )
    assert first.ok
    assert second.ok
    assert first.certificate.certificate_id != second.certificate.certificate_id
    assert first.certificate.promoted_from_validation_certificate_id is None
    assert second.certificate.promoted_from_validation_certificate_id is None
    rows = BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker)
    assert len(rows) == 2


def test_promoted_from_validation_certificate_id_round_trips(
    db_conn, registered_worker, profile,
):
    result = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="ev",
        reason="promoted_from_validation",
        promoted_from_validation_certificate_id="validation-cert-abc",
    )
    assert result.ok
    assert result.certificate.promoted_from_validation_certificate_id == "validation-cert-abc"
    fetched = BaselineSecurityCertificatesRepo(db_conn).get(result.certificate.certificate_id)
    assert fetched.promoted_from_validation_certificate_id == "validation-cert-abc"


def test_blank_promoted_from_validation_certificate_id_is_refused(
    db_conn, registered_worker, profile,
):
    result = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="ev",
        reason="ok",
        promoted_from_validation_certificate_id="   ",
    )
    assert not result.ok
    assert result.reason == "malformed_certificate_request"
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


# -- 3. the partial index is what makes a concurrent double-promotion --------
# --    fail deterministically down to exactly one row -----------------------


def test_concurrent_recording_with_identical_provenance_yields_one_row(
    tmp_path, profile,
):
    path = tmp_path / "production.db"
    conn = db_module.connect(path)
    db_module.migrate(conn)
    WorkersRepo(conn).register(worker_id="w1", kind="fake", network_class="local")
    conn.close()

    barrier = threading.Barrier(2)
    outcomes: list = [None, None]

    def _run(index):
        thread_conn = db_module.connect(path)
        try:
            barrier.wait(timeout=5)
            outcomes[index] = record_baseline_certificate(
                thread_conn,
                worker_id="w1",
                runtime_profile=profile,
                outcome=SecurityBaselineOutcome.PASS,
                evidence_ref="ev",
                reason="promoted_from_validation",
                promoted_from_validation_certificate_id="same-validation-cert",
            )
        except sqlite3.IntegrityError as exc:
            outcomes[index] = exc
        finally:
            thread_conn.close()

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads)

    successes = [o for o in outcomes if hasattr(o, "ok") and o.ok]
    conflicts = [o for o in outcomes if isinstance(o, sqlite3.IntegrityError)]
    assert len(successes) == 1, outcomes
    assert len(conflicts) == 1, outcomes

    verify_conn = db_module.connect(path)
    rows = BaselineSecurityCertificatesRepo(verify_conn).list_for_worker("w1")
    assert len(rows) == 1
    assert rows[0].promoted_from_validation_certificate_id == "same-validation-cert"
    assert rows[0].certificate_id == successes[0].certificate.certificate_id
    verify_conn.close()


# -- 4. two DISTINCT VALIDATION certificate IDs are independently promotable -


def test_two_distinct_validation_certificate_ids_each_promote_independently(
    db_conn, registered_worker, profile,
):
    first = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="ev-1",
        reason="promoted_from_validation",
        promoted_from_validation_certificate_id="validation-cert-1",
    )
    second = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="ev-2",
        reason="promoted_from_validation",
        promoted_from_validation_certificate_id="validation-cert-2",
    )
    assert first.ok
    assert second.ok
    assert first.certificate.certificate_id != second.certificate.certificate_id
    repo = BaselineSecurityCertificatesRepo(db_conn)
    assert repo.get_by_promotion_provenance("validation-cert-1").certificate_id == (
        first.certificate.certificate_id
    )
    assert repo.get_by_promotion_provenance("validation-cert-2").certificate_id == (
        second.certificate.certificate_id
    )
    assert repo.get_by_promotion_provenance("validation-cert-does-not-exist") is None
    rows = repo.list_for_worker(registered_worker)
    assert len(rows) == 2

    # A THIRD attempt reusing the FIRST provenance id must still fail --
    # this is not merely "two distinct ids work", it also confirms a
    # repeat of an already-used id is still rejected once more,
    # independent of how many distinct ids have been used in between.
    with pytest.raises(sqlite3.IntegrityError):
        record_baseline_certificate(
            db_conn,
            worker_id=registered_worker,
            runtime_profile=profile,
            outcome=SecurityBaselineOutcome.PASS,
            evidence_ref="ev-3",
            reason="promoted_from_validation",
            promoted_from_validation_certificate_id="validation-cert-1",
        )
