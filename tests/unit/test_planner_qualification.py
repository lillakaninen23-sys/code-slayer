"""The planner qualification harness (Phase 8.2e).

Covers: correct classification of a genuine tool call, a text response,
a transport timeout, and malformed/schema-invalid params, each into a
distinct category (never collapsed); the strict-parser boundary is never
bypassed here (no second parser, no prose recovery); metrics aggregate
exact counts/rates; a case runs exactly the repetitions requested; the
module cannot grant trust/permission/policy authority, structurally;
and this harness never selects a production planner itself.
"""

from __future__ import annotations

import ast
from dataclasses import replace

import pytest

from code_slayer.intelligence.service import RepositoryIntelligenceService
from code_slayer.planning.fake_planner import FakePlanner
from code_slayer.planning.planner import (
    PlannerAffectedFileProposal,
    PlannerFailureCategory,
    PlannerOutcome,
    PlannerRequest,
    PlannerResponse,
    PlannerStructuredOutput,
    ToolCallTransport,
)
from code_slayer.planning.qualification import (
    DEFAULT_MAX_CORRECTION_ATTEMPTS,
    AttemptProvenance,
    PlannerTrial,
    QualificationOutcome,
    RuntimeContextProfile,
    TokenMeasurement,
    TokenMeasurementSource,
    ToolTransportOutcome,
    TrialOutcome,
    VerifiedExpectedInput,
    actual_evaluated_measurement_from_usage,
    aggregate_planner_trials,
    build_attempt_provenance,
    classify_planner_response,
    estimate_request_token_measurement,
    estimate_request_tokens,
    observe_determinism,
    payload_fingerprint,
    preflight_check,
    run_corrected_planner_case,
    run_planner_case,
    run_planner_case_with_correction,
    run_planner_trial,
    run_tool_transport_case,
    run_tool_transport_trial,
    verify_full_input_preservation,
)
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.protocol import (
    ToolRequirement,
    WorkerAdapterError,
    WorkerRequest,
    WorkerResponse,
    WorkerResponseKind,
    WorkerToolCall,
    WorkerUsage,
)

_REQUEST = PlannerRequest(original_request="Add a read-only endpoint.")


def _structured(**overrides) -> PlannerResponse:
    # Goal deliberately shares a significant word ("endpoint") with
    # _REQUEST's own task text so pre-existing tests that assume a
    # generically "valid" plan aren't incidentally rejected by the
    # task-relevance gate (Phase 8.2e's success-semantics revision).
    goal = overrides.pop("goal", "Add the requested read-only endpoint")
    output = PlannerStructuredOutput(goal=goal, **overrides)
    return PlannerResponse(PlannerOutcome.STRUCTURED, output=output, raw="{}")


def _snapshot(git_repo_with_commit):
    service = RepositoryIntelligenceService(git_repo_with_commit)
    try:
        return service.inspect()
    finally:
        service.close()


# --- 1. TOOL_CALL counted correctly ------------------------------------------


def test_genuine_tool_call_classified_as_valid_structured_plan():
    outcome, detail, hint = classify_planner_response(_structured())
    assert outcome == TrialOutcome.VALID_STRUCTURED_PLAN
    assert detail is None


def test_run_tool_transport_trial_counts_a_genuine_tool_call():
    adapter = FakeWorkerAdapter(
        [
            WorkerResponse(
                kind=WorkerResponseKind.TOOL_CALL,
                tool_call=WorkerToolCall("read_file", {"path": "a"}),
            ),
        ]
    )
    request = WorkerRequest(
        task_id="t",
        role="planner",
        original_prompt="read a",
        allowed_tools=("read_file",),
        tool_requirement=ToolRequirement.REQUIRED,
    )
    trial = run_tool_transport_trial(adapter, request)
    assert trial.outcome == ToolTransportOutcome.GENUINE_TOOL_CALL


# --- 2. TEXT counted as NON_TOOL_RESPONSE ------------------------------------


def test_text_response_is_non_tool_response():
    response = PlannerResponse(
        PlannerOutcome.MALFORMED,
        error="invalid_transport_response:text_response",
        failure_category=PlannerFailureCategory.NON_TOOL_RESPONSE,
    )
    outcome, _detail, _hint = classify_planner_response(response)
    assert outcome == TrialOutcome.NON_TOOL_RESPONSE


def test_tool_transport_text_response_is_non_tool_response():
    adapter = FakeWorkerAdapter(
        [
            WorkerResponse(kind=WorkerResponseKind.TEXT, text="sure, I'll help"),
        ]
    )
    request = WorkerRequest(
        task_id="t",
        role="planner",
        original_prompt="read a",
        allowed_tools=("read_file",),
        tool_requirement=ToolRequirement.REQUIRED,
    )
    trial = run_tool_transport_trial(adapter, request)
    assert trial.outcome == ToolTransportOutcome.NON_TOOL_RESPONSE


# --- 3. transport timeout categorized separately -----------------------------


def test_transport_timeout_is_distinct_from_other_transport_errors():
    timeout = PlannerResponse(
        PlannerOutcome.MALFORMED,
        error="adapter_error:transport_timeout",
        failure_category=PlannerFailureCategory.TRANSPORT_ERROR,
    )
    other = PlannerResponse(
        PlannerOutcome.MALFORMED,
        error="adapter_error:transport_connection_failed",
        failure_category=PlannerFailureCategory.TRANSPORT_ERROR,
    )
    assert classify_planner_response(timeout)[0] == TrialOutcome.TRANSPORT_TIMEOUT
    assert classify_planner_response(other)[0] == TrialOutcome.TRANSPORT_ERROR


def test_tool_transport_timeout_is_distinct_from_other_transport_errors():
    request = WorkerRequest(task_id="t", role="planner", original_prompt="x")
    timeout_adapter = FakeWorkerAdapter([WorkerAdapterError("transport_timeout")])
    error_adapter = FakeWorkerAdapter([WorkerAdapterError("transport_connection_failed")])
    timeout_trial = run_tool_transport_trial(timeout_adapter, request)
    error_trial = run_tool_transport_trial(error_adapter, request)
    assert timeout_trial.outcome == ToolTransportOutcome.TRANSPORT_TIMEOUT
    assert error_trial.outcome == ToolTransportOutcome.TRANSPORT_ERROR


# --- 4. malformed args categorized separately --------------------------------


def test_schema_invalid_tool_call_is_distinct_from_non_tool_response():
    response = PlannerResponse(
        PlannerOutcome.MALFORMED,
        error="malformed_structured_output",
        failure_category=PlannerFailureCategory.SCHEMA_INVALID,
    )
    outcome, _detail, _hint = classify_planner_response(response)
    assert outcome == TrialOutcome.TOOL_SCHEMA_INVALID
    assert outcome != TrialOutcome.NON_TOOL_RESPONSE


# --- 5. strict parser rejection preserved ------------------------------------


def test_classification_never_reads_raw_text_or_error_prose():
    """A response carrying plan-shaped-looking free text in `raw` must
    never be salvaged into a valid outcome -- classification looks only
    at `outcome`/`output`/`failure_category`, never `raw`."""
    response = PlannerResponse(
        PlannerOutcome.MALFORMED,
        raw='{"goal": "a fully valid-looking plan", "requirements": []}',
        error="invalid_transport_response:text_response",
        failure_category=PlannerFailureCategory.NON_TOOL_RESPONSE,
    )
    outcome, _detail, _hint = classify_planner_response(response)
    assert outcome == TrialOutcome.NON_TOOL_RESPONSE


def test_evidence_grounded_rejection_uses_only_the_existing_strict_validator(git_repo_with_commit):
    snapshot = _snapshot(git_repo_with_commit)
    # Claims to modify a file that does not exist in the real snapshot.
    response = _structured(
        affected_files=(
            PlannerAffectedFileProposal(path="does_not_exist.py", action="modify", reason="x"),
        )
    )
    outcome, detail, hint = classify_planner_response(response, snapshot)
    assert outcome == TrialOutcome.PLAN_VALIDATION_REJECTED
    assert hint == "DRAFT"
    assert "does_not_exist.py" in detail


def test_evidence_grounded_success_reports_ready_or_needs_input(git_repo_with_commit):
    snapshot = _snapshot(git_repo_with_commit)
    ready = classify_planner_response(_structured(), snapshot)
    assert ready[0] == TrialOutcome.VALID_STRUCTURED_PLAN
    assert ready[2] == "READY"


# --- 6. metrics aggregate correctly ------------------------------------------


def test_metrics_aggregate_exact_counts_and_rates():
    trials = (
        run_planner_trial(FakePlanner([_structured()]), _REQUEST),
        run_planner_trial(
            FakePlanner(
                [
                    PlannerResponse(
                        PlannerOutcome.MALFORMED,
                        error="adapter_error:transport_timeout",
                        failure_category=PlannerFailureCategory.TRANSPORT_ERROR,
                    )
                ]
            ),
            _REQUEST,
        ),
        run_planner_trial(
            FakePlanner(
                [
                    PlannerResponse(
                        PlannerOutcome.MALFORMED,
                        error="invalid_transport_response:text_response",
                        failure_category=PlannerFailureCategory.NON_TOOL_RESPONSE,
                    )
                ]
            ),
            _REQUEST,
        ),
        run_planner_trial(
            FakePlanner(
                [
                    PlannerResponse(
                        PlannerOutcome.MALFORMED,
                        error="malformed_structured_output",
                        failure_category=PlannerFailureCategory.SCHEMA_INVALID,
                    )
                ]
            ),
            _REQUEST,
        ),
    )
    metrics = aggregate_planner_trials("case", "candidate", trials)
    assert metrics.repetitions == 4
    assert metrics.outcome_counts[TrialOutcome.VALID_STRUCTURED_PLAN.value] == 1
    assert metrics.outcome_counts[TrialOutcome.TRANSPORT_TIMEOUT.value] == 1
    assert metrics.outcome_counts[TrialOutcome.NON_TOOL_RESPONSE.value] == 1
    assert metrics.outcome_counts[TrialOutcome.TOOL_SCHEMA_INVALID.value] == 1
    # genuine tool call = VALID_STRUCTURED_PLAN + TOOL_SCHEMA_INVALID (2/4)
    assert metrics.genuine_tool_call_rate == pytest.approx(0.5)
    assert metrics.fully_valid_rate == pytest.approx(0.25)
    assert metrics.non_tool_response_rate == pytest.approx(0.25)
    assert metrics.transport_failure_rate == pytest.approx(0.25)
    assert metrics.schema_valid_rate_of_genuine == pytest.approx(0.5)


def test_metrics_never_collapse_transport_and_correctness_into_one_number():
    trials = (
        run_planner_trial(
            FakePlanner(
                [
                    PlannerResponse(
                        PlannerOutcome.MALFORMED,
                        error="adapter_error:transport_timeout",
                        failure_category=PlannerFailureCategory.TRANSPORT_ERROR,
                    )
                ]
            ),
            _REQUEST,
        ),
    )
    metrics = aggregate_planner_trials("case", "candidate", trials)
    assert metrics.transport_failure_rate == 1.0
    assert metrics.non_tool_response_rate == 0.0
    assert metrics.genuine_tool_call_rate == 0.0


# --- 7. repeated trial counts are exact --------------------------------------


def test_run_planner_case_runs_exactly_the_requested_repetitions():
    planner = FakePlanner([_structured() for _ in range(7)])
    trials, cold_start = run_planner_case(planner, _REQUEST, repetitions=7)
    assert len(trials) == 7
    assert cold_start is None
    assert len(planner.calls) == 7


def test_run_planner_case_warm_up_is_excluded_from_trials_and_metrics():
    planner = FakePlanner([_structured() for _ in range(6)])
    trials, cold_start = run_planner_case(planner, _REQUEST, repetitions=5, warm_up=True)
    assert len(trials) == 5
    assert cold_start is not None
    assert len(planner.calls) == 6  # 1 warm-up + 5 measured


def test_run_tool_transport_case_runs_exactly_the_requested_repetitions():
    adapter = FakeWorkerAdapter(
        [
            WorkerResponse(
                kind=WorkerResponseKind.TOOL_CALL,
                tool_call=WorkerToolCall("read_file", {"path": "a"}),
            )
            for _ in range(3)
        ]
    )
    request = WorkerRequest(
        task_id="t",
        role="planner",
        original_prompt="x",
        allowed_tools=("read_file",),
        tool_requirement=ToolRequirement.REQUIRED,
    )
    trials, cold_start = run_tool_transport_case(adapter, request, repetitions=3)
    assert len(trials) == 3
    assert cold_start is None


# --- 8/11. no qualification result can grant trust/authority ----------------


def test_qualification_module_imports_no_trust_policy_lease_or_permission_authority():
    import code_slayer.planning.qualification as qualification_module

    forbidden = {
        "code_slayer.tools.executor",
        "code_slayer.policy.engine",
        "code_slayer.lease.manager",
        "code_slayer.repo.checkpoint",
        "code_slayer.workers.trust",
        "code_slayer.workers.promotion",
        "code_slayer.permissions.service",
        "code_slayer.permissions",
    }
    tree = ast.parse(open(qualification_module.__file__, encoding="utf-8").read())
    imported = _imported_modules(tree)
    overlap = {f for f in forbidden if any(name.startswith(f) for name in imported)}
    assert not overlap, f"qualification module imports forbidden: {overlap}"


def _imported_modules(tree: ast.Module) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


# --- 9. no production planner selection occurs automatically ----------------


def test_qualification_module_exposes_no_production_selection_function():
    import code_slayer.planning.qualification as qualification_module

    forbidden_substrings = (
        "select",
        "promote",
        "configure_production",
        "set_production",
        "activate",
    )
    public_names = [name for name in vars(qualification_module) if not name.startswith("_")]
    offending = [
        name for name in public_names if any(sub in name.lower() for sub in forbidden_substrings)
    ]
    assert not offending, f"qualification module exposes a selection-shaped name: {offending}"


# --- 10. no prose recovery ----------------------------------------------------


def test_classify_planner_response_source_never_references_raw_or_error_text_content():
    """Structural proof, not just behavioral: `classify_planner_response`'s
    own source touches `response.error` only to check for the literal
    substring `timeout` (an already-known, code-owned adapter error
    vocabulary), and never touches `response.raw`/`response.text` at
    all -- there is no code path that could parse a plan out of them."""
    import inspect

    from code_slayer.planning.qualification import classify_planner_response

    source = inspect.getsource(classify_planner_response)
    assert "response.raw" not in source
    assert "response.text" not in source
    assert ".raw" not in source


# --- determinism / payload verification helpers ------------------------------


def test_observe_determinism_reports_when_outcomes_or_structure_differ():
    stable = tuple(run_planner_trial(FakePlanner([_structured()]), _REQUEST) for _ in range(3))
    report = observe_determinism(stable)
    assert report["protocol_form_changed"] is False
    assert report["structure_changed"] is False

    changing = (
        run_planner_trial(FakePlanner([_structured()]), _REQUEST),
        run_planner_trial(FakePlanner([_structured(requirements=("a", "b"))]), _REQUEST),
    )
    report2 = observe_determinism(changing)
    assert report2["structure_changed"] is True


def test_payload_fingerprint_is_stable_and_reflects_tool_choice():
    payload_a = {
        "model": "m",
        "tool_choice": "required",
        "temperature": 0.0,
        "stream": False,
        "tools": [{"function": {"name": "emit_engineering_plan"}}],
    }
    payload_b = dict(payload_a, tool_choice=None)
    assert payload_fingerprint(payload_a) == payload_fingerprint(dict(payload_a))
    assert payload_fingerprint(payload_a) != payload_fingerprint(payload_b)


# --- 12. existing tests remain green: verified by the full suite, not here --


# =============================================================================
# Self-correction / retry (Phase 8.2e extension)
# =============================================================================


def _non_tool_response() -> PlannerResponse:
    return PlannerResponse(
        PlannerOutcome.MALFORMED,
        error="invalid_transport_response:text_response",
        failure_category=PlannerFailureCategory.NON_TOOL_RESPONSE,
    )


def _transport_timeout() -> PlannerResponse:
    return PlannerResponse(
        PlannerOutcome.MALFORMED,
        error="adapter_error:transport_timeout",
        failure_category=PlannerFailureCategory.TRANSPORT_ERROR,
    )


def _transport_error() -> PlannerResponse:
    return PlannerResponse(
        PlannerOutcome.MALFORMED,
        error="adapter_error:transport_connection_failed",
        failure_category=PlannerFailureCategory.TRANSPORT_ERROR,
    )


# --- A. first attempt PASS -> no unnecessary retries -------------------------


def test_correction_a_first_attempt_pass_has_no_retries():
    planner = FakePlanner([_structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.PASS_FIRST_TRY
    assert result.attempt_count == 1
    assert result.feedback == ()
    assert len(planner.calls) == 1


# --- B. correctable failure -> structured feedback -> retry -> PASS_AFTER_FEEDBACK


def test_correction_b_correctable_failure_gets_feedback_and_retries_to_pass():
    planner = FakePlanner([_non_tool_response(), _structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.PASS_AFTER_FEEDBACK
    assert result.attempt_count == 2
    assert len(result.feedback) == 1
    assert "emit_engineering_plan" in result.feedback[0].render()
    assert planner.calls[0].prior_attempt_feedback is None
    assert planner.calls[1].prior_attempt_feedback == result.feedback[0].render()


def test_correction_b_evidence_rejection_feedback_names_the_rejected_claim(git_repo_with_commit):
    snapshot = _snapshot(git_repo_with_commit)
    rejected = _structured(
        affected_files=(
            PlannerAffectedFileProposal(path="does_not_exist.py", action="modify", reason="x"),
        )
    )
    planner = FakePlanner([rejected, _structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        snapshot=snapshot,
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.PASS_AFTER_FEEDBACK
    assert "does_not_exist.py" in result.feedback[0].observed_behaviour


# --- C. failure persists past retry budget -> correct final failure --------


def test_correction_c_failure_persists_past_retry_budget_is_fail_capability():
    responses = [_non_tool_response() for _ in range(DEFAULT_MAX_CORRECTION_ATTEMPTS + 1)]
    planner = FakePlanner(responses)
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.FAIL_CAPABILITY
    assert result.attempt_count == DEFAULT_MAX_CORRECTION_ATTEMPTS + 1
    assert len(result.feedback) == DEFAULT_MAX_CORRECTION_ATTEMPTS


def test_correction_c_max_correction_attempts_zero_means_no_retry_at_all():
    planner = FakePlanner([_non_tool_response()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        max_correction_attempts=0,
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.FAIL_CAPABILITY
    assert result.attempt_count == 1
    assert result.feedback == ()


# --- D. transport timeout -> NOT classified as capability failure ----------


def test_correction_d_transport_timeout_is_not_capability_failure():
    planner = FakePlanner([_transport_timeout()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="A",
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.FAIL_TRANSPORT_TIMEOUT
    assert result.outcome != QualificationOutcome.FAIL_CAPABILITY
    assert result.attempt_count == 1  # never retried -- nothing to correct
    assert result.feedback == ()


# --- E. repeated transport timeouts -> early stop ---------------------------


def test_correction_e_repeated_transport_timeouts_trigger_early_stop():
    planner = FakePlanner([_transport_timeout()] * 10)
    results, early_stopped = run_corrected_planner_case(
        planner,
        _REQUEST,
        qualification_class="A",
        repetitions=10,
        early_stop_after_consecutive_transport_failures=3,
        unsafe_allow_unverified_environment=True,
    )
    assert early_stopped is True
    assert len(results) == 3
    assert all(r.outcome == QualificationOutcome.FAIL_TRANSPORT_TIMEOUT for r in results)


def test_correction_e_single_timeout_does_not_trigger_early_stop():
    planner = FakePlanner([_transport_timeout(), _structured(), _structured()])
    results, early_stopped = run_corrected_planner_case(
        planner,
        _REQUEST,
        qualification_class="A",
        repetitions=3,
        early_stop_after_consecutive_transport_failures=3,
        unsafe_allow_unverified_environment=True,
    )
    assert early_stopped is False
    assert len(results) == 3


# --- F. runtime/tool failure -> preserves correct failure category ---------


def test_correction_f_runtime_error_is_fail_runtime_not_capability():
    planner = FakePlanner([_transport_error()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="A",
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.FAIL_RUNTIME
    assert result.attempt_count == 1
    assert result.feedback == ()


# --- G. policy/scope rules cannot be bypassed by retry ----------------------
#
# Supersedes the prior decision that FAIL_POLICY/FAIL_SCOPE were
# "reserved, not produced today": planning now has a real, deterministic
# policy/scope dimension (a plan's own claimed `affected_files` checked
# against `tools.file_tools.relative_path()`/`within()`), and these
# tests prove it -- while still proving retry can never make a
# persistent violation quietly disappear.


def _out_of_scope(**overrides):
    return _structured(
        affected_files=(
            PlannerAffectedFileProposal(path="src/bar.py", action="modify", reason="x"),
        ),
        **overrides,
    )


def _policy_forbidden(**overrides):
    return _structured(
        affected_files=(
            PlannerAffectedFileProposal(path="../outside.py", action="modify", reason="x"),
        ),
        **overrides,
    )


def test_correction_g_scope_violation_corrects_to_a_pass():
    """The user-facing example: attempt 1 touches an out-of-scope path,
    gets feedback naming it, attempt 2 stays in scope -- a legitimate
    PASS_AFTER_FEEDBACK, with the violation still visible in the chain."""
    planner = FakePlanner([_out_of_scope(), _structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        allowed_scope=("src/foo.py",),
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.PASS_AFTER_FEEDBACK
    assert result.attempts[0].outcome == TrialOutcome.SCOPE_VIOLATION
    assert "src/bar.py" in result.feedback[0].observed_behaviour
    assert result.attempts[1].outcome == TrialOutcome.VALID_STRUCTURED_PLAN


def test_correction_g_scope_violation_exhausted_is_fail_scope_not_capability():
    responses = [_out_of_scope() for _ in range(DEFAULT_MAX_CORRECTION_ATTEMPTS + 1)]
    planner = FakePlanner(responses)
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        allowed_scope=("src/foo.py",),
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.FAIL_SCOPE
    assert result.outcome != QualificationOutcome.FAIL_CAPABILITY
    assert result.attempt_count == DEFAULT_MAX_CORRECTION_ATTEMPTS + 1


def test_correction_g_policy_violation_corrects_to_a_pass():
    planner = FakePlanner([_policy_forbidden(), _structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="A",
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.PASS_AFTER_FEEDBACK
    assert result.attempts[0].outcome == TrialOutcome.POLICY_VIOLATION
    assert "../outside.py" in result.feedback[0].observed_behaviour


def test_correction_g_policy_violation_exhausted_is_fail_policy_never_a_pass():
    """Retry can never soften or bypass the policy check: the identical
    forbidden path on every attempt never becomes a pass, and the final
    outcome is distinguishably FAIL_POLICY, not FAIL_CAPABILITY."""
    responses = [_policy_forbidden() for _ in range(DEFAULT_MAX_CORRECTION_ATTEMPTS + 1)]
    planner = FakePlanner(responses)
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="A",
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.FAIL_POLICY
    assert result.outcome not in (
        QualificationOutcome.PASS_FIRST_TRY,
        QualificationOutcome.PASS_AFTER_FEEDBACK,
        QualificationOutcome.FAIL_CAPABILITY,
    )
    assert all(a.outcome == TrialOutcome.POLICY_VIOLATION for a in result.attempts)


def test_correction_g_policy_check_always_runs_even_without_a_declared_scope():
    """Universal: policy is checked whether or not the caller ever
    declares `allowed_scope` -- never opt-in, unlike scope."""
    planner = FakePlanner([_policy_forbidden()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="A",
        max_correction_attempts=0,
        unsafe_allow_unverified_environment=True,
    )
    assert result.attempts[0].outcome == TrialOutcome.POLICY_VIOLATION


def test_correction_g_no_declared_scope_never_produces_scope_violation():
    """Backward compatible: omitting `allowed_scope` (every pre-existing
    caller) skips scope checking entirely -- an affected_files claim
    that would be out-of-scope under some hypothetical declaration is
    simply never flagged when no scope was ever declared."""
    planner = FakePlanner([_out_of_scope()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        max_correction_attempts=0,
        unsafe_allow_unverified_environment=True,
    )
    assert result.attempts[0].outcome == TrialOutcome.VALID_STRUCTURED_PLAN


def test_correction_g_feedback_never_leaks_the_answer_or_expands_scope():
    """Structured feedback repeats only the model's OWN claimed path(s)
    back to it and the originally allowed scope -- never a hint at the
    qualification task's solution, and never an offer of more scope than
    the original task had."""
    planner = FakePlanner([_out_of_scope(), _structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        allowed_scope=("src/foo.py",),
        unsafe_allow_unverified_environment=True,
    )
    rendered = result.feedback[0].render()
    assert "src/bar.py" in rendered  # repeats the model's own claim back
    assert "solution" not in rendered.lower()
    assert "answer" not in rendered.lower()
    # never grants scope beyond what the task originally allowed
    assert "you may now" not in rendered.lower()
    assert "unrestricted" not in rendered.lower()
    assert "no longer restricted" not in rendered.lower()


def test_correction_g_a_second_rejected_attempt_never_becomes_a_pass(git_repo_with_commit):
    """Retrying never loosens evidence validation or schema strictness:
    if every attempt is still rejected, the final outcome is still a
    failure, never silently promoted to a pass."""
    snapshot = _snapshot(git_repo_with_commit)
    rejected = _structured(
        affected_files=(
            PlannerAffectedFileProposal(path="does_not_exist.py", action="modify", reason="x"),
        )
    )
    planner = FakePlanner([rejected, rejected, rejected])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        snapshot=snapshot,
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.FAIL_CAPABILITY
    assert result.outcome not in (
        QualificationOutcome.PASS_FIRST_TRY,
        QualificationOutcome.PASS_AFTER_FEEDBACK,
    )


# --- H. audit/provenance contains the whole attempt chain -------------------


def test_correction_h_result_reconstructs_the_full_attempt_chain():
    planner = FakePlanner([_non_tool_response(), _non_tool_response(), _structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.PASS_AFTER_FEEDBACK
    assert result.attempt_count == 3
    assert [t.outcome for t in result.attempts] == [
        TrialOutcome.NON_TOOL_RESPONSE,
        TrialOutcome.NON_TOOL_RESPONSE,
        TrialOutcome.VALID_STRUCTURED_PLAN,
    ]
    assert len(result.feedback) == 2
    assert [f.attempt_number for f in result.feedback] == [1, 2]
    assert result.final_trial.outcome == TrialOutcome.VALID_STRUCTURED_PLAN


def test_correction_h_provenance_reconstructs_a_scope_violation_chain():
    """The attempt/provenance chain for a scope-violation correction is
    just as fully reconstructable as any other correctable failure."""
    profile = RuntimeContextProfile(
        model_tag="m",
        effective_context_tokens=8192,
        output_token_budget=1024,
    )
    planner = FakePlanner([_out_of_scope(), _structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        allowed_scope=("src/foo.py",),
        context_profile=profile,
    )
    assert result.outcome == QualificationOutcome.PASS_AFTER_FEEDBACK
    assert len(result.provenance) == 2
    assert result.provenance[0].outcome == TrialOutcome.SCOPE_VIOLATION.value
    assert result.provenance[0].attempt_number == 1
    assert result.provenance[1].outcome == TrialOutcome.VALID_STRUCTURED_PLAN.value
    assert result.provenance[1].attempt_number == 2
    # attempt 2's provenance binds to feedback -- the correction actually happened.
    assert result.provenance[1].feedback_fingerprint is not None


def test_correction_h_repeated_run_is_deterministically_reproducible():
    """Same canned response sequence in, byte-for-byte identical attempt/
    feedback/outcome chain out -- no hidden state, no randomness."""

    def _run():
        planner = FakePlanner([_out_of_scope(), _structured()])
        return run_planner_case_with_correction(
            planner,
            _REQUEST,
            qualification_class="C",
            allowed_scope=("src/foo.py",),
            unsafe_allow_unverified_environment=True,
        )

    first, second = _run(), _run()
    assert first.outcome == second.outcome
    assert [t.outcome for t in first.attempts] == [t.outcome for t in second.attempts]
    assert [f.render() for f in first.feedback] == [f.render() for f in second.feedback]


# --- G/H (canonical taxonomy). RUNTIME_TOOL_FAILURE / INFRASTRUCTURE_FAILURE -
#
# This role has no real, side-effecting tool execution (the Planner
# never runs create_file/write_file) -- there is nothing that could fail
# as "the tool itself broke" distinctly from "the transport/connection
# itself failed". Both canonical semantics are therefore satisfied by
# the SAME existing `TRANSPORT_ERROR`/`FAIL_RUNTIME` pairing; see the
# module docstring's "Failure classification" cross-reference.


def test_runtime_tool_failure_semantic_is_never_capability_failure():
    planner = FakePlanner([_transport_error()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="A",
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.FAIL_RUNTIME
    assert result.outcome != QualificationOutcome.FAIL_CAPABILITY


def test_infrastructure_failure_semantic_is_never_capability_failure():
    planner = FakePlanner([_transport_error()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="A",
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.FAIL_RUNTIME
    assert result.outcome != QualificationOutcome.FAIL_CAPABILITY
    # never silently retried as if it were the model's own mistake --
    # zero feedback, zero retry attempt.
    assert result.feedback == ()
    assert result.attempt_count == 1


# --- additional structural/robustness coverage -------------------------------


def test_feedback_generation_is_pure_and_requires_no_second_model():
    """`_build_feedback` never calls `.plan(`/`.infer(` and never
    constructs an adapter -- it is a pure function over an already-
    computed `PlannerTrial`, never a second model invocation."""
    import inspect

    from code_slayer.planning.qualification import _build_feedback

    source = inspect.getsource(_build_feedback)
    assert ".plan(" not in source
    assert ".infer(" not in source
    assert "adapter" not in source.lower()


def test_run_corrected_planner_case_validates_its_own_arguments():
    with pytest.raises(ValueError):
        run_corrected_planner_case(
            FakePlanner([]),
            _REQUEST,
            qualification_class="A",
            repetitions=0,
        )
    with pytest.raises(ValueError):
        run_planner_case_with_correction(
            FakePlanner([]),
            _REQUEST,
            qualification_class="A",
            max_correction_attempts=-1,
        )
    with pytest.raises(ValueError):
        run_corrected_planner_case(
            FakePlanner([_structured()]),
            _REQUEST,
            qualification_class="A",
            repetitions=1,
            early_stop_after_consecutive_transport_failures=0,
        )


def test_uncorrectable_outcomes_never_generate_feedback_or_retry():
    """TRANSPORT_TIMEOUT/TRANSPORT_ERROR/VALID_STRUCTURED_PLAN are never
    in `_CORRECTABLE_OUTCOMES` -- proven directly against the module's
    own set, not re-derived here."""
    from code_slayer.planning.qualification import _CORRECTABLE_OUTCOMES

    assert TrialOutcome.TRANSPORT_TIMEOUT not in _CORRECTABLE_OUTCOMES
    assert TrialOutcome.TRANSPORT_ERROR not in _CORRECTABLE_OUTCOMES
    assert TrialOutcome.VALID_STRUCTURED_PLAN not in _CORRECTABLE_OUTCOMES
    assert TrialOutcome.NON_TOOL_RESPONSE in _CORRECTABLE_OUTCOMES
    assert TrialOutcome.TOOL_SCHEMA_INVALID in _CORRECTABLE_OUTCOMES
    assert TrialOutcome.PLAN_VALIDATION_REJECTED in _CORRECTABLE_OUTCOMES


# =============================================================================
# Context-adequacy preflight, INVALID_ENVIRONMENT, task-relevance, and
# provenance (independent-review remediation)
# =============================================================================

_GENEROUS_PROFILE = RuntimeContextProfile(
    model_tag="test-model",
    effective_context_tokens=1_000_000,
)
# Sized so _REQUEST's attempt-1 (no feedback, ~962 estimated tokens) fits,
# but attempt-2 (with real NON_TOOL_RESPONSE feedback attached, ~1112
# estimated tokens) does not -- see the token measurements used to derive
# this constant.
_MARGINAL_PROFILE = RuntimeContextProfile(model_tag="test-model", effective_context_tokens=6150)
_TINY_PROFILE = RuntimeContextProfile(model_tag="test-model", effective_context_tokens=10)


# --- A. Input ryms -> attempt får köras --------------------------------------


def test_preflight_a_request_that_fits_lets_the_attempt_run():
    planner = FakePlanner([_structured()])
    trial = run_planner_trial(planner, _REQUEST, context_profile=_GENEROUS_PROFILE)
    assert trial.outcome != TrialOutcome.INVALID_ENVIRONMENT
    assert trial.outcome == TrialOutcome.VALID_STRUCTURED_PLAN
    assert len(planner.calls) == 1  # the model was genuinely called


# --- B. Input+budget+margin överskrider context -> INVALID_ENVIRONMENT,
#        modellen anropas INTE -----------------------------------------------


def test_preflight_b_oversized_request_never_calls_the_model():
    planner = FakePlanner([])  # any call at all raises FakePlannerError
    trial = run_planner_trial(planner, _REQUEST, context_profile=_TINY_PROFILE)
    assert trial.outcome == TrialOutcome.INVALID_ENVIRONMENT
    assert len(planner.calls) == 0  # proves no network call was ever attempted
    assert trial.latency_seconds == 0.0


def test_preflight_b_run_planner_case_with_correction_reports_invalid_environment():
    planner = FakePlanner([])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=_TINY_PROFILE,
    )
    assert result.outcome == QualificationOutcome.INVALID_ENVIRONMENT
    assert result.outcome != QualificationOutcome.FAIL_CAPABILITY
    assert result.outcome != QualificationOutcome.FAIL_RUNTIME
    assert result.outcome != QualificationOutcome.FAIL_TRANSPORT_TIMEOUT
    assert len(planner.calls) == 0


# --- C. En retry som växer över context -> INVALID_ENVIRONMENT, inte
#        capability fail ------------------------------------------------------


def test_preflight_c_retry_growth_over_context_is_invalid_environment_not_capability():
    """A merely marginal ESTIMATED overflow never pre-blocks (see the
    module docstring) -- so attempt 2 for a growing retry genuinely
    reaches the model here. What proves the environment invalid is the
    real call's own reported `usage.prompt_tokens` coming back at or
    above the effective context -- exactly what a real runtime would
    report once truncation occurs -- overriding the outcome to
    INVALID_ENVIRONMENT post-call, never FAIL_CAPABILITY."""
    truncated_response = replace(
        _structured(),
        usage=WorkerUsage(prompt_tokens=6150, completion_tokens=20, total_tokens=6170),
    )
    planner = FakePlanner([_non_tool_response(), truncated_response])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=_MARGINAL_PROFILE,
    )
    assert result.outcome == QualificationOutcome.INVALID_ENVIRONMENT
    assert result.outcome != QualificationOutcome.FAIL_CAPABILITY
    assert result.attempt_count == 2
    assert result.attempts[0].outcome == TrialOutcome.NON_TOOL_RESPONSE
    assert result.attempts[1].outcome == TrialOutcome.INVALID_ENVIRONMENT
    # Both attempts genuinely reached the model this time (post-call
    # verification, not a pre-call block).
    assert len(planner.calls) == 2


def test_preflight_c_bare_first_attempt_truncation_is_also_caught_post_call():
    """The same post-call override applies on attempt 1, not only on a
    growing retry."""
    truncated_response = replace(
        _structured(),
        usage=WorkerUsage(prompt_tokens=6150, completion_tokens=5, total_tokens=6155),
    )
    planner = FakePlanner([truncated_response])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=_MARGINAL_PROFILE,
    )
    assert result.outcome == QualificationOutcome.INVALID_ENVIRONMENT
    assert result.attempt_count == 1


# --- D/E/F: A2 keeps original task, repository context, and carries
#            deterministic feedback -------------------------------------------


def test_preflight_d_a2_retains_original_task():
    planner = FakePlanner([_non_tool_response(), _structured()])
    run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        unsafe_allow_unverified_environment=True,
    )
    assert planner.calls[0].original_request == _REQUEST.original_request
    assert planner.calls[1].original_request == _REQUEST.original_request


def test_preflight_e_a2_retains_repository_context(git_repo_with_commit):
    snapshot = _snapshot(git_repo_with_commit)
    request = PlannerRequest(
        original_request="Add the requested read-only endpoint.",
        repo_context=snapshot.projects,
        discovered_commands=snapshot.commands,
    )
    planner = FakePlanner([_non_tool_response(), _structured()])
    run_planner_case_with_correction(
        planner,
        request,
        qualification_class="C",
        snapshot=snapshot,
        unsafe_allow_unverified_environment=True,
    )
    assert planner.calls[0].repo_context == request.repo_context
    assert planner.calls[1].repo_context == request.repo_context
    assert planner.calls[0].discovered_commands == request.discovered_commands
    assert planner.calls[1].discovered_commands == request.discovered_commands


def test_preflight_f_a2_carries_deterministic_feedback():
    planner = FakePlanner([_non_tool_response(), _structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        unsafe_allow_unverified_environment=True,
    )
    assert planner.calls[0].prior_attempt_feedback is None
    assert planner.calls[1].prior_attempt_feedback == result.feedback[0].render()
    assert "SAME qualification task" in planner.calls[1].prior_attempt_feedback


# --- G. First-pass and corrected-pass held separate (see also the
#        pre-existing correction_a/b/c tests above) ---------------------------


def test_preflight_g_invalid_environment_is_never_conflated_with_either_pass_kind():
    planner = FakePlanner([])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="A",
        context_profile=_TINY_PROFILE,
    )
    assert result.outcome not in (
        QualificationOutcome.PASS_FIRST_TRY,
        QualificationOutcome.PASS_AFTER_FEEDBACK,
    )


# --- H. Historical raw failure kan samexistera med INVALID_ENVIRONMENT
#        qualification validity -----------------------------------------------


def test_preflight_h_historical_trial_coexists_with_a_later_environment_interpretation():
    """A raw historical `PlannerTrial` (e.g. already durably recorded
    before this revision) is never mutated to add environment-validity
    information after the fact -- a separate, independent `preflight_
    check()` call against the same request/profile expresses that
    interpretation instead, without touching the original record."""
    historical_trial = PlannerTrial(
        outcome=TrialOutcome.NON_TOOL_RESPONSE,
        latency_seconds=12.3,
        detail="invalid_transport_response:text_response",
    )
    # The original historical record is untouched...
    assert historical_trial.outcome == TrialOutcome.NON_TOOL_RESPONSE
    # ...while a separate, independent interpretation can still say the
    # environment that produced it was invalid.
    preflight = preflight_check(_REQUEST, _TINY_PROFILE)
    assert preflight.fits is False
    # The historical object is a frozen dataclass -- structurally, nothing
    # here could have mutated it even if it tried.
    from dataclasses import FrozenInstanceError

    with pytest.raises(FrozenInstanceError):
        historical_trial.outcome = TrialOutcome.VALID_STRUCTURED_PLAN


# --- I. Irrelevant men schema-valid plan får inte automatiskt bli PASS -------


def test_preflight_i_irrelevant_goal_is_not_promoted_to_a_pass():
    irrelevant = _structured(goal="Refactor the unrelated billing subsystem entirely")
    outcome, detail, _hint = classify_planner_response(
        irrelevant,
        original_request="Add a read-only endpoint for planning job counts.",
    )
    assert outcome == TrialOutcome.TASK_NOT_RELEVANT
    assert outcome != TrialOutcome.VALID_STRUCTURED_PLAN
    assert detail is not None


def test_preflight_i_relevant_goal_still_passes():
    relevant = _structured(goal="Add a read-only endpoint that returns job counts")
    outcome, _detail, _hint = classify_planner_response(
        relevant,
        original_request="Add a read-only endpoint for planning job counts.",
    )
    assert outcome == TrialOutcome.VALID_STRUCTURED_PLAN


def test_preflight_i_task_not_relevant_is_retryable_and_generates_feedback():
    planner = FakePlanner(
        [
            _structured(goal="Refactor the unrelated billing subsystem entirely"),
            _structured(goal="Add the requested read-only endpoint"),
        ]
    )
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        unsafe_allow_unverified_environment=True,
    )
    assert result.attempts[0].outcome == TrialOutcome.TASK_NOT_RELEVANT
    assert result.outcome == QualificationOutcome.PASS_AFTER_FEEDBACK
    assert "ORIGINAL_REQUEST" in result.feedback[0].render()


# --- J. Påhittad blockerande evidence får inte ge qualification PASS --------


def test_preflight_j_fabricated_blocking_evidence_never_becomes_a_pass(git_repo_with_commit):
    snapshot = _snapshot(git_repo_with_commit)
    fabricated = _structured(
        affected_files=(
            PlannerAffectedFileProposal(
                path="totally_fabricated_path.py", action="modify", reason="x"
            ),
        )
    )
    outcome, _detail, _hint = classify_planner_response(fabricated, snapshot)
    assert outcome == TrialOutcome.PLAN_VALIDATION_REJECTED
    assert outcome != TrialOutcome.VALID_STRUCTURED_PLAN


# --- K. Provenance/fingerprint ändras om task/context/runtime-context ändras -


def test_preflight_k_provenance_fingerprint_changes_with_task():
    trial = PlannerTrial(outcome=TrialOutcome.VALID_STRUCTURED_PLAN, latency_seconds=1.0)
    prov_a = build_attempt_provenance(
        qualification_class="B",
        request=_REQUEST,
        profile=_GENEROUS_PROFILE,
        attempt_number=1,
        trial=trial,
    )
    other_request = PlannerRequest(original_request="Add a completely different endpoint.")
    prov_b = build_attempt_provenance(
        qualification_class="B",
        request=other_request,
        profile=_GENEROUS_PROFILE,
        attempt_number=1,
        trial=trial,
    )
    assert prov_a.task_fingerprint != prov_b.task_fingerprint
    assert prov_a.request_fingerprint != prov_b.request_fingerprint


def test_preflight_k_provenance_fingerprint_changes_with_runtime_context():
    trial = PlannerTrial(outcome=TrialOutcome.VALID_STRUCTURED_PLAN, latency_seconds=1.0)
    prov_a = build_attempt_provenance(
        qualification_class="B",
        request=_REQUEST,
        profile=_GENEROUS_PROFILE,
        attempt_number=1,
        trial=trial,
    )
    other_profile = RuntimeContextProfile(model_tag="other-tag", effective_context_tokens=8192)
    prov_b = build_attempt_provenance(
        qualification_class="B",
        request=_REQUEST,
        profile=other_profile,
        attempt_number=1,
        trial=trial,
    )
    assert prov_a.model_tag != prov_b.model_tag
    assert prov_a.effective_context_tokens != prov_b.effective_context_tokens


def test_preflight_k_provenance_never_stores_raw_repository_text(git_repo_with_commit):
    snapshot = _snapshot(git_repo_with_commit)
    request = PlannerRequest(
        original_request="Add a read-only endpoint.",
        repo_context=snapshot.projects,
    )
    trial = PlannerTrial(outcome=TrialOutcome.VALID_STRUCTURED_PLAN, latency_seconds=1.0)
    prov = build_attempt_provenance(
        qualification_class="C",
        request=request,
        profile=_GENEROUS_PROFILE,
        attempt_number=1,
        trial=trial,
    )
    assert isinstance(prov, AttemptProvenance)
    for value in vars(prov).values():
        if isinstance(value, str):
            # Every stored string is a bounded fingerprint/tag, never raw text.
            assert len(value) < 200


def test_preflight_k_correction_result_carries_provenance_when_profile_supplied():
    planner = FakePlanner([_structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        context_profile=_GENEROUS_PROFILE,
    )
    assert len(result.provenance) == 1
    assert result.provenance[0].attempt_number == 1
    assert result.provenance[0].environment_valid is True


def test_preflight_k_correction_result_has_no_provenance_without_a_profile():
    planner = FakePlanner([_structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        unsafe_allow_unverified_environment=True,
    )
    assert result.provenance == ()


# --- L. Inga modell-specifika specialfall -------------------------------------


def test_preflight_l_no_model_specific_special_cases_in_the_module_source():
    """The module *docstring* legitimately cites the real qwen3-coder
    finding that motivated this fix as historical evidence/rationale --
    that is documentation, not a special case. What must never exist is
    a model name inside actual executable code (a conditional branch, a
    literal comparison, a hardcoded default) -- so this checks only the
    source *after* the module docstring."""
    import ast

    import code_slayer.planning.qualification as qualification_module

    source = open(qualification_module.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    docstring_node = tree.body[0]
    assert isinstance(docstring_node, ast.Expr) and isinstance(docstring_node.value, ast.Constant)
    code_only = "\n".join(source.splitlines()[docstring_node.end_lineno :]).lower()
    for forbidden in ("qwen", "devstral", "gemma", "ollama"):
        assert forbidden not in code_only, (
            f"qualification module's executable code references a specific model: {forbidden}"
        )


# --- estimate_request_tokens / preflight_check basic contracts --------------


def test_estimate_request_tokens_grows_with_feedback():
    without_feedback = estimate_request_tokens(_REQUEST)
    with_feedback = estimate_request_tokens(
        _REQUEST.__class__(**{**vars(_REQUEST), "prior_attempt_feedback": "x" * 300}),
    )
    assert with_feedback > without_feedback


def test_runtime_context_profile_validates_its_own_fields():
    with pytest.raises(ValueError):
        RuntimeContextProfile(model_tag="", effective_context_tokens=100)
    with pytest.raises(ValueError):
        RuntimeContextProfile(model_tag="m", effective_context_tokens=-1)
    with pytest.raises(ValueError, match="both be set"):
        RuntimeContextProfile(
            model_tag="m",
            effective_context_tokens=100,
            normalizer_id="x",
        )
    with pytest.raises(ValueError, match="temperature"):
        RuntimeContextProfile(model_tag="m", effective_context_tokens=100, temperature=True)
    with pytest.raises(ValueError, match="temperature"):
        RuntimeContextProfile(model_tag="m", effective_context_tokens=100, temperature=2.5)
    coerced = RuntimeContextProfile(model_tag="m", effective_context_tokens=100, temperature=0)
    assert coerced.temperature == 0.0
    assert isinstance(coerced.temperature, float)


def test_attempt_provenance_records_native_vs_normalized_identity():
    """Native-only and compatibility-normalizer runtimes remain
    distinguishable in qualification evidence — certificates bind to
    this identity, so it must never be omitted or inferred."""
    trial = PlannerTrial(outcome=TrialOutcome.VALID_STRUCTURED_PLAN, latency_seconds=1.0)
    native = build_attempt_provenance(
        qualification_class="C",
        request=_REQUEST,
        profile=_GENEROUS_PROFILE,
        attempt_number=1,
        trial=trial,
        response=_structured(),
    )
    assert native.normalizer_id is None
    assert native.normalizer_version is None
    assert native.tool_call_transport is None

    normalized_profile = RuntimeContextProfile(
        model_tag=_GENEROUS_PROFILE.model_tag,
        effective_context_tokens=_GENEROUS_PROFILE.effective_context_tokens,
        output_token_budget=_GENEROUS_PROFILE.output_token_budget,
        safety_margin_tokens=_GENEROUS_PROFILE.safety_margin_tokens,
        model_digest=_GENEROUS_PROFILE.model_digest,
        endpoint=_GENEROUS_PROFILE.endpoint,
        runtime_version=_GENEROUS_PROFILE.runtime_version,
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
    )
    normalized_response = PlannerResponse(
        PlannerOutcome.STRUCTURED,
        output=PlannerStructuredOutput(goal="Add the requested read-only endpoint"),
        raw="{}",
        tool_call_transport=ToolCallTransport.NORMALIZED,
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
    )
    normalized = build_attempt_provenance(
        qualification_class="C",
        request=_REQUEST,
        profile=normalized_profile,
        attempt_number=1,
        trial=trial,
        response=normalized_response,
    )
    assert normalized.normalizer_id == "qwen_textual_tool_v1"
    assert normalized.normalizer_version == 1
    assert normalized.tool_call_transport == ToolCallTransport.NORMALIZED.value
    assert (native.normalizer_id, native.normalizer_version) != (
        normalized.normalizer_id,
        normalized.normalizer_version,
    )
    assert native.runtime_config_fingerprint is None
    assert normalized.runtime_config_fingerprint is None
    assert native.temperature is None


def test_attempt_provenance_records_runtime_config_fingerprint_when_temperature_set():
    trial = PlannerTrial(outcome=TrialOutcome.VALID_STRUCTURED_PLAN, latency_seconds=1.0)
    profile = RuntimeContextProfile(
        model_tag="qwen3-coder-ctx16k:30b",
        effective_context_tokens=16384,
        output_token_budget=4096,
        model_digest="sha256:abc",
        endpoint="http://192.168.32.8:11434/v1",
        runtime_version="0.16.1",
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
        temperature=0.0,
    )
    prov = build_attempt_provenance(
        qualification_class="C",
        request=_REQUEST,
        profile=profile,
        attempt_number=1,
        trial=trial,
        response=_structured(),
    )
    assert prov.temperature == 0.0
    assert prov.runtime_config_fingerprint == profile.runtime_config_fingerprint()
    assert prov.runtime_config_fingerprint is not None
    hotter = RuntimeContextProfile(
        model_tag=profile.model_tag,
        effective_context_tokens=profile.effective_context_tokens,
        output_token_budget=profile.output_token_budget,
        model_digest=profile.model_digest,
        endpoint=profile.endpoint,
        runtime_version=profile.runtime_version,
        normalizer_id=profile.normalizer_id,
        normalizer_version=profile.normalizer_version,
        temperature=1.5,
    )
    hot_prov = build_attempt_provenance(
        qualification_class="C",
        request=_REQUEST,
        profile=hotter,
        attempt_number=1,
        trial=trial,
        response=_structured(),
    )
    assert hot_prov.runtime_config_fingerprint != prov.runtime_config_fingerprint
    assert hot_prov.temperature == 1.5


def test_aggregate_planner_trials_excludes_invalid_environment_from_rates():
    planner_ok = FakePlanner([_structured()])
    ok_trial = run_planner_trial(planner_ok, _REQUEST, context_profile=_GENEROUS_PROFILE)
    invalid_trial = run_planner_trial(
        FakePlanner([]),
        _REQUEST,
        context_profile=_TINY_PROFILE,
    )
    metrics = aggregate_planner_trials("C", "candidate", (ok_trial, invalid_trial))
    assert metrics.repetitions == 2
    assert metrics.non_assessable_count == 1
    assert metrics.assessable_repetitions == 1
    # The one valid-environment trial passed -- rates reflect only it,
    # never diluted by the environment-invalid one.
    assert metrics.fully_valid_rate == pytest.approx(1.0)
    assert metrics.genuine_tool_call_rate == pytest.approx(1.0)


# =============================================================================
# Second hardening round: exact vs. estimated vs. unknown token measurement,
# verified-profile-required scoring, and real output-budget enforcement
# =============================================================================


class _FakeExactCounter:
    """A test-local `TokenCounter` reporting a caller-fixed EXACT count,
    regardless of what the actual request renders to -- proves the
    *mechanism* (an injected exact measurement overriding a hopeless
    estimate), not any particular real-world number."""

    def __init__(self, token_count: int) -> None:
        self._token_count = token_count

    def measure(self, request: PlannerRequest) -> TokenMeasurement:
        return TokenMeasurement(
            source=TokenMeasurementSource.VERIFIED_FULL_INPUT,
            token_count=self._token_count,
            request_fingerprint="fake",
            method="test_double:fixed_exact_count",
        )


# --- A. Exact token count under context -> attempt runs ---------------------


def test_measurement_a_exact_count_under_context_lets_the_attempt_run():
    # Sized so a small EXACT count (below) fits, while _REQUEST's own
    # ESTIMATE would not -- this exercises the exact-count path
    # explicitly via injection, not merely a profile generous enough to
    # fit the estimate on its own.
    profile = RuntimeContextProfile(
        model_tag="t",
        effective_context_tokens=200,
        output_token_budget=0,
        safety_margin_tokens=100,
    )
    counter = _FakeExactCounter(token_count=10)
    result = preflight_check(_REQUEST, profile, exact_counter=counter)
    assert result.fits is True
    assert result.measurement.source == TokenMeasurementSource.VERIFIED_FULL_INPUT
    planner = FakePlanner([_structured()])
    trial = run_planner_trial(planner, _REQUEST, context_profile=profile, exact_counter=counter)
    assert trial.outcome != TrialOutcome.INVALID_ENVIRONMENT
    assert len(planner.calls) == 1


# --- B. Exact token count över context -> INVALID_ENVIRONMENT --------------


def test_measurement_b_exact_count_over_context_is_invalid_environment():
    profile = RuntimeContextProfile(
        model_tag="t",
        effective_context_tokens=100,
        output_token_budget=0,
        safety_margin_tokens=0,
    )
    counter = _FakeExactCounter(token_count=500)  # far above the effective context
    planner = FakePlanner([])
    trial = run_planner_trial(planner, _REQUEST, context_profile=profile, exact_counter=counter)
    assert trial.outcome == TrialOutcome.INVALID_ENVIRONMENT
    assert len(planner.calls) == 0


# --- C. Conservative estimate över context men exact measurement visar att
#        den ryms -> FÅR INTE bli INVALID_ENVIRONMENT -----------------------


def test_measurement_c_hopeless_estimate_overridden_by_a_fitting_exact_count():
    """This is the exact bug this round fixes: a heuristic estimate that
    (like the real ~34% overestimate observed on Class C) looks hopeless
    against a small profile must never alone prove the environment
    invalid when an exact measurement shows the real request fits."""
    profile = RuntimeContextProfile(
        model_tag="t",
        effective_context_tokens=100,
        output_token_budget=0,
        safety_margin_tokens=0,
    )
    # Without any exact counter, _REQUEST's estimate (~962) against this
    # tiny profile is hopeless (962 > 100*2) and blocks pre-call.
    blocked = preflight_check(_REQUEST, profile)
    assert blocked.fits is False
    assert blocked.measurement.source == TokenMeasurementSource.ESTIMATED

    # With an exact counter reporting a real, small, fitting count, the
    # same profile and request now fits.
    counter = _FakeExactCounter(token_count=50)
    allowed = preflight_check(_REQUEST, profile, exact_counter=counter)
    assert allowed.fits is True
    assert allowed.measurement.source == TokenMeasurementSource.VERIFIED_FULL_INPUT

    planner = FakePlanner([_structured()])
    trial = run_planner_trial(planner, _REQUEST, context_profile=profile, exact_counter=counter)
    assert trial.outcome != TrialOutcome.INVALID_ENVIRONMENT
    assert len(planner.calls) == 1


def test_measurement_c_marginal_estimate_overflow_proceeds_without_an_exact_counter():
    """A merely marginal (non-hopeless) ESTIMATED overflow, with no exact
    counter available, must not be pre-call-blocked either -- it proceeds
    to the real call, relying on post-call verification instead (see the
    module docstring)."""
    result = preflight_check(_REQUEST, _MARGINAL_PROFILE)
    assert result.fits is True
    assert result.measurement.source == TokenMeasurementSource.ESTIMATED


# --- D. Scorebar qualification utan verifierad effective context ->
#        fail closed / ENVIRONMENT_UNVERIFIED --------------------------------


def test_verified_profile_d_no_profile_is_environment_unverified_never_a_pass():
    planner = FakePlanner([_structured()])  # would otherwise pass instantly
    result = run_planner_case_with_correction(planner, _REQUEST, qualification_class="B")
    assert result.outcome == QualificationOutcome.ENVIRONMENT_UNVERIFIED
    assert result.attempt_count == 0
    assert len(planner.calls) == 0  # the model was never called at all
    assert result.outcome not in (
        QualificationOutcome.PASS_FIRST_TRY,
        QualificationOutcome.PASS_AFTER_FEEDBACK,
        QualificationOutcome.FAIL_CAPABILITY,
    )


def test_verified_profile_d_run_corrected_planner_case_is_cheap_when_unverified():
    planner = FakePlanner([])  # any call at all would raise
    results, early_stopped = run_corrected_planner_case(
        planner,
        _REQUEST,
        qualification_class="B",
        repetitions=10,
    )
    assert len(results) == 10
    assert all(r.outcome == QualificationOutcome.ENVIRONMENT_UNVERIFIED for r in results)
    assert early_stopped is False
    assert len(planner.calls) == 0


def test_verified_profile_d_unsafe_escape_hatch_must_be_explicit():
    planner = FakePlanner([_structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        unsafe_allow_unverified_environment=True,
    )
    assert result.outcome == QualificationOutcome.PASS_FIRST_TRY  # opt-in restores old behavior


# --- E/F. Output budget enforcement is real when the profile says so -------


def test_output_budget_e_enforced_profile_threads_max_output_tokens_to_the_request():
    profile = RuntimeContextProfile(
        model_tag="t",
        effective_context_tokens=1_000_000,
        output_token_budget=777,
        output_budget_enforcement_verified=True,
    )
    planner = FakePlanner([_structured()])
    run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        context_profile=profile,
    )
    assert planner.calls[0].output_token_budget == 777


def test_output_budget_f_unenforced_profile_never_sends_a_budget_and_says_so():
    planner = FakePlanner([_structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        context_profile=_GENEROUS_PROFILE,
    )
    assert planner.calls[0].output_token_budget is None
    assert result.provenance[0].output_budget_enforced is False


def test_output_budget_f_enforced_profile_records_that_in_provenance():
    profile = RuntimeContextProfile(
        model_tag="t",
        effective_context_tokens=1_000_000,
        output_budget_enforcement_verified=True,
    )
    planner = FakePlanner([_structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        context_profile=profile,
    )
    assert result.provenance[0].output_budget_enforced is True


# --- G. Retry får ny tokenmätning eftersom feedback förändrar requeststorleken


def test_retry_g_gets_a_fresh_token_measurement_reflecting_feedback_growth():
    planner = FakePlanner([_non_tool_response(), _structured()])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        context_profile=_GENEROUS_PROFILE,
    )
    assert len(result.provenance) == 2
    assert result.provenance[1].measured_input_tokens > result.provenance[0].measured_input_tokens


# --- H. Request-hash ändras -> gammal exact measurement får inte återanvändas -


def test_measurement_h_request_fingerprint_changes_so_a_stale_measurement_is_never_reused():
    measurement_a = estimate_request_token_measurement(_REQUEST)
    other = PlannerRequest(original_request="A completely different task entirely.")
    measurement_b = estimate_request_token_measurement(other)
    assert measurement_a.request_fingerprint != measurement_b.request_fingerprint


# --- I. Token measurement provenance innehåller källa/metod -----------------


def test_measurement_i_provenance_records_source_and_method():
    trial = PlannerTrial(outcome=TrialOutcome.VALID_STRUCTURED_PLAN, latency_seconds=1.0)
    prov = build_attempt_provenance(
        qualification_class="B",
        request=_REQUEST,
        profile=_GENEROUS_PROFILE,
        attempt_number=1,
        trial=trial,
    )
    assert prov.token_measurement_source == TokenMeasurementSource.ESTIMATED.value
    assert "char_heuristic" in prov.token_measurement_method


# --- J. Inga qwen-/Class-C-specialfall (extends the existing generic check) -


def test_measurement_j_no_class_c_specific_identifiers_in_executable_code():
    import ast

    import code_slayer.planning.qualification as qualification_module

    source = open(qualification_module.__file__, encoding="utf-8").read()
    tree = ast.parse(source)
    docstring_node = tree.body[0]
    code_only = "\n".join(source.splitlines()[docstring_node.end_lineno :]).lower()
    for forbidden in ("class_c", "planning-jobs", "planning_jobs_count"):
        assert forbidden not in code_only, (
            f"qualification module's executable code hardcodes a specific task/class: {forbidden}"
        )


# =============================================================================
# Third hardening round: expected-vs-actual input tokens, fingerprint-bound
# verification, and output-budget-exhaustion classification
# (independent-review remediation)
# =============================================================================


def _with_usage(response: PlannerResponse, *, prompt_tokens: int, completion_tokens: int = 20):
    return replace(
        response,
        usage=WorkerUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


# --- A. expected=9724, actual=9724 -> input preservation verified -----------


def test_expected_actual_a_matching_actual_verifies_full_input_preservation():
    fp = preflight_check(_REQUEST, _GENEROUS_PROFILE).measurement.request_fingerprint
    expected = VerifiedExpectedInput(
        request_fingerprint=fp,
        expected_tokens=9724,
        method="cross_context_size_comparison",
    )
    result = verify_full_input_preservation(_REQUEST, 9724, expected)
    assert result.verified is True
    assert result.measurement.source == TokenMeasurementSource.VERIFIED_FULL_INPUT

    response = _with_usage(_structured(), prompt_tokens=9724)
    planner = FakePlanner([response])
    trial = run_planner_trial(
        planner,
        _REQUEST,
        context_profile=_GENEROUS_PROFILE,
        verified_expected_input=expected,
    )
    assert trial.outcome == TrialOutcome.VALID_STRUCTURED_PLAN


# --- B. expected=9724, actual=4096 -> INVALID_ENVIRONMENT/input_truncated ---


def test_expected_actual_b_actual_below_expected_is_input_truncated():
    fp = preflight_check(_REQUEST, _GENEROUS_PROFILE).measurement.request_fingerprint
    expected = VerifiedExpectedInput(
        request_fingerprint=fp,
        expected_tokens=9724,
        method="cross_context_size_comparison",
    )
    result = verify_full_input_preservation(_REQUEST, 4096, expected)
    assert result.verified is False
    assert result.reason == "input_truncated"

    response = _with_usage(_structured(), prompt_tokens=4096)
    planner = FakePlanner([response])
    trial = run_planner_trial(
        planner,
        _REQUEST,
        context_profile=_GENEROUS_PROFILE,
        verified_expected_input=expected,
    )
    assert trial.outcome == TrialOutcome.INVALID_ENVIRONMENT
    assert "input_truncated" in trial.detail


# --- C. actual=3900 < ctx=4096 must NOT become VERIFIED_FULL_INPUT without
#        an explicit verified expected baseline ------------------------------


def test_expected_actual_c_actual_under_context_alone_is_never_verified():
    """This is exactly the bug this round fixes: `usage.prompt_tokens`
    landing below the effective context proves nothing about whether the
    full, untruncated 9000-token original request survived -- it might
    just as well be a 3900-token *evaluated* remainder of a runtime
    truncation. Without a verified expected baseline, this must be
    reported as ACTUAL_EVALUATED only, never VERIFIED_FULL_INPUT."""
    profile = RuntimeContextProfile(model_tag="t", effective_context_tokens=4096)
    response = _with_usage(_structured(), prompt_tokens=3900)
    planner = FakePlanner([response])
    trial = run_planner_trial(planner, _REQUEST, context_profile=profile)
    # 3900 < 4096, so the weak ceiling check does not fire either -- the
    # trial proceeds to ordinary classification, but nothing here may
    # ever claim the full (possibly 9000-token) original was preserved.
    assert trial.outcome == TrialOutcome.VALID_STRUCTURED_PLAN
    trial_prov = build_attempt_provenance(
        qualification_class="C",
        request=_REQUEST,
        profile=profile,
        attempt_number=1,
        trial=trial,
        response=response,
    )
    assert trial_prov.token_measurement_source == TokenMeasurementSource.ACTUAL_EVALUATED.value
    assert trial_prov.token_measurement_source != TokenMeasurementSource.VERIFIED_FULL_INPUT.value
    assert trial_prov.full_input_preservation_verified is False


# --- D. request fingerprint ändras -> gammal expected measurement får inte
#        återanvändas --------------------------------------------------------


def test_expected_actual_d_stale_expected_measurement_is_never_reused():
    other_request = PlannerRequest(original_request="A totally different task.")
    stale_fp = preflight_check(_REQUEST, _GENEROUS_PROFILE).measurement.request_fingerprint
    expected = VerifiedExpectedInput(
        request_fingerprint=stale_fp,
        expected_tokens=9724,
        method="cross_context_size_comparison",
    )
    result = verify_full_input_preservation(other_request, 9724, expected)
    assert result.verified is False
    assert result.reason == "request_fingerprint_mismatch_expected_measurement_stale"


# --- E. retry-feedback ändrar request -> ny measurement krävs ---------------


def test_expected_actual_e_a_retry_never_reuses_attempt_1s_verified_baseline():
    """A `VerifiedExpectedInput` established for attempt 1's pristine
    request must not silently apply to attempt 2, whose added feedback
    text changes the fingerprint -- the retry falls back to the weaker
    ceiling check instead of wrongly inheriting attempt 1's guarantee."""
    fp_attempt_1 = preflight_check(_REQUEST, _GENEROUS_PROFILE).measurement.request_fingerprint
    expected_for_attempt_1 = VerifiedExpectedInput(
        request_fingerprint=fp_attempt_1,
        expected_tokens=962,
        method="test_baseline",
    )
    profile = RuntimeContextProfile(
        model_tag="t",
        effective_context_tokens=1100,
        output_token_budget=0,
        safety_margin_tokens=0,
    )
    attempt_1_response = _with_usage(_non_tool_response(), prompt_tokens=962)
    # Attempt 2's usage happens to be reported at/over this small
    # profile's ceiling -- with no verified baseline applying to its
    # (different, feedback-grown) fingerprint, the weak ceiling check is
    # what catches it, not a wrongly-reused attempt-1 baseline.
    attempt_2_response = _with_usage(_structured(), prompt_tokens=1100)
    planner = FakePlanner([attempt_1_response, attempt_2_response])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        context_profile=profile,
        verified_expected_input=expected_for_attempt_1,
    )
    assert result.attempts[0].outcome == TrialOutcome.NON_TOOL_RESPONSE
    assert result.attempts[1].outcome == TrialOutcome.INVALID_ENVIRONMENT
    assert "no verified expected-input baseline applies" in result.attempts[1].detail


# --- F. finish_reason == "length" -> OUTPUT_BUDGET_EXHAUSTED, never a
#        schema/capability fail ----------------------------------------------


def test_output_truncation_f_finish_reason_length_is_output_budget_exhausted():
    truncated = replace(_structured(), finish_reason="length")
    outcome, detail, _hint = classify_planner_response(truncated)
    assert outcome == TrialOutcome.OUTPUT_BUDGET_EXHAUSTED
    assert outcome not in (
        TrialOutcome.TOOL_SCHEMA_INVALID,
        TrialOutcome.NON_TOOL_RESPONSE,
        TrialOutcome.PLAN_VALIDATION_REJECTED,
    )
    assert "length" in detail


def test_output_truncation_f_takes_priority_over_evidence_rejection(git_repo_with_commit):
    snapshot = _snapshot(git_repo_with_commit)
    truncated_and_would_have_been_rejected = replace(
        _structured(
            affected_files=(
                PlannerAffectedFileProposal(path="does_not_exist.py", action="modify", reason="x"),
            )
        ),
        finish_reason="length",
    )
    outcome, _detail, _hint = classify_planner_response(
        truncated_and_would_have_been_rejected,
        snapshot,
    )
    assert outcome == TrialOutcome.OUTPUT_BUDGET_EXHAUSTED


def test_output_truncation_f_case_level_outcome_is_output_budget_exhausted_never_capability():
    planner = FakePlanner([replace(_non_tool_response(), finish_reason="length")])
    result = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="B",
        context_profile=_GENEROUS_PROFILE,
    )
    assert result.outcome == QualificationOutcome.OUTPUT_BUDGET_EXHAUSTED
    assert result.outcome != QualificationOutcome.FAIL_CAPABILITY
    assert result.attempt_count == 1  # never retried -- nothing to correct
    assert result.feedback == ()


# --- G. finish_reason == "stop" under cap -> normal qualification semantics -


def test_output_truncation_g_finish_reason_stop_is_unaffected():
    normal = replace(_structured(), finish_reason="stop")
    outcome, _detail, _hint = classify_planner_response(normal)
    assert outcome == TrialOutcome.VALID_STRUCTURED_PLAN


def test_output_truncation_g_no_finish_reason_at_all_is_unaffected():
    # Ordinary responses (this module's other tests) never set
    # finish_reason -- confirming that omitting it entirely behaves
    # exactly like "stop", not like "length".
    outcome, _detail, _hint = classify_planner_response(_structured())
    assert outcome == TrialOutcome.VALID_STRUCTURED_PLAN


# --- H. completion_tokens binds in provenance --------------------------------


def test_output_truncation_h_completion_tokens_and_finish_reason_bind_in_provenance():
    response = _with_usage(
        replace(_structured(), finish_reason="length"),
        prompt_tokens=500,
        completion_tokens=4096,
    )
    trial = PlannerTrial(outcome=TrialOutcome.OUTPUT_BUDGET_EXHAUSTED, latency_seconds=1.0)
    prov = build_attempt_provenance(
        qualification_class="B",
        request=_REQUEST,
        profile=_GENEROUS_PROFILE,
        attempt_number=1,
        trial=trial,
        response=response,
    )
    assert prov.completion_tokens == 4096
    assert prov.finish_reason == "length"


def test_output_truncation_h_requested_max_tokens_binds_in_provenance():
    profile = RuntimeContextProfile(
        model_tag="t",
        effective_context_tokens=1_000_000,
        output_token_budget=777,
        output_budget_enforcement_verified=True,
    )
    request_with_budget = replace(_REQUEST, output_token_budget=777)
    trial = PlannerTrial(outcome=TrialOutcome.VALID_STRUCTURED_PLAN, latency_seconds=1.0)
    prov = build_attempt_provenance(
        qualification_class="B",
        request=request_with_budget,
        profile=profile,
        attempt_number=1,
        trial=trial,
        response=_structured(),
    )
    assert prov.requested_max_tokens == 777


# --- I. Inga qwen-/Class-C-specialregler (reuses the existing generic checks)


def test_expected_actual_i_no_model_specific_logic_in_verification_functions():
    import inspect

    source = inspect.getsource(verify_full_input_preservation)
    source += inspect.getsource(actual_evaluated_measurement_from_usage)
    for forbidden in ("qwen", "devstral", "class_c", "9724", "4096"):
        assert forbidden not in source.lower(), (
            f"verification logic hardcodes a specific model/value: {forbidden}"
        )
