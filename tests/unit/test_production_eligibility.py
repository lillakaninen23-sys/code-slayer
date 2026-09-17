"""Production eligibility (`workers.production_eligibility.
evaluate_production_eligibility`): the one authoritative gate combining
a Baseline Security Certificate (`workers.security_baseline`) and a Role
Certificate (`workers.role_qualification`) into a single fail-closed
decision.

Core invariant under test throughout: production eligibility requires
BOTH a valid Baseline Security Certificate AND a valid certificate for
the EXACT requested role — a strong role certificate never compensates
for a missing/failed/disqualifying Baseline Security Certificate, and
hard Security disqualifiers always win regardless of role-certificate
strength."""

from __future__ import annotations

import sqlite3

import pytest

from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.db import transaction
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.production_eligibility import (
    EligibilityDecision,
    evaluate_production_eligibility,
)
from code_slayer.workers.role_qualification import (
    ProductionRole,
    RoleQualificationOutcome,
    record_role_certificate,
)
from code_slayer.workers.security_baseline import (
    HardDisqualifierCategory,
    RuntimeProfileIdentity,
    SecurityBaselineOutcome,
    record_baseline_certificate,
    runtime_profile_identity_from_config,
)

POLICY_VERSION = "planner-certification-v1"


class _FakeClock:
    def __init__(self, start: str = "2026-01-01T00:00:00.000000Z") -> None:
        self.now = start

    def __call__(self) -> str:
        return self.now


@pytest.fixture
def registered_worker(db_conn) -> str:
    WorkersRepo(db_conn).register(worker_id="w1", kind="fake", network_class="local")
    return "w1"


def _full_profile(**overrides) -> RuntimeProfileIdentity:
    kwargs = dict(
        model_tag="devstral:24b",
        model_digest="sha256:abc",
        endpoint="http://local:11436/v1",
        runtime_version="0.1.0",
        effective_context_tokens=16384,
        output_token_budget=4096,
        temperature=0.0,
        tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
    )
    kwargs.update(overrides)
    return runtime_profile_identity_from_config(**kwargs)


@pytest.fixture
def profile() -> RuntimeProfileIdentity:
    return _full_profile()


def _security_pass(conn, worker_id, profile, *, now_fn=None, evidence_ref="sec-ev"):
    kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    return record_baseline_certificate(
        conn,
        worker_id=worker_id,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref=evidence_ref,
        reason="ok",
        **kwargs,
    )


def _security_fail(conn, worker_id, profile, *, now_fn=None, evidence_ref="sec-ev"):
    kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    return record_baseline_certificate(
        conn,
        worker_id=worker_id,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.FAIL,
        evidence_ref=evidence_ref,
        reason="unsafe",
        **kwargs,
    )


def _security_hard_disqualified(conn, worker_id, profile, *, now_fn=None, evidence_ref="sec-ev"):
    kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    return record_baseline_certificate(
        conn,
        worker_id=worker_id,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.HARD_DISQUALIFIED,
        evidence_ref=evidence_ref,
        reason="attempted_policy_bypass",
        hard_disqualifiers=(HardDisqualifierCategory.POLICY_OR_GATE_BYPASS_ATTEMPT,),
        **kwargs,
    )


def _role_pass(conn, worker_id, role, profile, *, now_fn=None, evidence_ref="role-ev"):
    kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    return record_role_certificate(
        conn,
        worker_id=worker_id,
        role=role,
        runtime_profile=profile,
        policy_version=POLICY_VERSION,
        outcome=RoleQualificationOutcome.PASS,
        classification="PASS_FIRST_TRY",
        evidence_ref=evidence_ref,
        reason="ok",
        **kwargs,
    )


def _role_fail(conn, worker_id, role, profile, *, now_fn=None, evidence_ref="role-ev"):
    kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    return record_role_certificate(
        conn,
        worker_id=worker_id,
        role=role,
        runtime_profile=profile,
        policy_version=POLICY_VERSION,
        outcome=RoleQualificationOutcome.FAIL,
        classification="FAIL_POLICY",
        evidence_ref=evidence_ref,
        reason="bad",
        **kwargs,
    )


def _evaluate(conn, worker_id, role, profile, *, policy_version=POLICY_VERSION):
    return evaluate_production_eligibility(
        conn,
        worker_id=worker_id,
        role=role,
        runtime_profile=profile,
        expected_role_policy_version=policy_version,
    )


# -- 1: both valid => eligible -----------------------------------------------


def test_baseline_security_pass_and_role_pass_is_eligible(db_conn, registered_worker, profile):
    security = _security_pass(db_conn, registered_worker, profile)
    role = _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(
        True,
        "eligible",
        security_certificate_id=security.certificate.certificate_id,
        role_certificate_id=role.certificate.certificate_id,
    )


# -- 2: Security PASS + missing role cert => denied --------------------------


def test_security_pass_with_missing_role_certificate_is_denied(
    db_conn,
    registered_worker,
    profile,
):
    security = _security_pass(db_conn, registered_worker, profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(
        False,
        "no_role_certificate",
        security_certificate_id=security.certificate.certificate_id,
    )


# -- 3: role PASS + missing Security cert => denied --------------------------


def test_role_pass_with_missing_security_certificate_is_denied(
    db_conn,
    registered_worker,
    profile,
):
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(False, "no_baseline_security_certificate")


# -- 4: Security FAIL + role PASS => denied ----------------------------------


def test_security_fail_with_role_pass_is_denied(db_conn, registered_worker, profile):
    security = _security_fail(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(
        False,
        "security_baseline_fail",
        security_certificate_id=security.certificate.certificate_id,
    )


# -- 5: Security hard-disqualified + role PASS => denied ---------------------


def test_security_hard_disqualifier_with_role_pass_is_denied(db_conn, registered_worker, profile):
    security = _security_hard_disqualified(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(
        False,
        "security_hard_disqualifier",
        security_certificate_id=security.certificate.certificate_id,
    )


# -- 6: role FAIL + Security PASS => denied ----------------------------------


def test_role_fail_with_security_pass_is_denied(db_conn, registered_worker, profile):
    security = _security_pass(db_conn, registered_worker, profile)
    role = _role_fail(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(
        False,
        "role_qualification_fail",
        security_certificate_id=security.certificate.certificate_id,
        role_certificate_id=role.certificate.certificate_id,
    )


# -- 7/8: a role certificate authorizes ONLY its own exact role -------------


def test_planner_certificate_does_not_authorize_coder(db_conn, registered_worker, profile):
    _security_pass(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.CODER, profile)
    assert not decision.eligible
    assert decision.reason == "no_role_certificate"


def test_coder_certificate_does_not_authorize_planner(db_conn, registered_worker, profile):
    _security_pass(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, ProductionRole.CODER, profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert not decision.eligible
    assert decision.reason == "no_role_certificate"


# -- 9/10: dedicated Security-role vs. mandatory baseline Security ----------


def test_security_role_certificate_does_not_replace_baseline_security_certificate(
    db_conn,
    registered_worker,
    profile,
):
    """A `ProductionRole.SECURITY` role certificate is not the mandatory
    Baseline Security Certificate -- without the latter, eligibility for
    the SECURITY role itself is still denied."""
    _role_pass(db_conn, registered_worker, ProductionRole.SECURITY, profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.SECURITY, profile)
    assert decision == EligibilityDecision(False, "no_baseline_security_certificate")


def test_baseline_security_certificate_does_not_create_security_role_eligibility(
    db_conn,
    registered_worker,
    profile,
):
    """The reverse: a valid Baseline Security Certificate alone never
    makes a worker eligible for the dedicated SECURITY role -- a real
    Role Certificate for SECURITY specifically is still required."""
    security = _security_pass(db_conn, registered_worker, profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.SECURITY, profile)
    assert decision == EligibilityDecision(
        False,
        "no_role_certificate",
        security_certificate_id=security.certificate.certificate_id,
    )


# -- 13: runtime-profile mismatch (either certificate) => denied ------------


def test_security_certificate_for_a_different_profile_is_denied(
    db_conn,
    registered_worker,
    profile,
):
    _security_pass(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    other_profile = _full_profile(
        model_tag="a-different-model",
        model_digest="sha256:zzz",
        endpoint="http://other/v1",
        runtime_version="9.9.9",
    )
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, other_profile)
    assert decision == EligibilityDecision(False, "baseline_security_certificate_profile_mismatch")


def test_role_certificate_for_a_different_profile_is_denied(db_conn, registered_worker, profile):
    other_profile = _full_profile(model_digest="sha256:different")
    security = _security_pass(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, other_profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(
        False,
        "role_certificate_profile_mismatch",
        security_certificate_id=security.certificate.certificate_id,
    )


# -- 14/15: qualification-policy/version mismatch or staleness => denied ----


def test_role_certificate_with_wrong_policy_version_is_denied(db_conn, registered_worker, profile):
    security = _security_pass(db_conn, registered_worker, profile)
    role = _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    decision = _evaluate(
        db_conn,
        registered_worker,
        ProductionRole.PLANNER,
        profile,
        policy_version="a-completely-different-policy-version",
    )
    assert decision == EligibilityDecision(
        False,
        "role_certificate_policy_version_stale",
        security_certificate_id=security.certificate.certificate_id,
        role_certificate_id=role.certificate.certificate_id,
    )


def test_role_certificate_recorded_under_a_stale_prior_policy_version_is_denied(
    db_conn,
    registered_worker,
    profile,
):
    """Item 15: a certificate that used to be current under an OLDER
    policy version is exactly as denied as one for an unrelated version
    -- staleness is just a policy_version mismatch by another name."""
    security = _security_pass(db_conn, registered_worker, profile)
    with transaction(db_conn):
        RoleCertificatesRepo(db_conn).record_in_transaction(
            certificate_id="stale-cert",
            worker_id=registered_worker,
            role=ProductionRole.PLANNER.value,
            policy_version="planner-certification-v0-retired",
            model_tag=profile.model_tag,
            model_digest=profile.model_digest,
            endpoint=profile.endpoint,
            runtime_version=profile.runtime_version,
            normalizer_id=profile.normalizer_id,
            normalizer_version=profile.normalizer_version,
            runtime_config_fingerprint=profile.runtime_config_fingerprint,
            outcome=RoleQualificationOutcome.PASS.value,
            classification="PASS_FIRST_TRY",
            evidence_ref="ev",
            reason="ok",
            issued_at="2020-01-01T00:00:00.000000Z",
        )
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(
        False,
        "role_certificate_policy_version_stale",
        security_certificate_id=security.certificate.certificate_id,
        role_certificate_id="stale-cert",
    )


def test_security_certificate_recorded_under_a_stale_baseline_version_is_denied(
    db_conn,
    registered_worker,
    profile,
):
    with transaction(db_conn):
        BaselineSecurityCertificatesRepo(db_conn).record_in_transaction(
            certificate_id="stale-sec-cert",
            worker_id=registered_worker,
            baseline_version="baseline-security-v0-retired",
            model_tag=profile.model_tag,
            model_digest=profile.model_digest,
            endpoint=profile.endpoint,
            runtime_version=profile.runtime_version,
            normalizer_id=profile.normalizer_id,
            normalizer_version=profile.normalizer_version,
            runtime_config_fingerprint=profile.runtime_config_fingerprint,
            outcome=SecurityBaselineOutcome.PASS.value,
            hard_disqualifiers_json="[]",
            evidence_ref="ev",
            reason="ok",
            issued_at="2020-01-01T00:00:00.000000Z",
        )
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(
        False,
        "baseline_security_certificate_policy_version_stale",
        security_certificate_id="stale-sec-cert",
    )


# -- 19: malformed/unknown persisted outcome fails closed --------------------


def test_role_certificate_with_unknown_persisted_outcome_fails_closed(
    db_conn,
    registered_worker,
    profile,
):
    security = _security_pass(db_conn, registered_worker, profile)
    with transaction(db_conn):
        RoleCertificatesRepo(db_conn).record_in_transaction(
            certificate_id="garbage-cert",
            worker_id=registered_worker,
            role=ProductionRole.PLANNER.value,
            policy_version=POLICY_VERSION,
            model_tag=profile.model_tag,
            model_digest=profile.model_digest,
            endpoint=profile.endpoint,
            runtime_version=profile.runtime_version,
            normalizer_id=profile.normalizer_id,
            normalizer_version=profile.normalizer_version,
            runtime_config_fingerprint=profile.runtime_config_fingerprint,
            outcome="SOMETHING_UNEXPECTED",
            classification="corrupted",
            evidence_ref="ev",
            reason="ok",
            issued_at="2026-01-01T00:00:00.000000Z",
        )
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(
        False,
        "role_qualification_fail",
        security_certificate_id=security.certificate.certificate_id,
        role_certificate_id="garbage-cert",
    )


def test_security_certificate_with_unknown_persisted_outcome_fails_closed(
    db_conn,
    registered_worker,
    profile,
):
    with transaction(db_conn):
        BaselineSecurityCertificatesRepo(db_conn).record_in_transaction(
            certificate_id="garbage-sec-cert",
            worker_id=registered_worker,
            baseline_version="baseline-security-v1",
            model_tag=profile.model_tag,
            model_digest=profile.model_digest,
            endpoint=profile.endpoint,
            runtime_version=profile.runtime_version,
            normalizer_id=profile.normalizer_id,
            normalizer_version=profile.normalizer_version,
            runtime_config_fingerprint=profile.runtime_config_fingerprint,
            outcome="SOMETHING_UNEXPECTED",
            hard_disqualifiers_json="[]",
            evidence_ref="ev",
            reason="ok",
            issued_at="2026-01-01T00:00:00.000000Z",
        )
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(
        False,
        "security_baseline_fail",
        security_certificate_id="garbage-sec-cert",
    )


# -- 20: persistence/read failure fails closed, never "eligible" ------------


def test_persistence_read_failure_is_never_converted_to_eligible(
    db_conn,
    registered_worker,
    profile,
):
    _security_pass(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    db_conn.close()
    with pytest.raises(sqlite3.Error):
        _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)


# -- 21: caller cannot fabricate eligibility ----------------------------------


def test_evaluate_production_eligibility_accepts_no_role_verdict_parameter():
    """Structural, not conventional: the production entry point's own
    signature has no parameter through which a caller could assert a
    role-qualification result -- both certificates are always loaded
    from durable evidence inside the function itself."""
    import inspect

    params = set(inspect.signature(evaluate_production_eligibility).parameters)
    assert "role_qualification" not in params
    assert "role_status" not in params
    assert params == {
        "conn",
        "worker_id",
        "role",
        "runtime_profile",
        "expected_role_policy_version",
    }


def test_no_role_certificate_denies_even_with_a_real_security_pass(
    db_conn,
    registered_worker,
    profile,
):
    """Without a REAL, durable role certificate, eligibility is denied
    regardless of anything else being true -- there is no way to talk
    the gate into "eligible" without real evidence of both kinds."""
    _security_pass(db_conn, registered_worker, profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert not decision.eligible


# -- 24: no role certificate is transitively created for another role -------


def test_evaluating_one_role_never_creates_or_affects_another_roles_certificate(
    db_conn,
    registered_worker,
    profile,
):
    _security_pass(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    # Evaluating PLANNER (which succeeds) must never fabricate or affect
    # CODER's own certificate state.
    planner_decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert planner_decision.eligible
    coder_decision = _evaluate(db_conn, registered_worker, ProductionRole.CODER, profile)
    assert not coder_decision.eligible
    assert coder_decision.reason == "no_role_certificate"
    assert (
        RoleCertificatesRepo(db_conn).list_for_worker_role(
            registered_worker,
            ProductionRole.CODER.value,
        )
        == []
    )


# -- fail-closed: unknown worker, malformed request, insufficient identity --


def test_unknown_worker_is_denied(db_conn, profile):
    decision = _evaluate(db_conn, "never-registered", ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(False, "unknown_worker")


def test_malformed_role_type_is_denied(db_conn, registered_worker, profile):
    decision = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role="planner",  # a plain string, not the enum
        runtime_profile=profile,
        expected_role_policy_version=POLICY_VERSION,
    )
    assert decision == EligibilityDecision(False, "malformed_eligibility_request")


def test_incompletely_specified_runtime_profile_is_denied(db_conn, registered_worker):
    weak_profile = RuntimeProfileIdentity(model_tag="devstral:24b")
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, weak_profile)
    assert decision == EligibilityDecision(False, "insufficient_runtime_profile_identity")


def test_incompletely_specified_profile_is_denied_even_with_a_matching_weak_certificate(
    db_conn,
    registered_worker,
):
    """A certificate CAN legitimately be recorded against a loosely
    specified profile (recording stays honest about what was actually
    established) -- but production eligibility must still refuse to
    treat it as authoritative."""
    weak_profile = RuntimeProfileIdentity(model_tag="devstral:24b")
    _security_pass(db_conn, registered_worker, weak_profile)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, weak_profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, weak_profile)
    assert decision == EligibilityDecision(False, "insufficient_runtime_profile_identity")


# -- stale superseded certificates never win over the latest evidence -------


def test_stale_superseded_security_certificate_never_preferred_over_the_latest(
    db_conn,
    registered_worker,
    profile,
):
    clock = _FakeClock("2026-01-01T00:00:00.000000Z")
    _security_pass(db_conn, registered_worker, profile, now_fn=clock, evidence_ref="old")
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    clock.now = "2026-01-02T00:00:00.000000Z"
    _security_fail(db_conn, registered_worker, profile, now_fn=clock, evidence_ref="new")

    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert not decision.eligible
    assert decision.reason == "security_baseline_fail"


# -- native vs compatibility-normalizer identity must not silently substitute


def test_native_certificate_does_not_authorize_a_normalized_runtime(
    db_conn,
    registered_worker,
    profile,
):
    _security_pass(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    normalized = _full_profile(
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
    )
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, normalized)
    assert decision == EligibilityDecision(False, "baseline_security_certificate_profile_mismatch")


def test_normalized_certificate_does_not_authorize_a_native_runtime(
    db_conn,
    registered_worker,
    profile,
):
    normalized = _full_profile(
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
    )
    _security_pass(db_conn, registered_worker, normalized)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, normalized)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(False, "baseline_security_certificate_profile_mismatch")


def test_matching_normalized_profiles_can_be_eligible(db_conn, registered_worker, profile):
    normalized = _full_profile(
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
    )
    security = _security_pass(db_conn, registered_worker, normalized)
    role = _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, normalized)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, normalized)
    assert decision == EligibilityDecision(
        True,
        "eligible",
        security_certificate_id=security.certificate.certificate_id,
        role_certificate_id=role.certificate.certificate_id,
    )


# -- runtime-config fingerprint: temperature/context/budget are identity -----


def test_identical_runtime_config_fingerprint_is_eligible(db_conn, registered_worker, profile):
    """The same exact factory-built profile still matches. Identity is
    exact, not 'close enough'."""
    security = _security_pass(db_conn, registered_worker, profile)
    role = _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    same_again = _full_profile()
    assert same_again.matches(profile)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, same_again)
    assert decision == EligibilityDecision(
        True,
        "eligible",
        security_certificate_id=security.certificate.certificate_id,
        role_certificate_id=role.certificate.certificate_id,
    )


def test_temperature_mismatch_is_denied(db_conn, registered_worker, profile):
    _security_pass(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    hotter = _full_profile(temperature=1.5)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, hotter)
    assert decision == EligibilityDecision(False, "baseline_security_certificate_profile_mismatch")


def test_effective_context_mismatch_is_denied(db_conn, registered_worker, profile):
    _security_pass(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    smaller = _full_profile(effective_context_tokens=8192)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, smaller)
    assert decision == EligibilityDecision(False, "baseline_security_certificate_profile_mismatch")


def test_output_token_budget_mismatch_is_denied(db_conn, registered_worker, profile):
    _security_pass(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    smaller = _full_profile(output_token_budget=1024)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, smaller)
    assert decision == EligibilityDecision(False, "baseline_security_certificate_profile_mismatch")


def test_legacy_null_fingerprint_does_not_authorize_fully_specified_runtime(
    db_conn,
    registered_worker,
    profile,
):
    """A pre-v14 certificate (NULL fingerprint) with otherwise identical
    model/endpoint/runtime/normalizer fields must not authorize a
    current fully-specified runtime. None is never a wildcard."""
    legacy = RuntimeProfileIdentity(
        model_tag=profile.model_tag,
        model_digest=profile.model_digest,
        endpoint=profile.endpoint,
        runtime_version=profile.runtime_version,
        normalizer_id=profile.normalizer_id,
        normalizer_version=profile.normalizer_version,
        runtime_config_fingerprint=None,
    )
    assert not legacy.is_fully_specified
    _security_pass(db_conn, registered_worker, legacy)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, legacy)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, profile)
    assert decision == EligibilityDecision(False, "baseline_security_certificate_profile_mismatch")


def test_missing_runtime_config_fingerprint_is_insufficient(db_conn, registered_worker):
    """Tag/digest/endpoint/runtime_version without a fingerprint is not
    a production-authoritative identity, even if certificates exist for
    that incomplete profile."""
    incomplete = RuntimeProfileIdentity(
        model_tag="devstral:24b",
        model_digest="sha256:abc",
        endpoint="http://local:11436/v1",
        runtime_version="0.1.0",
    )
    _security_pass(db_conn, registered_worker, incomplete)
    _role_pass(db_conn, registered_worker, ProductionRole.PLANNER, incomplete)
    decision = _evaluate(db_conn, registered_worker, ProductionRole.PLANNER, incomplete)
    assert decision == EligibilityDecision(False, "insufficient_runtime_profile_identity")
