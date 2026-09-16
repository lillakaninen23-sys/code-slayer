"""Planner role certification boundary (`planning.planner_certification`):
turning completed `planning.qualification` evidence into a durable
`ProductionRole.PLANNER` role certificate.

Retains and reuses `planning.qualification`'s own real evaluation
machinery (`run_planner_case_with_correction`, `RuntimeContextProfile`,
`FakePlanner`) — this module never invents a second qualification
harness."""

from __future__ import annotations

import inspect

import pytest

from code_slayer.planning.fake_planner import FakePlanner
from code_slayer.planning.planner import (
    PlannerOutcome,
    PlannerRequest,
    PlannerResponse,
    PlannerStructuredOutput,
)
from code_slayer.planning.planner_certification import (
    PLANNER_CERTIFICATION_POLICY_VERSION,
    certify_planner_from_qualification,
)
from code_slayer.planning.qualification import (
    QualificationOutcome,
    RuntimeContextProfile,
    run_planner_case_with_correction,
)
from code_slayer.store.db import connect, migrate
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.security_baseline import RuntimeProfileIdentity

_REQUEST = PlannerRequest(original_request="Add a read-only endpoint.")


def _structured(**overrides) -> PlannerResponse:
    goal = overrides.pop("goal", "Add the requested read-only endpoint")
    output = PlannerStructuredOutput(goal=goal, **overrides)
    return PlannerResponse(PlannerOutcome.STRUCTURED, output=output, raw="{}")


def _non_tool_response() -> PlannerResponse:
    from code_slayer.planning.planner import PlannerFailureCategory

    return PlannerResponse(
        PlannerOutcome.MALFORMED, error="invalid_transport_response:text_response",
        failure_category=PlannerFailureCategory.NON_TOOL_RESPONSE,
    )


def _out_of_scope(**overrides) -> PlannerResponse:
    goal = overrides.pop("goal", "Add the requested read-only endpoint")
    output = PlannerStructuredOutput(
        goal=goal, affected_files=(
            {"path": "outside/scope.py", "action": "modify", "reason": "x"},
        ),
    )
    return PlannerResponse(PlannerOutcome.STRUCTURED, output=output, raw="{}")


_PROFILE = RuntimeContextProfile(
    model_tag="devstral:24b", effective_context_tokens=8192, output_token_budget=1024,
    model_digest="sha256:abc", endpoint="http://local:11436/v1", runtime_version="0.1.0",
)


def _pass_first_try_result(qualification_class="C"):
    planner = FakePlanner([_structured()])
    return run_planner_case_with_correction(
        planner, _REQUEST, qualification_class=qualification_class, context_profile=_PROFILE,
    )


def _pass_after_feedback_result(qualification_class="C"):
    planner = FakePlanner([_non_tool_response(), _structured()])
    return run_planner_case_with_correction(
        planner, _REQUEST, qualification_class=qualification_class, context_profile=_PROFILE,
    )


def _fail_capability_result(qualification_class="C"):
    planner = FakePlanner([_non_tool_response()])
    return run_planner_case_with_correction(
        planner, _REQUEST, qualification_class=qualification_class, context_profile=_PROFILE,
        max_correction_attempts=0,
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.db")
    migrate(c)
    WorkersRepo(c).register(worker_id="w1", kind="fake", network_class="local")
    yield c
    c.close()


# -- strict policy: no partial credit, no arbitrary thresholds ---------------

def test_all_instances_pass_first_try_yields_a_strictly_classified_pass(conn):
    results = (_pass_first_try_result(), _pass_first_try_result())
    result = certify_planner_from_qualification(
        conn, worker_id="w1", results=results, early_stopped=False,
    )
    assert result.ok
    assert result.certificate.outcome == "PASS"
    assert result.certificate.classification == "PASS_FIRST_TRY"
    assert result.certificate.role == "PLANNER"
    assert result.certificate.policy_version == PLANNER_CERTIFICATION_POLICY_VERSION


def test_one_corrected_instance_is_classified_pass_after_feedback_not_strict(conn):
    """Item 23: bounded-correction qualification remains distinguishable
    from strict first-pass qualification, even when mixed with a
    genuinely strict-first-try instance in the same evidence set."""
    results = (_pass_first_try_result(), _pass_after_feedback_result())
    result = certify_planner_from_qualification(
        conn, worker_id="w1", results=results, early_stopped=False,
    )
    assert result.ok
    assert result.certificate.outcome == "PASS"
    assert result.certificate.classification == "PASS_AFTER_FEEDBACK"


def test_any_failing_instance_denies_the_whole_certificate_no_partial_credit(conn):
    """A good outcome elsewhere in the evidence never averages out a
    genuine failure -- this mirrors conformance/promotion's own "every
    required case must pass" strictness."""
    results = (_pass_first_try_result(), _fail_capability_result())
    result = certify_planner_from_qualification(
        conn, worker_id="w1", results=results, early_stopped=False,
    )
    assert result.ok
    assert result.certificate.outcome == "FAIL"
    assert result.certificate.classification == QualificationOutcome.FAIL_CAPABILITY.value


def test_failure_classification_preserves_the_specific_qualification_outcome(conn):
    """Malformed tool output, scope/policy violations, infrastructure
    errors, and disqualifiers must retain their own semantics rather
    than being flattened to a bare FAIL."""
    result = certify_planner_from_qualification(
        conn, worker_id="w1", results=(_fail_capability_result(),), early_stopped=False,
    )
    assert result.ok
    assert result.certificate.classification == "FAIL_CAPABILITY"


# -- certification-boundary-level refusals: no certificate is recorded ------

def test_empty_evidence_is_refused_without_recording_anything(conn):
    result = certify_planner_from_qualification(
        conn, worker_id="w1", results=(), early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "empty_qualification_evidence"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_early_stopped_run_is_refused_without_recording_anything(conn):
    """Repeated consecutive transport failures mean the evaluation could
    not even complete -- there is no complete evidence to certify from,
    so this must never be silently treated as a FAIL verdict either."""
    result = certify_planner_from_qualification(
        conn, worker_id="w1", results=(_pass_first_try_result(),), early_stopped=True,
    )
    assert not result.ok
    assert result.reason == "qualification_run_early_stopped_insufficient_evidence"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_instance_with_no_provenance_is_refused(conn):
    """Evidence produced with `unsafe_allow_unverified_environment=True`
    (no verified `context_profile`) was never bound to a specific,
    verified runtime and can never be certified against one."""
    planner = FakePlanner([_structured()])
    unverified = run_planner_case_with_correction(
        planner, _REQUEST, qualification_class="C", unsafe_allow_unverified_environment=True,
    )
    assert unverified.provenance == ()
    result = certify_planner_from_qualification(
        conn, worker_id="w1", results=(unverified,), early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "ambiguous_or_unverified_runtime_profile_in_evidence"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_ambiguous_runtime_profile_across_instances_is_refused(conn):
    """Two instances whose provenance disagrees about which runtime was
    actually tested is ambiguous evidence -- never resolved by guessing
    one of them."""
    other_profile = RuntimeContextProfile(
        model_tag="a-different-model", effective_context_tokens=8192,
        model_digest="sha256:zzz", endpoint="http://other/v1", runtime_version="9.9.9",
    )
    planner_a = FakePlanner([_structured()])
    result_a = run_planner_case_with_correction(
        planner_a, _REQUEST, qualification_class="C", context_profile=_PROFILE,
    )
    planner_b = FakePlanner([_structured()])
    result_b = run_planner_case_with_correction(
        planner_b, _REQUEST, qualification_class="C", context_profile=other_profile,
    )
    result = certify_planner_from_qualification(
        conn, worker_id="w1", results=(result_a, result_b), early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "ambiguous_or_unverified_runtime_profile_in_evidence"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_insufficiently_specified_profile_in_evidence_is_refused(conn):
    """`workers.security_baseline.RuntimeProfileIdentity.
    is_fully_specified` applies here too -- a Planner qualification run
    that never established `model_digest`/`endpoint`/`runtime_version`
    is refused, not silently certified against a weak binding."""
    loose_profile = RuntimeContextProfile(model_tag="devstral:24b", effective_context_tokens=8192)
    planner = FakePlanner([_structured()])
    result_ = run_planner_case_with_correction(
        planner, _REQUEST, qualification_class="C", context_profile=loose_profile,
    )
    assert result_.provenance  # provenance exists, just incompletely specified
    result = certify_planner_from_qualification(
        conn, worker_id="w1", results=(result_,), early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "insufficient_runtime_profile_identity"


def test_malformed_results_type_is_refused(conn):
    result = certify_planner_from_qualification(
        conn, worker_id="w1", results=("not a QualificationAttemptResult",), early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "malformed_qualification_evidence"


# -- evidence_ref: deterministic, leak-free ----------------------------------

def test_evidence_ref_is_deterministic_and_never_contains_raw_text(conn):
    results_1 = (_pass_first_try_result(),)
    results_2 = (_pass_first_try_result(),)  # a fresh, byte-for-byte-identical run
    result_1 = certify_planner_from_qualification(
        conn, worker_id="w1", results=results_1, early_stopped=False,
    )
    fingerprint_1 = result_1.certificate.evidence_ref

    result_2 = certify_planner_from_qualification(
        conn, worker_id="w1", results=results_2, early_stopped=False,
    )
    assert result_2.certificate.evidence_ref == fingerprint_1
    assert _REQUEST.original_request not in fingerprint_1
    assert "endpoint" not in fingerprint_1  # a hex digest, not a serialized dict


# -- record_role_certificate() is the ONLY write path, correctly parameterized --

def test_certificate_binds_the_agreed_runtime_profile_from_evidence(conn):
    result = certify_planner_from_qualification(
        conn, worker_id="w1", results=(_pass_first_try_result(),), early_stopped=False,
    )
    cert = result.certificate
    identity = RuntimeProfileIdentity(
        model_tag=cert.model_tag, model_digest=cert.model_digest, endpoint=cert.endpoint,
        runtime_version=cert.runtime_version,
    )
    assert identity == RuntimeProfileIdentity(
        model_tag="devstral:24b", model_digest="sha256:abc", endpoint="http://local:11436/v1",
        runtime_version="0.1.0",
    )
    assert identity.is_fully_specified


def test_certification_boundary_has_no_override_parameter_for_its_own_decision():
    """Item 22: the transformation from qualification evidence to a
    certificate happens ONLY through this function's own strict policy
    -- there is no parameter letting a caller assert the outcome,
    classification, or role directly (those are always derived from
    `results` inside the function)."""
    params = set(inspect.signature(certify_planner_from_qualification).parameters)
    assert params == {"conn", "worker_id", "results", "early_stopped", "now_fn"}
    assert "outcome" not in params
    assert "classification" not in params
    assert "role" not in params


def test_worker_registration_creates_no_planner_certificate(conn):
    WorkersRepo(conn).register(worker_id="fresh", kind="fake", network_class="local")
    assert RoleCertificatesRepo(conn).list_for_worker_role("fresh", "PLANNER") == []


def test_a_fail_result_denies_even_though_qualification_module_never_writes_state(conn):
    """`planning.qualification` itself never writes durable state (its
    own module docstring) -- this test confirms the certification
    boundary is the sole place a decision becomes durable, and that
    nothing about `planning.qualification`'s purity changes."""
    result = certify_planner_from_qualification(
        conn, worker_id="w1", results=(_fail_capability_result(),), early_stopped=False,
    )
    assert result.ok  # a durable FAIL certificate IS recorded -- see docstring
    assert result.certificate.outcome == "FAIL"


def test_unknown_worker_is_refused(conn):
    result = certify_planner_from_qualification(
        conn, worker_id="never-registered", results=(_pass_first_try_result(),),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "unknown_worker"
