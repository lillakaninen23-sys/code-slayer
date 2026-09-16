"""Baseline Security Certification foundation: certificate
representation/recording (`workers.security_baseline`) and the
production-eligibility gate (`workers.production_eligibility`).

Core invariant under test throughout: production eligibility requires
BOTH a valid Baseline Security Certificate AND a valid role-specific
qualification result — a strong role-qualification result never
compensates for a missing/failed/disqualifying certificate, and hard
Security disqualifiers always win regardless of role-qualification
strength."""

from __future__ import annotations

import json

import pytest

from code_slayer.audit.events import EventType
from code_slayer.audit.verify import verify_chain
from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.db import migrate
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.production_eligibility import (
    EligibilityDecision,
    RoleQualificationStatus,
    is_worker_eligible,
)
from code_slayer.workers.promotion import promote_from_conformance
from code_slayer.workers.security_baseline import (
    BASELINE_VERSION,
    HardDisqualifierCategory,
    RuntimeProfileIdentity,
    SecurityBaselineOutcome,
    SecurityCertificationResult,
    record_baseline_certificate,
)
from code_slayer.workers.trust import TrustLevel, WorkerTrustManager


class _FakeClock:
    def __init__(self, start: str = "2026-01-01T00:00:00.000000Z") -> None:
        self.now = start

    def __call__(self) -> str:
        return self.now


@pytest.fixture
def registered_worker(db_conn) -> str:
    WorkersRepo(db_conn).register(worker_id="w1", kind="fake", network_class="local")
    return "w1"


@pytest.fixture
def profile() -> RuntimeProfileIdentity:
    return RuntimeProfileIdentity(model_tag="devstral:24b", endpoint="http://local:11436/v1")


def _pass(conn, worker_id, profile, *, evidence_ref="evidence-1", now_fn=None, reason="ok"):
    kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    return record_baseline_certificate(
        conn, worker_id=worker_id, runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS, evidence_ref=evidence_ref, reason=reason, **kwargs,
    )


def _fail(conn, worker_id, profile, *, evidence_ref="evidence-1", now_fn=None, reason="unsafe"):
    kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    return record_baseline_certificate(
        conn, worker_id=worker_id, runtime_profile=profile,
        outcome=SecurityBaselineOutcome.FAIL, evidence_ref=evidence_ref, reason=reason, **kwargs,
    )


def _hard_disqualified(conn, worker_id, profile, *, evidence_ref="evidence-1", now_fn=None):
    kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    return record_baseline_certificate(
        conn, worker_id=worker_id, runtime_profile=profile,
        outcome=SecurityBaselineOutcome.HARD_DISQUALIFIED, evidence_ref=evidence_ref,
        reason="attempted_policy_bypass",
        hard_disqualifiers=(HardDisqualifierCategory.POLICY_OR_GATE_BYPASS_ATTEMPT,), **kwargs,
    )


# -- RuntimeProfileIdentity: exact matching, no wildcards --------------------

def test_runtime_profile_identity_requires_nonempty_model_tag():
    with pytest.raises(ValueError):
        RuntimeProfileIdentity(model_tag="")


def test_runtime_profile_identity_matches_require_every_field_equal():
    a = RuntimeProfileIdentity(model_tag="m", model_digest="d1", endpoint="e", runtime_version="v")
    b = RuntimeProfileIdentity(model_tag="m", model_digest="d1", endpoint="e", runtime_version="v")
    c = RuntimeProfileIdentity(model_tag="m", model_digest="d2", endpoint="e", runtime_version="v")
    assert a.matches(b)
    assert not a.matches(c)


def test_runtime_profile_identity_none_never_matches_a_set_value():
    a = RuntimeProfileIdentity(model_tag="m")
    b = RuntimeProfileIdentity(model_tag="m", model_digest="d1")
    assert not a.matches(b)
    assert not b.matches(a)


# -- record_baseline_certificate(): fail-closed validation -------------------

def test_pass_certificate_requires_evidence_reference(db_conn, registered_worker, profile):
    result = record_baseline_certificate(
        db_conn, worker_id=registered_worker, runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS, evidence_ref="   ", reason="ok",
    )
    assert result == SecurityCertificationResult(False, "missing_evidence_reference")


def test_fail_certificate_also_requires_evidence_reference(db_conn, registered_worker, profile):
    """A certificate is never a bare boolean -- FAIL needs provenance
    exactly as much as PASS does."""
    result = record_baseline_certificate(
        db_conn, worker_id=registered_worker, runtime_profile=profile,
        outcome=SecurityBaselineOutcome.FAIL, evidence_ref="", reason="unsafe",
    )
    assert not result.ok
    assert result.reason == "missing_evidence_reference"


def test_hard_disqualified_requires_at_least_one_disqualifier(db_conn, registered_worker, profile):
    result = record_baseline_certificate(
        db_conn, worker_id=registered_worker, runtime_profile=profile,
        outcome=SecurityBaselineOutcome.HARD_DISQUALIFIED, evidence_ref="ev", reason="bad",
        hard_disqualifiers=(),
    )
    assert result == SecurityCertificationResult(
        False, "hard_disqualified_requires_at_least_one_disqualifier",
    )


def test_pass_outcome_rejects_disqualifiers_being_attached(db_conn, registered_worker, profile):
    """A hard disqualifier can never be silently attached to a PASS --
    that inconsistency is refused outright, never coerced."""
    result = record_baseline_certificate(
        db_conn, worker_id=registered_worker, runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS, evidence_ref="ev", reason="ok",
        hard_disqualifiers=(HardDisqualifierCategory.DESTRUCTIVE_BEHAVIOR,),
    )
    assert result == SecurityCertificationResult(
        False, "hard_disqualifiers_only_valid_for_hard_disqualified_outcome",
    )


def test_certificate_for_unregistered_worker_is_refused(db_conn, profile):
    result = _pass(db_conn, "never-registered", profile)
    assert result == SecurityCertificationResult(False, "unknown_worker")
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker("never-registered") == []


def test_malformed_outcome_type_is_refused(db_conn, registered_worker, profile):
    result = record_baseline_certificate(
        db_conn, worker_id=registered_worker, runtime_profile=profile,
        outcome="PASS",  # a plain string, not SecurityBaselineOutcome.PASS
        evidence_ref="ev", reason="ok",
    )
    assert result == SecurityCertificationResult(False, "malformed_certificate_request")


def test_malformed_runtime_profile_type_is_refused(db_conn, registered_worker):
    result = record_baseline_certificate(
        db_conn, worker_id=registered_worker, runtime_profile="devstral:24b",
        outcome=SecurityBaselineOutcome.PASS, evidence_ref="ev", reason="ok",
    )
    assert result == SecurityCertificationResult(False, "malformed_certificate_request")


# -- persistence round-trip (item 12) ----------------------------------------

def test_certificate_round_trip_across_reopen(tmp_path):
    from code_slayer.store.db import connect

    path = tmp_path / "state.db"
    conn = connect(path)
    migrate(conn)
    WorkersRepo(conn).register(worker_id="w1", kind="fake", network_class="local")
    profile_ = RuntimeProfileIdentity(model_tag="devstral:24b", runtime_version="0.1.0")
    result = record_baseline_certificate(
        conn, worker_id="w1", runtime_profile=profile_, outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="conformance-run-abc", reason="baseline_checks_passed",
    )
    assert result.ok
    certificate_id = result.certificate.certificate_id
    conn.close()

    reopened = connect(path)
    migrate(reopened)
    fetched = BaselineSecurityCertificatesRepo(reopened).get(certificate_id)
    assert fetched is not None
    assert fetched.worker_id == "w1"
    assert fetched.model_tag == "devstral:24b"
    assert fetched.runtime_version == "0.1.0"
    assert fetched.model_digest is None
    assert fetched.outcome == "PASS"
    assert fetched.evidence_ref == "conformance-run-abc"
    assert fetched.baseline_version == BASELINE_VERSION

    decision = is_worker_eligible(
        reopened, worker_id="w1", role="coder", runtime_profile=profile_,
        role_qualification=RoleQualificationStatus.PASS,
    )
    assert decision.eligible
    reopened.close()


# -- append-only schema (item 13 companion: migration/persistence) ----------

def test_certificate_table_is_append_only(db_conn, registered_worker, profile):
    import sqlite3

    result = _pass(db_conn, registered_worker, profile)
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "UPDATE worker_baseline_security_certificates SET outcome = 'FAIL' "
            "WHERE certificate_id = ?",
            (result.certificate.certificate_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "DELETE FROM worker_baseline_security_certificates WHERE certificate_id = ?",
            (result.certificate.certificate_id,),
        )


# -- audit/provenance (item 14) ----------------------------------------------

def test_recording_a_certificate_is_audited(db_conn, registered_worker, profile):
    result = _pass(db_conn, registered_worker, profile, reason="baseline_checks_passed")
    rows = [
        dict(r) for r in db_conn.execute(
            "SELECT event_type, payload_json FROM audit_events WHERE task_id IS NULL "
            "ORDER BY seq",
        )
    ]
    matching = [r for r in rows if r["event_type"] == "SECURITY_BASELINE_CERTIFICATE_RECORDED"]
    assert len(matching) == 1
    payload = json.loads(matching[0]["payload_json"])
    assert payload["certificate_id"] == result.certificate.certificate_id
    assert payload["worker_id"] == registered_worker
    assert payload["outcome"] == "PASS"
    assert payload["evidence_ref"] == "evidence-1"
    assert verify_chain(db_conn, task_id=None).ok


def test_hard_disqualified_certificate_is_also_audited_with_its_categories(
    db_conn, registered_worker, profile,
):
    _hard_disqualified(db_conn, registered_worker, profile)
    rows = [
        dict(r) for r in db_conn.execute(
            "SELECT payload_json FROM audit_events WHERE event_type = ?",
            (EventType.SECURITY_BASELINE_CERTIFICATE_RECORDED.value,),
        )
    ]
    payload = json.loads(rows[0]["payload_json"])
    assert payload["outcome"] == "HARD_DISQUALIFIED"
    assert payload["hard_disqualifiers"] == ["POLICY_OR_GATE_BYPASS_ATTEMPT"]


# -- registration/trust never fabricate a certificate (items 8, 9) ----------

def test_worker_registration_creates_no_security_certificate(db_conn):
    WorkersRepo(db_conn).register(worker_id="fresh-worker", kind="fake", network_class="local")
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker("fresh-worker") == []


def test_trust_promotion_creates_no_security_certificate(db_conn, registered_worker):
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
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


# -- certificate never mutates trust (item 10) -------------------------------

def test_recording_any_certificate_outcome_never_changes_trust(db_conn, registered_worker, profile):
    manager = WorkerTrustManager(db_conn)
    before = manager.current_trust(registered_worker, "coder", "read_file")
    _pass(db_conn, registered_worker, profile)
    _fail(db_conn, registered_worker, profile, evidence_ref="ev2")
    _hard_disqualified(db_conn, registered_worker, profile, evidence_ref="ev3")
    after = manager.current_trust(registered_worker, "coder", "read_file")
    assert before == after == TrustLevel.LOCKED


# -- is_worker_eligible(): the core invariant (items 1-7) --------------------

def test_role_qualification_alone_without_baseline_security_is_ineligible(
    db_conn, registered_worker, profile,
):
    decision = is_worker_eligible(
        db_conn, worker_id=registered_worker, role="planner", runtime_profile=profile,
        role_qualification=RoleQualificationStatus.PASS,
    )
    assert decision == EligibilityDecision(False, "no_baseline_security_certificate")


def test_baseline_security_pass_alone_without_role_qualification_is_ineligible(
    db_conn, registered_worker, profile,
):
    cert = _pass(db_conn, registered_worker, profile)
    decision = is_worker_eligible(
        db_conn, worker_id=registered_worker, role="planner", runtime_profile=profile,
        role_qualification=RoleQualificationStatus.MISSING,
    )
    assert decision == EligibilityDecision(
        False, "role_qualification_missing", certificate_id=cert.certificate.certificate_id,
    )


def test_both_valid_is_eligible(db_conn, registered_worker, profile):
    cert = _pass(db_conn, registered_worker, profile)
    decision = is_worker_eligible(
        db_conn, worker_id=registered_worker, role="planner", runtime_profile=profile,
        role_qualification=RoleQualificationStatus.PASS,
    )
    assert decision == EligibilityDecision(
        True, "eligible", certificate_id=cert.certificate.certificate_id,
    )


def test_security_fail_with_role_pass_is_ineligible(db_conn, registered_worker, profile):
    """A good role-qualification score never compensates for a Security
    FAIL."""
    cert = _fail(db_conn, registered_worker, profile)
    decision = is_worker_eligible(
        db_conn, worker_id=registered_worker, role="planner", runtime_profile=profile,
        role_qualification=RoleQualificationStatus.PASS,
    )
    assert decision == EligibilityDecision(
        False, "security_baseline_fail", certificate_id=cert.certificate.certificate_id,
    )


def test_hard_disqualifier_with_role_pass_is_ineligible(db_conn, registered_worker, profile):
    """Hard Security disqualifiers win regardless of role-qualification
    strength -- the literal core invariant."""
    cert = _hard_disqualified(db_conn, registered_worker, profile)
    decision = is_worker_eligible(
        db_conn, worker_id=registered_worker, role="planner", runtime_profile=profile,
        role_qualification=RoleQualificationStatus.PASS,
    )
    assert decision == EligibilityDecision(
        False, "security_hard_disqualifier", certificate_id=cert.certificate.certificate_id,
    )


def test_certificate_for_a_different_runtime_profile_is_ineligible(
    db_conn, registered_worker, profile,
):
    """Item 6: a certificate exists, but for the WRONG runtime/profile."""
    _pass(db_conn, registered_worker, profile)
    different_profile = RuntimeProfileIdentity(model_tag="a-completely-different-model")
    decision = is_worker_eligible(
        db_conn, worker_id=registered_worker, role="planner", runtime_profile=different_profile,
        role_qualification=RoleQualificationStatus.PASS,
    )
    assert decision == EligibilityDecision(False, "baseline_security_certificate_profile_mismatch")


def test_stale_superseded_certificate_is_never_preferred_over_the_latest(
    db_conn, registered_worker, profile,
):
    """Item 7: an older PASS for the exact same profile is superseded by
    a later FAIL -- eligibility must reflect the latest evidence, never
    silently reuse the earlier, now-stale PASS."""
    clock = _FakeClock("2026-01-01T00:00:00.000000Z")
    _pass(db_conn, registered_worker, profile, now_fn=clock, evidence_ref="ev-old")
    clock.now = "2026-01-02T00:00:00.000000Z"
    _fail(db_conn, registered_worker, profile, now_fn=clock, evidence_ref="ev-new")

    decision = is_worker_eligible(
        db_conn, worker_id=registered_worker, role="planner", runtime_profile=profile,
        role_qualification=RoleQualificationStatus.PASS,
    )
    assert not decision.eligible
    assert decision.reason == "security_baseline_fail"


def test_a_later_pass_correctly_supersedes_an_earlier_hard_disqualifier(
    db_conn, registered_worker, profile,
):
    """The reverse of the previous test: re-evaluation is not itself
    forbidden, and the latest evidence always governs -- only the most
    recent certificate for a matching profile is ever consulted."""
    clock = _FakeClock("2026-01-01T00:00:00.000000Z")
    _hard_disqualified(db_conn, registered_worker, profile, now_fn=clock, evidence_ref="ev-old")
    clock.now = "2026-01-02T00:00:00.000000Z"
    _pass(db_conn, registered_worker, profile, now_fn=clock, evidence_ref="ev-new")

    decision = is_worker_eligible(
        db_conn, worker_id=registered_worker, role="planner", runtime_profile=profile,
        role_qualification=RoleQualificationStatus.PASS,
    )
    assert decision.eligible


def test_unknown_worker_is_ineligible(db_conn, profile):
    decision = is_worker_eligible(
        db_conn, worker_id="never-registered", role="planner", runtime_profile=profile,
        role_qualification=RoleQualificationStatus.PASS,
    )
    assert decision == EligibilityDecision(False, "unknown_worker")


def test_malformed_eligibility_request_is_refused(db_conn, registered_worker, profile):
    decision = is_worker_eligible(
        db_conn, worker_id=registered_worker, role="planner", runtime_profile=profile,
        role_qualification="PASS",  # a plain string, not RoleQualificationStatus.PASS
    )
    assert decision == EligibilityDecision(False, "malformed_eligibility_request")


# -- no transitive certification across roles (item 11) ---------------------

def test_role_qualification_never_carries_across_roles(db_conn, registered_worker, profile):
    """A PASS role_qualification supplied for 'planner' has no bearing
    whatsoever on a separate eligibility check for 'coder' with the same
    worker and the same valid Baseline Security certificate -- each call
    supplies its own role_qualification independently, and there is no
    shared/derived state connecting them."""
    _pass(db_conn, registered_worker, profile)

    planner_decision = is_worker_eligible(
        db_conn, worker_id=registered_worker, role="planner", runtime_profile=profile,
        role_qualification=RoleQualificationStatus.PASS,
    )
    assert planner_decision.eligible

    coder_decision = is_worker_eligible(
        db_conn, worker_id=registered_worker, role="coder", runtime_profile=profile,
        role_qualification=RoleQualificationStatus.MISSING,
    )
    assert not coder_decision.eligible
    assert coder_decision.reason == "role_qualification_missing"


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
