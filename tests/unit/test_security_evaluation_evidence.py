"""Durable Baseline Security evaluation evidence: round-trip, hash,
tamper detection, suite/spec/runtime binding. Never issues a certificate.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from code_slayer.audit.canonical import canonical_json
from code_slayer.security.evaluation import (
    EVALUATION_SUITE_VERSION,
    mandatory_case_ids,
    run_baseline_security_evaluation,
)
from code_slayer.security.evidence import (
    EVIDENCE_KIND,
    EVIDENCE_SPEC_VERSION,
    SecurityEvaluationEvidenceError,
    build_baseline_security_evidence_document,
    persist_baseline_security_evidence,
    read_baseline_security_evidence,
)
from code_slayer.store.content_store import ContentStore
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind, WorkerToolCall
from code_slayer.workers.security_baseline import (
    BASELINE_VERSION,
    fingerprint_runtime_identity,
    runtime_profile_identity_from_config,
)


@pytest.fixture
def registered_worker(db_conn) -> str:
    WorkersRepo(db_conn).register(worker_id="w1", kind="fake", network_class="local")
    return "w1"


@pytest.fixture
def blobs_dir(tmp_path) -> Path:
    directory = tmp_path / "blobs"
    directory.mkdir()
    return directory


def _profile(**overrides):
    kwargs = dict(
        model_tag="qwen3-coder-ctx16k:30b",
        model_digest="sha256:abc",
        endpoint="http://192.168.32.8:11434/v1",
        runtime_version="0.16.1",
        effective_context_tokens=16384,
        temperature=0.0,
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
    )
    kwargs.update(overrides)
    return runtime_profile_identity_from_config(**kwargs)


def _text() -> WorkerResponse:
    return WorkerResponse(kind=WorkerResponseKind.TEXT, text="refused")


def _run_pass(db_conn, registered_worker, blobs_dir, profile=None):
    adapter = FakeWorkerAdapter([_text() for _ in mandatory_case_ids()])
    return run_baseline_security_evaluation(
        db_conn,
        worker_id=registered_worker,
        adapter=adapter,
        runtime_profile=profile if profile is not None else _profile(),
        blobs_dir=blobs_dir,
    )


def _put_document(db_conn, blobs_dir, document: dict) -> str:
    payload = canonical_json(document).encode("utf-8")
    blob = ContentStore(db_conn, blobs_dir).put(
        payload,
        media_type="application/json",
        source_kind=EVIDENCE_KIND,
        exportable=False,
    )
    return blob.content_hash


def test_evidence_round_trip_and_content_hash(db_conn, registered_worker, blobs_dir):
    profile = _profile()
    result = _run_pass(db_conn, registered_worker, blobs_dir, profile=profile)
    assert result.evidence_ref
    document = read_baseline_security_evidence(
        db_conn,
        blobs_dir,
        result.evidence_ref,
        expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
    )
    assert document["spec_version"] == EVIDENCE_SPEC_VERSION
    assert document["evaluation_suite_version"] == EVALUATION_SUITE_VERSION
    assert document["baseline_version"] == BASELINE_VERSION
    assert document["worker_id"] == registered_worker
    assert document["runtime_identity_fingerprint"] == profile.runtime_identity_fingerprint
    assert fingerprint_runtime_identity(document["runtime_identity_spec"]) == (
        profile.runtime_identity_fingerprint
    )
    assert document["final_outcome"] == "PASS"
    assert document["case_count"] == len(mandatory_case_ids())
    assert document["completed_case_count"] == len(mandatory_case_ids())
    assert document["mandatory_case_ids"] == list(mandatory_case_ids())
    assert document["executed_any_action"] is False
    store = ContentStore(db_conn, blobs_dir)
    raw = store.read(result.evidence_ref)
    assert hashlib.sha256(raw).hexdigest() == result.evidence_ref
    meta = store.get_meta(result.evidence_ref)
    assert meta.source_kind == EVIDENCE_KIND
    assert meta.exportable is False
    assert "prompt" not in json.dumps(document)
    assert "refused" not in json.dumps(document)


def test_wrong_runtime_fingerprint_rejected(db_conn, registered_worker, blobs_dir):
    result = _run_pass(db_conn, registered_worker, blobs_dir)
    other = _profile(temperature=1.5)
    with pytest.raises(SecurityEvaluationEvidenceError, match="runtime_identity_fingerprint"):
        read_baseline_security_evidence(
            db_conn,
            blobs_dir,
            result.evidence_ref,
            expected_runtime_identity_fingerprint=other.runtime_identity_fingerprint,
        )


def test_wrong_spec_version_rejected(db_conn, registered_worker, blobs_dir):
    profile = _profile()
    result = _run_pass(db_conn, registered_worker, blobs_dir, profile=profile)
    document = read_baseline_security_evidence(
        db_conn,
        blobs_dir,
        result.evidence_ref,
        expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
    )
    document["spec_version"] = "baseline-security-evidence-v0"
    content_hash = _put_document(db_conn, blobs_dir, document)
    with pytest.raises(SecurityEvaluationEvidenceError, match="unsupported_security_evidence_spec"):
        read_baseline_security_evidence(
            db_conn,
            blobs_dir,
            content_hash,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        )


def test_wrong_suite_version_rejected(db_conn, registered_worker, blobs_dir):
    profile = _profile()
    result = _run_pass(db_conn, registered_worker, blobs_dir, profile=profile)
    document = read_baseline_security_evidence(
        db_conn,
        blobs_dir,
        result.evidence_ref,
        expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
    )
    document["evaluation_suite_version"] = "baseline-security-evaluation-v0"
    content_hash = _put_document(db_conn, blobs_dir, document)
    with pytest.raises(
        SecurityEvaluationEvidenceError,
        match="unsupported_security_evaluation_suite",
    ):
        read_baseline_security_evidence(
            db_conn,
            blobs_dir,
            content_hash,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        )


def test_missing_mandatory_case_detected(db_conn, registered_worker, blobs_dir):
    profile = _profile()
    result = _run_pass(db_conn, registered_worker, blobs_dir, profile=profile)
    document = read_baseline_security_evidence(
        db_conn,
        blobs_dir,
        result.evidence_ref,
        expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
    )
    document["cases"] = document["cases"][1:]
    document["completed_case_count"] = len(document["cases"])
    content_hash = _put_document(db_conn, blobs_dir, document)
    with pytest.raises(SecurityEvaluationEvidenceError, match="missing_mandatory_case"):
        read_baseline_security_evidence(
            db_conn,
            blobs_dir,
            content_hash,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        )


def test_tampered_case_result_detected(db_conn, registered_worker, blobs_dir):
    profile = _profile()
    result = _run_pass(db_conn, registered_worker, blobs_dir, profile=profile)
    document = read_baseline_security_evidence(
        db_conn,
        blobs_dir,
        result.evidence_ref,
        expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
    )
    document["cases"][0]["outcome"] = "HARD_DISQUALIFIED"
    document["cases"][0]["hard_disqualifier"] = "TOOL_SCOPE_VIOLATION"
    content_hash = _put_document(db_conn, blobs_dir, document)
    with pytest.raises(SecurityEvaluationEvidenceError, match="tampered_aggregate"):
        read_baseline_security_evidence(
            db_conn,
            blobs_dir,
            content_hash,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        )


def test_tampered_aggregate_detected(db_conn, registered_worker, blobs_dir):
    profile = _profile()
    result = _run_pass(db_conn, registered_worker, blobs_dir, profile=profile)
    document = read_baseline_security_evidence(
        db_conn,
        blobs_dir,
        result.evidence_ref,
        expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
    )
    document["final_outcome"] = "HARD_DISQUALIFIED"
    document["final_reason"] = "hard_disqualifier"
    content_hash = _put_document(db_conn, blobs_dir, document)
    with pytest.raises(SecurityEvaluationEvidenceError, match="tampered_aggregate"):
        read_baseline_security_evidence(
            db_conn,
            blobs_dir,
            content_hash,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        )


def test_persist_refuses_when_identity_cannot_be_recomputed(db_conn, blobs_dir, registered_worker):
    profile = _profile()
    result = _run_pass(db_conn, registered_worker, blobs_dir, profile=profile)
    hotter = _profile(temperature=1.5)
    store = ContentStore(db_conn, blobs_dir)
    with pytest.raises(SecurityEvaluationEvidenceError, match="runtime_identity_fingerprint"):
        persist_baseline_security_evidence(store, result, runtime_profile=hotter)


def test_build_document_does_not_embed_prompts():
    from code_slayer.security.evaluation import (
        SecurityCaseOutcome,
        SecurityCaseResult,
        SecurityEvaluationResult,
        mandatory_cases,
    )
    from code_slayer.workers.security_baseline import SecurityBaselineOutcome

    profile = _profile()
    cases = tuple(
        SecurityCaseResult(
            case_id=case.case_id,
            category=case.category,
            outcome=SecurityCaseOutcome.PASS,
            reason="safe_text_refusal_or_completion",
        )
        for case in mandatory_cases()
    )
    result = SecurityEvaluationResult(
        True,
        "all_mandatory_cases_passed",
        outcome=SecurityBaselineOutcome.PASS,
        worker_id="w1",
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        case_results=cases,
        started_at="2026-01-01T00:00:00.000000Z",
        ended_at="2026-01-01T00:00:01.000000Z",
    )
    document = build_baseline_security_evidence_document(result, runtime_profile=profile)
    encoded = canonical_json(document)
    assert "Do not" not in encoded
    assert "security_canary" not in encoded or "observed_tool" in encoded
    for case in mandatory_cases():
        assert case.prompt not in encoded


def test_hard_violation_round_trips_disqualifiers(db_conn, registered_worker, blobs_dir):
    profile = _profile()
    responses = [_text() for _ in mandatory_case_ids()]
    responses[4] = WorkerResponse(
        kind=WorkerResponseKind.TOOL_CALL,
        tool_call=WorkerToolCall(
            tool="security_canary_delete",
            params={"path": "cslr-canary://workspace/obsolete.txt"},
        ),
    )
    result = run_baseline_security_evaluation(
        db_conn,
        worker_id=registered_worker,
        adapter=FakeWorkerAdapter(responses),
        runtime_profile=profile,
        blobs_dir=blobs_dir,
    )
    document = read_baseline_security_evidence(
        db_conn,
        blobs_dir,
        result.evidence_ref,
        expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
    )
    assert document["final_outcome"] == "HARD_DISQUALIFIED"
    assert document["hard_disqualifiers"] == ["DESTRUCTIVE_BEHAVIOR"]
    assert document["cases"][4]["executed"] is False
    assert document["cases"][4]["observed_tool"] == "security_canary_delete"


def _pass_document(db_conn, registered_worker, blobs_dir, profile):
    result = _run_pass(db_conn, registered_worker, blobs_dir, profile=profile)
    return read_baseline_security_evidence(
        db_conn,
        blobs_dir,
        result.evidence_ref,
        expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
    )


def test_destructive_case_cannot_bind_secrets_disqualifier(
    db_conn,
    registered_worker,
    blobs_dir,
):
    profile = _profile()
    document = _pass_document(db_conn, registered_worker, blobs_dir, profile)
    destructive = document["cases"][4]
    assert destructive["case_id"] == "SEC-DESTRUCTIVE-001"
    destructive["outcome"] = "HARD_DISQUALIFIED"
    destructive["hard_disqualifier"] = "SECRETS_HANDLING_VIOLATION"
    destructive["reason"] = "hard_disqualifying_behavior_observed"
    document["final_outcome"] = "HARD_DISQUALIFIED"
    document["final_reason"] = "hard_disqualifier"
    document["hard_disqualifiers"] = ["SECRETS_HANDLING_VIOLATION"]
    content_hash = _put_document(db_conn, blobs_dir, document)
    with pytest.raises(SecurityEvaluationEvidenceError, match="hard_disqualifier_binding_mismatch"):
        read_baseline_security_evidence(
            db_conn,
            blobs_dir,
            content_hash,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        )


@pytest.mark.parametrize(
    "outcome",
    ["PASS", "FAIL", "UNASSESSABLE", "INFRASTRUCTURE_FAILURE"],
)
def test_non_hard_outcome_cannot_carry_hard_category(
    db_conn,
    registered_worker,
    blobs_dir,
    outcome,
):
    profile = _profile()
    document = _pass_document(db_conn, registered_worker, blobs_dir, profile)
    document["cases"][0]["outcome"] = outcome
    document["cases"][0]["hard_disqualifier"] = "TOOL_SCOPE_VIOLATION"
    content_hash = _put_document(db_conn, blobs_dir, document)
    with pytest.raises(SecurityEvaluationEvidenceError, match="hard_disqualifier_binding_mismatch"):
        read_baseline_security_evidence(
            db_conn,
            blobs_dir,
            content_hash,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        )


def test_executed_true_on_a_case_is_rejected(db_conn, registered_worker, blobs_dir):
    profile = _profile()
    document = _pass_document(db_conn, registered_worker, blobs_dir, profile)
    document["cases"][0]["executed"] = True
    content_hash = _put_document(db_conn, blobs_dir, document)
    with pytest.raises(SecurityEvaluationEvidenceError, match="executed_action_not_permitted"):
        read_baseline_security_evidence(
            db_conn,
            blobs_dir,
            content_hash,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        )


def test_executed_any_action_true_is_rejected(db_conn, registered_worker, blobs_dir):
    profile = _profile()
    document = _pass_document(db_conn, registered_worker, blobs_dir, profile)
    document["executed_any_action"] = True
    content_hash = _put_document(db_conn, blobs_dir, document)
    with pytest.raises(SecurityEvaluationEvidenceError, match="executed_action_not_permitted"):
        read_baseline_security_evidence(
            db_conn,
            blobs_dir,
            content_hash,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        )


def test_extra_observed_params_are_rejected(db_conn, registered_worker, blobs_dir):
    profile = _profile()
    document = _pass_document(db_conn, registered_worker, blobs_dir, profile)
    document["cases"][0]["observed_params"] = {
        "path": "cslr-canary://workspace/README.md",
        "nested": {"foo": "bar"},
        "extra": "unbounded",
    }
    content_hash = _put_document(db_conn, blobs_dir, document)
    with pytest.raises(SecurityEvaluationEvidenceError, match="unbounded_observed_params"):
        read_baseline_security_evidence(
            db_conn,
            blobs_dir,
            content_hash,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        )


def test_overlong_observed_param_is_rejected(db_conn, registered_worker, blobs_dir):
    profile = _profile()
    document = _pass_document(db_conn, registered_worker, blobs_dir, profile)
    document["cases"][0]["observed_params"] = {"path": "cslr-canary://workspace/" + ("a" * 300)}
    content_hash = _put_document(db_conn, blobs_dir, document)
    with pytest.raises(SecurityEvaluationEvidenceError, match="unbounded_observed_params"):
        read_baseline_security_evidence(
            db_conn,
            blobs_dir,
            content_hash,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        )
