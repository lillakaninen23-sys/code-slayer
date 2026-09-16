"""Role Qualification Certification foundation: the generic, role-
agnostic certificate representation and recording primitive
(`workers.role_qualification`).

The Planner-specific certification boundary that produces real
certificates from `planning.qualification` evidence is tested separately
in `tests/unit/test_planner_certification.py`; `workers.production_
eligibility`'s own combination tests live in `tests/unit/
test_production_eligibility.py`."""

from __future__ import annotations

import json
import sqlite3

import pytest

from code_slayer.audit.verify import verify_chain
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.promotion import promote_from_conformance
from code_slayer.workers.role_qualification import (
    ProductionRole,
    RoleCertificationResult,
    RoleQualificationOutcome,
    record_role_certificate,
)
from code_slayer.workers.security_baseline import RuntimeProfileIdentity
from code_slayer.workers.trust import TrustLevel, WorkerTrustManager


@pytest.fixture
def registered_worker(db_conn) -> str:
    WorkersRepo(db_conn).register(worker_id="w1", kind="fake", network_class="local")
    return "w1"


@pytest.fixture
def profile() -> RuntimeProfileIdentity:
    return RuntimeProfileIdentity(
        model_tag="devstral:24b", model_digest="sha256:abc", endpoint="http://local:11436/v1",
        runtime_version="0.1.0",
    )


def _record(
    conn, worker_id, role, profile, outcome, *, classification="PASS_FIRST_TRY",
    evidence_ref="evidence-1", reason="ok", policy_version="planner-certification-v1",
):
    return record_role_certificate(
        conn, worker_id=worker_id, role=role, runtime_profile=profile,
        policy_version=policy_version, outcome=outcome, classification=classification,
        evidence_ref=evidence_ref, reason=reason,
    )


def _passing_conformance_responses():
    from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind, WorkerToolCall

    return [
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="hi there"),
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="clean output"),
        WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="read_file", params={"path": "README.md"}),
        ),
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="continuing"),
        WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="read_file", params={"path": "README.md"}),
        ),
    ]


# -- ProductionRole: fixed vocabulary ----------------------------------------

def test_production_role_has_exactly_the_five_required_roles():
    assert {r.value for r in ProductionRole} == {
        "PLANNER", "CODER", "REVIEWER", "REPAIRER", "SECURITY",
    }


# -- record_role_certificate(): fail-closed validation -----------------------

def test_pass_certificate_requires_evidence_reference(db_conn, registered_worker, profile):
    result = _record(
        db_conn, registered_worker, ProductionRole.PLANNER, profile,
        RoleQualificationOutcome.PASS, evidence_ref="  ",
    )
    assert result == RoleCertificationResult(False, "missing_evidence_reference")


def test_fail_certificate_also_requires_evidence_reference(db_conn, registered_worker, profile):
    """A certificate is never a bare boolean -- FAIL needs provenance
    exactly as much as PASS does."""
    result = _record(
        db_conn, registered_worker, ProductionRole.PLANNER, profile,
        RoleQualificationOutcome.FAIL, classification="FAIL_POLICY", evidence_ref="",
    )
    assert not result.ok
    assert result.reason == "missing_evidence_reference"


def test_blank_classification_is_refused(db_conn, registered_worker, profile):
    result = _record(
        db_conn, registered_worker, ProductionRole.PLANNER, profile,
        RoleQualificationOutcome.PASS, classification="   ",
    )
    assert result == RoleCertificationResult(False, "malformed_certificate_request")


def test_blank_policy_version_is_refused(db_conn, registered_worker, profile):
    result = _record(
        db_conn, registered_worker, ProductionRole.PLANNER, profile,
        RoleQualificationOutcome.PASS, policy_version="",
    )
    assert result == RoleCertificationResult(False, "malformed_certificate_request")


def test_malformed_role_type_is_refused(db_conn, registered_worker, profile):
    result = record_role_certificate(
        db_conn, worker_id=registered_worker, role="PLANNER",  # a plain string, not the enum
        runtime_profile=profile, policy_version="v1", outcome=RoleQualificationOutcome.PASS,
        classification="PASS_FIRST_TRY", evidence_ref="ev", reason="ok",
    )
    assert result == RoleCertificationResult(False, "malformed_certificate_request")


def test_malformed_outcome_type_is_refused(db_conn, registered_worker, profile):
    result = record_role_certificate(
        db_conn, worker_id=registered_worker, role=ProductionRole.PLANNER,
        runtime_profile=profile, policy_version="v1", outcome="PASS",  # a plain string
        classification="PASS_FIRST_TRY", evidence_ref="ev", reason="ok",
    )
    assert result == RoleCertificationResult(False, "malformed_certificate_request")


def test_certificate_for_unregistered_worker_is_refused(db_conn, profile):
    result = _record(
        db_conn, "never-registered", ProductionRole.PLANNER, profile,
        RoleQualificationOutcome.PASS,
    )
    assert result == RoleCertificationResult(False, "unknown_worker")
    assert RoleCertificatesRepo(db_conn).list_for_worker_role(
        "never-registered", ProductionRole.PLANNER.value,
    ) == []


# -- append-only schema -------------------------------------------------

def test_certificate_table_is_append_only(db_conn, registered_worker, profile):
    result = _record(
        db_conn, registered_worker, ProductionRole.PLANNER, profile,
        RoleQualificationOutcome.PASS,
    )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "UPDATE worker_role_certificates SET outcome = 'FAIL' WHERE certificate_id = ?",
            (result.certificate.certificate_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "DELETE FROM worker_role_certificates WHERE certificate_id = ?",
            (result.certificate.certificate_id,),
        )


# -- audit/provenance -----------------------------------------------------

def test_recording_a_certificate_is_audited(db_conn, registered_worker, profile):
    result = _record(
        db_conn, registered_worker, ProductionRole.PLANNER, profile,
        RoleQualificationOutcome.PASS, reason="planner_qualification_passed",
    )
    rows = [
        dict(r) for r in db_conn.execute(
            "SELECT event_type, payload_json FROM audit_events WHERE task_id IS NULL "
            "ORDER BY seq",
        )
    ]
    matching = [r for r in rows if r["event_type"] == "ROLE_QUALIFICATION_CERTIFICATE_RECORDED"]
    assert len(matching) == 1
    payload = json.loads(matching[0]["payload_json"])
    assert payload["certificate_id"] == result.certificate.certificate_id
    assert payload["worker_id"] == registered_worker
    assert payload["role"] == "PLANNER"
    assert payload["outcome"] == "PASS"
    assert payload["classification"] == "PASS_FIRST_TRY"
    assert payload["evidence_ref"] == "evidence-1"
    assert verify_chain(db_conn, task_id=None).ok


# -- registration/trust/baseline-security never fabricate a role cert -------

def test_worker_registration_creates_no_role_certificate(db_conn):
    WorkersRepo(db_conn).register(worker_id="fresh-worker", kind="fake", network_class="local")
    for role in ProductionRole:
        assert RoleCertificatesRepo(db_conn).list_for_worker_role(
            "fresh-worker", role.value,
        ) == []


def test_trust_promotion_creates_no_role_certificate(db_conn, registered_worker):
    from code_slayer.workers.conformance import run_conformance_suite
    from code_slayer.workers.fake_adapter import FakeWorkerAdapter

    responses = _passing_conformance_responses()
    suite = run_conformance_suite(
        db_conn, FakeWorkerAdapter(responses), worker_id=registered_worker, role="coder",
    )
    assert suite.ok and suite.status == "PASSED"
    promotion = promote_from_conformance(
        db_conn, worker_id=registered_worker, role="coder", capability="read_file",
        run_id=suite.run_id,
    )
    assert promotion.ok
    for role in ProductionRole:
        assert RoleCertificatesRepo(db_conn).list_for_worker_role(
            registered_worker, role.value,
        ) == []


def test_baseline_security_certificate_creates_no_role_certificate(
    db_conn, registered_worker, profile,
):
    from code_slayer.workers.security_baseline import (
        SecurityBaselineOutcome,
        record_baseline_certificate,
    )

    result = record_baseline_certificate(
        db_conn, worker_id=registered_worker, runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS, evidence_ref="ev", reason="ok",
    )
    assert result.ok
    for role in ProductionRole:
        assert RoleCertificatesRepo(db_conn).list_for_worker_role(
            registered_worker, role.value,
        ) == []


def test_recording_any_role_certificate_never_changes_trust(db_conn, registered_worker, profile):
    manager = WorkerTrustManager(db_conn)
    before = manager.current_trust(registered_worker, "coder", "read_file")
    _record(
        db_conn, registered_worker, ProductionRole.CODER, profile, RoleQualificationOutcome.PASS,
    )
    after = manager.current_trust(registered_worker, "coder", "read_file")
    assert before == after == TrustLevel.LOCKED


# -- no transitive certification across roles --------------------------------

def test_a_planner_certificate_does_not_appear_under_any_other_role(
    db_conn, registered_worker, profile,
):
    _record(
        db_conn, registered_worker, ProductionRole.PLANNER, profile, RoleQualificationOutcome.PASS,
    )
    for other_role in (
        ProductionRole.CODER, ProductionRole.REVIEWER, ProductionRole.REPAIRER,
        ProductionRole.SECURITY,
    ):
        assert RoleCertificatesRepo(db_conn).list_for_worker_role(
            registered_worker, other_role.value,
        ) == []


def test_security_role_certificate_does_not_create_a_baseline_security_certificate(
    db_conn, registered_worker, profile,
):
    """Item 9/10's other half: a *dedicated Security-role* certificate
    (`ProductionRole.SECURITY`) is a completely different concept from
    the mandatory Baseline Security Certificate -- recording one must
    never create, or be confused with, the other."""
    from code_slayer.store.baseline_security_certificates_repo import (
        BaselineSecurityCertificatesRepo,
    )

    result = _record(
        db_conn, registered_worker, ProductionRole.SECURITY, profile,
        RoleQualificationOutcome.PASS,
    )
    assert result.ok
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []
