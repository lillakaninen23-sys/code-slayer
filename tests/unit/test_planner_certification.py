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
    ToolCallTransport,
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
from code_slayer.planning.qualification_evidence import (
    QUALIFICATION_EVIDENCE_KIND,
    read_planner_qualification_evidence,
)
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import connect, migrate
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.security_baseline import (
    RuntimeProfileIdentity,
    fingerprint_runtime_identity,
    runtime_profile_identity_from_config,
)

_REQUEST = PlannerRequest(original_request="Add a read-only endpoint.")


def _structured(**overrides) -> PlannerResponse:
    goal = overrides.pop("goal", "Add the requested read-only endpoint")
    output = PlannerStructuredOutput(goal=goal, **overrides)
    return PlannerResponse(PlannerOutcome.STRUCTURED, output=output, raw="{}")


def _non_tool_response() -> PlannerResponse:
    from code_slayer.planning.planner import PlannerFailureCategory

    return PlannerResponse(
        PlannerOutcome.MALFORMED,
        error="invalid_transport_response:text_response",
        failure_category=PlannerFailureCategory.NON_TOOL_RESPONSE,
    )


def _out_of_scope(**overrides) -> PlannerResponse:
    goal = overrides.pop("goal", "Add the requested read-only endpoint")
    output = PlannerStructuredOutput(
        goal=goal,
        affected_files=({"path": "outside/scope.py", "action": "modify", "reason": "x"},),
    )
    return PlannerResponse(PlannerOutcome.STRUCTURED, output=output, raw="{}")


_PROFILE = RuntimeContextProfile(
    model_tag="devstral:24b",
    effective_context_tokens=8192,
    output_token_budget=1024,
    model_digest="sha256:abc",
    endpoint="http://local:11436/v1",
    runtime_version="0.1.0",
    temperature=0.0,
)


def _pass_first_try_result(qualification_class="C"):
    planner = FakePlanner([_structured()])
    return run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class=qualification_class,
        context_profile=_PROFILE,
    )


def _pass_after_feedback_result(qualification_class="C"):
    planner = FakePlanner([_non_tool_response(), _structured()])
    return run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class=qualification_class,
        context_profile=_PROFILE,
    )


def _fail_capability_result(qualification_class="C"):
    planner = FakePlanner([_non_tool_response()])
    return run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class=qualification_class,
        context_profile=_PROFILE,
        max_correction_attempts=0,
    )


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "state.db")
    migrate(c)
    WorkersRepo(c).register(worker_id="w1", kind="fake", network_class="local")
    yield c
    c.close()


@pytest.fixture
def blobs_dir(tmp_path):
    directory = tmp_path / "blobs"
    directory.mkdir()
    return directory


def _certify(conn, blobs_dir, *, worker_id="w1", results, early_stopped=False, now_fn=None):
    kwargs = {} if now_fn is None else {"now_fn": now_fn}
    return certify_planner_from_qualification(
        conn,
        worker_id=worker_id,
        results=results,
        early_stopped=early_stopped,
        blobs_dir=blobs_dir,
        **kwargs,
    )


# -- strict policy: no partial credit, no arbitrary thresholds ---------------


def test_all_instances_pass_first_try_yields_a_strictly_classified_pass(conn, blobs_dir):
    results = (_pass_first_try_result(), _pass_first_try_result())
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=results,
        early_stopped=False,
    )
    assert result.ok
    assert result.certificate.outcome == "PASS"
    assert result.certificate.classification == "PASS_FIRST_TRY"
    assert result.certificate.role == "PLANNER"
    assert result.certificate.policy_version == PLANNER_CERTIFICATION_POLICY_VERSION


def test_one_corrected_instance_is_classified_pass_after_feedback_not_strict(conn, blobs_dir):
    """Item 23: bounded-correction qualification remains distinguishable
    from strict first-pass qualification, even when mixed with a
    genuinely strict-first-try instance in the same evidence set."""
    results = (_pass_first_try_result(), _pass_after_feedback_result())
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=results,
        early_stopped=False,
    )
    assert result.ok
    assert result.certificate.outcome == "PASS"
    assert result.certificate.classification == "PASS_AFTER_FEEDBACK"


def test_any_failing_instance_denies_the_whole_certificate_no_partial_credit(conn, blobs_dir):
    """A good outcome elsewhere in the evidence never averages out a
    genuine failure -- this mirrors conformance/promotion's own "every
    required case must pass" strictness."""
    results = (_pass_first_try_result(), _fail_capability_result())
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=results,
        early_stopped=False,
    )
    assert result.ok
    assert result.certificate.outcome == "FAIL"
    assert result.certificate.classification == QualificationOutcome.FAIL_CAPABILITY.value


def test_failure_classification_preserves_the_specific_qualification_outcome(conn, blobs_dir):
    """Malformed tool output, scope/policy violations, infrastructure
    errors, and disqualifiers must retain their own semantics rather
    than being flattened to a bare FAIL."""
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(_fail_capability_result(),),
        early_stopped=False,
    )
    assert result.ok
    assert result.certificate.classification == "FAIL_CAPABILITY"


# -- certification-boundary-level refusals: no certificate is recorded ------


def test_empty_evidence_is_refused_without_recording_anything(conn, blobs_dir):
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "empty_qualification_evidence"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_early_stopped_run_is_refused_without_recording_anything(conn, blobs_dir):
    """Repeated consecutive transport failures mean the evaluation could
    not even complete -- there is no complete evidence to certify from,
    so this must never be silently treated as a FAIL verdict either."""
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(_pass_first_try_result(),),
        early_stopped=True,
    )
    assert not result.ok
    assert result.reason == "qualification_run_early_stopped_insufficient_evidence"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_instance_with_no_provenance_is_refused(conn, blobs_dir):
    """Evidence produced with `unsafe_allow_unverified_environment=True`
    (no verified `context_profile`) was never bound to a specific,
    verified runtime and can never be certified against one."""
    planner = FakePlanner([_structured()])
    unverified = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        unsafe_allow_unverified_environment=True,
    )
    assert unverified.provenance == ()
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(unverified,),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "ambiguous_or_unverified_runtime_profile_in_evidence"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_ambiguous_runtime_profile_across_instances_is_refused(conn, blobs_dir):
    """Two instances whose provenance disagrees about which runtime was
    actually tested is ambiguous evidence -- never resolved by guessing
    one of them."""
    other_profile = RuntimeContextProfile(
        model_tag="a-different-model",
        effective_context_tokens=8192,
        model_digest="sha256:zzz",
        endpoint="http://other/v1",
        runtime_version="9.9.9",
        temperature=0.0,
    )
    planner_a = FakePlanner([_structured()])
    result_a = run_planner_case_with_correction(
        planner_a,
        _REQUEST,
        qualification_class="C",
        context_profile=_PROFILE,
    )
    planner_b = FakePlanner([_structured()])
    result_b = run_planner_case_with_correction(
        planner_b,
        _REQUEST,
        qualification_class="C",
        context_profile=other_profile,
    )
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(result_a, result_b),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "ambiguous_or_unverified_runtime_profile_in_evidence"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_insufficiently_specified_profile_in_evidence_is_refused(conn, blobs_dir):
    """`workers.security_baseline.RuntimeProfileIdentity.
    is_fully_specified` applies here too -- a Planner qualification run
    that never established `model_digest`/`endpoint`/`runtime_version`
    or a runtime-config fingerprint is refused, not silently certified
    against a weak binding."""
    loose_profile = RuntimeContextProfile(model_tag="devstral:24b", effective_context_tokens=8192)
    planner = FakePlanner([_structured()])
    result_ = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=loose_profile,
    )
    assert result_.provenance  # provenance exists, just incompletely specified
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(result_,),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "insufficient_runtime_profile_identity"


def test_missing_temperature_is_insufficient_runtime_profile_identity(conn, blobs_dir):
    """Model tag/digest/endpoint/runtime_version without an established
    sampling temperature cannot produce a runtime-config fingerprint,
    so the evidence is not a strong enough production binding."""
    no_temperature = RuntimeContextProfile(
        model_tag="devstral:24b",
        effective_context_tokens=8192,
        output_token_budget=1024,
        model_digest="sha256:abc",
        endpoint="http://local:11436/v1",
        runtime_version="0.1.0",
    )
    planner = FakePlanner([_structured()])
    evidence = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=no_temperature,
    )
    assert evidence.provenance
    assert evidence.provenance[0].runtime_config_fingerprint is None
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(evidence,),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "insufficient_runtime_profile_identity"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_malformed_results_type_is_refused(conn, blobs_dir):
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=("not a QualificationAttemptResult",),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "malformed_qualification_evidence"


# -- evidence_ref: deterministic, leak-free ----------------------------------


def test_evidence_ref_is_deterministic_and_never_contains_raw_text(conn, blobs_dir):
    results_1 = (_pass_first_try_result(),)
    results_2 = (_pass_first_try_result(),)  # a fresh, byte-for-byte-identical run
    result_1 = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=results_1,
        early_stopped=False,
    )
    fingerprint_1 = result_1.certificate.evidence_ref

    result_2 = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=results_2,
        early_stopped=False,
    )
    assert result_2.certificate.evidence_ref == fingerprint_1
    assert _REQUEST.original_request not in fingerprint_1
    assert "endpoint" not in fingerprint_1  # a hex digest, not a serialized dict
    meta = ContentStore(conn, blobs_dir).get_meta(fingerprint_1)
    assert meta is not None
    assert meta.source_kind == QUALIFICATION_EVIDENCE_KIND
    assert meta.exportable is False
    document = read_planner_qualification_evidence(
        conn,
        blobs_dir,
        fingerprint_1,
        expected_runtime_identity_fingerprint=result_1.certificate.runtime_identity_fingerprint,
        expected_role_evaluation_fingerprint=result_1.certificate.role_evaluation_fingerprint,
    )
    assert fingerprint_runtime_identity(document["runtime_identity_spec"]) == (
        result_1.certificate.runtime_identity_fingerprint
    )
    assert _REQUEST.original_request not in str(document)


# -- record_role_certificate() is the ONLY write path, correctly parameterized --


def test_certificate_binds_the_agreed_runtime_profile_from_evidence(conn, blobs_dir):
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(_pass_first_try_result(),),
        early_stopped=False,
    )
    cert = result.certificate
    expected = runtime_profile_identity_from_config(
        model_tag=_PROFILE.model_tag,
        model_digest=_PROFILE.model_digest,
        endpoint=_PROFILE.endpoint,
        runtime_version=_PROFILE.runtime_version,
        effective_context_tokens=_PROFILE.effective_context_tokens,
        temperature=_PROFILE.temperature,
        normalizer_id=_PROFILE.normalizer_id,
        normalizer_version=_PROFILE.normalizer_version,
    )
    identity = RuntimeProfileIdentity(
        model_tag=cert.model_tag,
        model_digest=cert.model_digest,
        endpoint=cert.endpoint,
        runtime_version=cert.runtime_version,
        normalizer_id=cert.normalizer_id,
        normalizer_version=cert.normalizer_version,
        runtime_identity_fingerprint=cert.runtime_identity_fingerprint,
    )
    assert identity.matches(expected)
    assert not identity.is_verified_current
    assert not identity.is_fully_specified
    assert expected.is_fully_specified
    assert expected.is_verified_current
    assert cert.role_evaluation_fingerprint is not None


def test_certification_boundary_has_no_override_parameter_for_its_own_decision():
    """Item 22: the transformation from qualification evidence to a
    certificate happens ONLY through this function's own strict policy
    -- there is no parameter letting a caller assert the outcome,
    classification, or role directly (those are always derived from
    `results` inside the function)."""
    params = set(inspect.signature(certify_planner_from_qualification).parameters)
    assert params == {"conn", "worker_id", "results", "early_stopped", "blobs_dir", "now_fn"}
    assert "outcome" not in params
    assert "classification" not in params
    assert "role" not in params


def test_worker_registration_creates_no_planner_certificate(conn):
    WorkersRepo(conn).register(worker_id="fresh", kind="fake", network_class="local")
    assert RoleCertificatesRepo(conn).list_for_worker_role("fresh", "PLANNER") == []


def test_a_fail_result_denies_even_though_qualification_module_never_writes_state(conn, blobs_dir):
    """`planning.qualification` itself never writes durable state (its
    own module docstring) -- this test confirms the certification
    boundary is the sole place a decision becomes durable, and that
    nothing about `planning.qualification`'s purity changes."""
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(_fail_capability_result(),),
        early_stopped=False,
    )
    assert result.ok  # a durable FAIL certificate IS recorded -- see docstring
    assert result.certificate.outcome == "FAIL"


def test_unknown_worker_is_refused(conn, blobs_dir):
    result = _certify(
        conn,
        blobs_dir,
        worker_id="never-registered",
        results=(_pass_first_try_result(),),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "unknown_worker"


def test_native_and_normalized_qualification_evidence_is_ambiguous(conn, blobs_dir):
    native = _pass_first_try_result()
    normalized_profile = RuntimeContextProfile(
        model_tag="devstral:24b",
        effective_context_tokens=8192,
        output_token_budget=1024,
        model_digest="sha256:abc",
        endpoint="http://local:11436/v1",
        runtime_version="0.1.0",
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
        temperature=0.0,
    )
    planner = FakePlanner([_structured()])
    normalized = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=normalized_profile,
    )
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(native, normalized),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "ambiguous_or_unverified_runtime_profile_in_evidence"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_normalized_qualification_evidence_binds_normalizer_identity(conn, blobs_dir):
    normalized_profile = RuntimeContextProfile(
        model_tag="devstral:24b",
        effective_context_tokens=8192,
        output_token_budget=1024,
        model_digest="sha256:abc",
        endpoint="http://local:11436/v1",
        runtime_version="0.1.0",
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
        temperature=0.0,
    )
    planner = FakePlanner([_structured()])
    evidence = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=normalized_profile,
    )
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(evidence,),
        early_stopped=False,
    )
    assert result.ok
    assert result.certificate.normalizer_id == "qwen_textual_tool_v1"
    assert result.certificate.normalizer_version == 1
    expected = runtime_profile_identity_from_config(
        model_tag=normalized_profile.model_tag,
        model_digest=normalized_profile.model_digest,
        endpoint=normalized_profile.endpoint,
        runtime_version=normalized_profile.runtime_version,
        effective_context_tokens=normalized_profile.effective_context_tokens,
        temperature=normalized_profile.temperature,
        normalizer_id=normalized_profile.normalizer_id,
        normalizer_version=normalized_profile.normalizer_version,
    )
    identity = RuntimeProfileIdentity(
        model_tag=result.certificate.model_tag,
        model_digest=result.certificate.model_digest,
        endpoint=result.certificate.endpoint,
        runtime_version=result.certificate.runtime_version,
        normalizer_id=result.certificate.normalizer_id,
        normalizer_version=result.certificate.normalizer_version,
        runtime_identity_fingerprint=result.certificate.runtime_identity_fingerprint,
    )
    assert identity.matches(expected)
    assert not identity.is_verified_current
    assert not identity.is_fully_specified
    assert expected.is_fully_specified


def test_certify_is_never_invoked_by_enabling_a_normalizer_on_the_planner():
    """Enabling the compatibility layer must not fabricate or upgrade a
    certificate. `WorkerAdapterPlanner` has no certification import."""
    import ast

    import code_slayer.planning.worker_planner as worker_planner_module

    source = open(worker_planner_module.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    imported = {
        alias.name for node in tree.body if isinstance(node, ast.ImportFrom) for alias in node.names
    }
    assert "certify_planner_from_qualification" not in imported
    assert "record_role_certificate" not in imported
    assert "record_baseline_certificate" not in imported


def test_normalized_transport_with_native_only_profile_is_refused(conn, blobs_dir):
    """A turn that actually used a compatibility decoder cannot be
    certified against a native-only runtime identity — the two must
    not be silently confused, even if every other identity field
    agrees."""
    planner = FakePlanner(
        [
            PlannerResponse(
                PlannerOutcome.STRUCTURED,
                output=PlannerStructuredOutput(goal="Add the requested read-only endpoint"),
                raw="{}",
                tool_call_transport=ToolCallTransport.NORMALIZED,
                normalizer_id="qwen_textual_tool_v1",
                normalizer_version=1,
            )
        ]
    )
    evidence = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=_PROFILE,
    )
    assert evidence.provenance[0].normalizer_id is None
    assert evidence.provenance[0].tool_call_transport == ToolCallTransport.NORMALIZED.value
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(evidence,),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "normalized_transport_without_normalizer_identity"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_temperature_mismatch_across_instances_is_ambiguous(conn, blobs_dir):
    """A Planner qualified at temperature=0.0 must not be certified from
    mixed evidence that also includes temperature=1.5 against the same
    model tag/digest/endpoint."""
    hot = RuntimeContextProfile(
        model_tag=_PROFILE.model_tag,
        effective_context_tokens=_PROFILE.effective_context_tokens,
        output_token_budget=_PROFILE.output_token_budget,
        model_digest=_PROFILE.model_digest,
        endpoint=_PROFILE.endpoint,
        runtime_version=_PROFILE.runtime_version,
        temperature=1.5,
    )
    planner = FakePlanner([_structured()])
    hot_result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=hot,
    )
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(_pass_first_try_result(), hot_result),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "ambiguous_or_unverified_runtime_profile_in_evidence"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_context_capacity_mismatch_across_instances_is_ambiguous(conn, blobs_dir):
    other = RuntimeContextProfile(
        model_tag=_PROFILE.model_tag,
        effective_context_tokens=16384,
        output_token_budget=_PROFILE.output_token_budget,
        model_digest=_PROFILE.model_digest,
        endpoint=_PROFILE.endpoint,
        runtime_version=_PROFILE.runtime_version,
        temperature=_PROFILE.temperature,
    )
    planner = FakePlanner([_structured()])
    other_result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=other,
    )
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(_pass_first_try_result(), other_result),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "ambiguous_or_unverified_runtime_profile_in_evidence"


def test_output_budget_mismatch_across_instances_is_ambiguous(conn, blobs_dir):
    other = RuntimeContextProfile(
        model_tag=_PROFILE.model_tag,
        effective_context_tokens=_PROFILE.effective_context_tokens,
        output_token_budget=4096,
        model_digest=_PROFILE.model_digest,
        endpoint=_PROFILE.endpoint,
        runtime_version=_PROFILE.runtime_version,
        temperature=_PROFILE.temperature,
    )
    planner = FakePlanner([_structured()])
    other_result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=other,
    )
    result = _certify(
        conn,
        blobs_dir,
        worker_id="w1",
        results=(_pass_first_try_result(), other_result),
        early_stopped=False,
    )
    assert not result.ok
    assert result.reason == "ambiguous_or_unverified_role_evaluation_in_evidence"
