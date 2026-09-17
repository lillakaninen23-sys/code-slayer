"""Durable Planner qualification evidence (`planning.qualification_evidence`)
and its binding into `planning.planner_certification`.

These tests prove the forensic gap closed before the first real Planner
certificate: the canonical runtime-config spec that produced a
certificate's fingerprint is durably reconstructable, attempt
provenance survives process restart, and issuance fails closed without
that evidence. Nothing here issues a live/production certificate.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_slayer.planning.fake_planner import FakePlanner
from code_slayer.planning.planner import (
    PlannerOutcome,
    PlannerRequest,
    PlannerResponse,
    PlannerStructuredOutput,
    ToolCallTransport,
)
from code_slayer.planning.planner_certification import certify_planner_from_qualification
from code_slayer.planning.qualification import (
    RuntimeContextProfile,
    run_planner_case_with_correction,
)
from code_slayer.planning.qualification_evidence import (
    QUALIFICATION_EVIDENCE_KIND,
    QUALIFICATION_EVIDENCE_SPEC_VERSION,
    QualificationEvidenceError,
    agreed_runtime_config_spec,
    build_planner_qualification_evidence_document,
    persist_planner_qualification_evidence,
    read_planner_qualification_evidence,
    verify_runtime_config_fingerprint,
)
from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import connect, migrate
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.role_qualification import ProductionRole
from code_slayer.workers.security_baseline import (
    RUNTIME_CONFIG_SPEC_VERSION,
    fingerprint_runtime_config,
    runtime_profile_identity_from_config,
)

_SECRET_PROMPT = "QUAL-EVIDENCE-PROMPT-MARKER-do-not-persist-7f3c"
_SECRET_RAW = "QUAL-EVIDENCE-RAW-MODEL-TEXT-do-not-persist-9a1e"

_REQUEST = PlannerRequest(
    original_request=f"Add a read-only endpoint. {_SECRET_PROMPT}",
)

_PROFILE = RuntimeContextProfile(
    model_tag="devstral:24b",
    effective_context_tokens=8192,
    output_token_budget=1024,
    model_digest="sha256:abc",
    endpoint="http://local:11436/v1",
    runtime_version="0.1.0",
    temperature=0.0,
)


def _structured(*, transport=ToolCallTransport.NATIVE) -> PlannerResponse:
    return PlannerResponse(
        PlannerOutcome.STRUCTURED,
        output=PlannerStructuredOutput(goal="Add the requested read-only endpoint"),
        raw=_SECRET_RAW,
        tool_call_transport=transport,
        original_transport_text=(
            "<function=emit_engineering_plan></function>"
            if transport == ToolCallTransport.NORMALIZED
            else None
        ),
        normalizer_id=(
            "qwen_textual_tool_v1" if transport == ToolCallTransport.NORMALIZED else None
        ),
        normalizer_version=1 if transport == ToolCallTransport.NORMALIZED else None,
    )


def _pass_result(profile=_PROFILE, transport=ToolCallTransport.NATIVE):
    planner = FakePlanner([_structured(transport=transport)])
    return run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=profile,
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


def _certify(conn, blobs_dir, results, *, early_stopped=False, worker_id="w1"):
    return certify_planner_from_qualification(
        conn,
        worker_id=worker_id,
        results=results,
        early_stopped=early_stopped,
        blobs_dir=blobs_dir,
    )


def _assert_no_unrelated_authority(conn):
    assert conn.execute("SELECT count(*) AS c FROM worker_trust_events").fetchone()["c"] == 0
    assert BaselineSecurityCertificatesRepo(conn).list_for_worker("w1") == []
    assert conn.execute("SELECT count(*) AS c FROM permission_grants").fetchone()["c"] == 0
    assert conn.execute("SELECT count(*) AS c FROM permission_requests").fetchone()["c"] == 0
    for role in ProductionRole:
        if role is ProductionRole.PLANNER:
            continue
        assert RoleCertificatesRepo(conn).list_for_worker_role("w1", role.value) == []


# -- 1. canonical runtime spec round-trips durably ---------------------------


def test_canonical_runtime_spec_round_trips_durably(conn, blobs_dir):
    evidence = _pass_result()
    result = _certify(conn, blobs_dir, (evidence,))
    assert result.ok
    document = read_planner_qualification_evidence(
        conn,
        blobs_dir,
        result.certificate.evidence_ref,
        expected_runtime_config_fingerprint=result.certificate.runtime_config_fingerprint,
    )
    assert document["spec_version"] == QUALIFICATION_EVIDENCE_SPEC_VERSION
    spec = document["runtime_config_spec"]
    assert spec["spec_version"] == RUNTIME_CONFIG_SPEC_VERSION
    expected = agreed_runtime_config_spec((evidence,))
    assert spec == expected
    assert document["model_tag"] == _PROFILE.model_tag
    assert document["model_digest"] == _PROFILE.model_digest
    assert document["endpoint"] == _PROFILE.endpoint
    assert document["runtime_version"] == _PROFILE.runtime_version
    assert document["normalizer_id"] is None
    assert document["normalizer_version"] is None
    assert document["effective_context_tokens"] == _PROFILE.effective_context_tokens
    assert document["temperature"] == _PROFILE.temperature
    assert document["output_token_budget"] == _PROFILE.output_token_budget
    assert document["tool_choice_enforcement"] == _PROFILE.tool_choice_enforcement


# -- 2. recomputed fingerprint equals the certificate runtime fingerprint ----


def test_recomputed_spec_fingerprint_equals_certificate_fingerprint(conn, blobs_dir):
    evidence = _pass_result()
    result = _certify(conn, blobs_dir, (evidence,))
    document = read_planner_qualification_evidence(
        conn,
        blobs_dir,
        result.certificate.evidence_ref,
        expected_runtime_config_fingerprint=result.certificate.runtime_config_fingerprint,
    )
    recomputed = fingerprint_runtime_config(document["runtime_config_spec"])
    assert recomputed == result.certificate.runtime_config_fingerprint
    assert recomputed == document["runtime_config_fingerprint"]
    expected = runtime_profile_identity_from_config(
        model_tag=_PROFILE.model_tag,
        model_digest=_PROFILE.model_digest,
        endpoint=_PROFILE.endpoint,
        runtime_version=_PROFILE.runtime_version,
        effective_context_tokens=_PROFILE.effective_context_tokens,
        output_token_budget=_PROFILE.output_token_budget,
        temperature=_PROFILE.temperature,
        tool_choice_enforcement=_PROFILE.tool_choice_enforcement,
        normalizer_id=_PROFILE.normalizer_id,
        normalizer_version=_PROFILE.normalizer_version,
    )
    assert recomputed == expected.runtime_config_fingerprint
    verify_runtime_config_fingerprint(
        document["runtime_config_spec"],
        result.certificate.runtime_config_fingerprint,
    )


# -- 3. tampered / mismatched spec fails closed ------------------------------


def test_tampered_spec_fails_closed_before_persist(conn, blobs_dir):
    evidence = _pass_result()
    spec = agreed_runtime_config_spec((evidence,))
    fingerprint = fingerprint_runtime_config(spec)
    document = build_planner_qualification_evidence_document(
        results=(evidence,),
        runtime_config_spec=spec,
        runtime_config_fingerprint=fingerprint,
        policy_version="planner-certification-v1",
        classification="PASS_FIRST_TRY",
    )
    tampered = dict(document)
    tampered_spec = dict(spec)
    tampered_spec["temperature"] = 1.5
    tampered["runtime_config_spec"] = tampered_spec
    store = ContentStore(conn, blobs_dir)
    with pytest.raises(QualificationEvidenceError, match="runtime_config_fingerprint_mismatch"):
        persist_planner_qualification_evidence(
            store,
            tampered,
            expected_runtime_config_fingerprint=fingerprint,
        )
    assert conn.execute("SELECT count(*) AS c FROM content_blobs").fetchone()["c"] == 0
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


def test_mismatched_expected_fingerprint_fails_closed(conn, blobs_dir):
    evidence = _pass_result()
    spec = agreed_runtime_config_spec((evidence,))
    fingerprint = fingerprint_runtime_config(spec)
    document = build_planner_qualification_evidence_document(
        results=(evidence,),
        runtime_config_spec=spec,
        runtime_config_fingerprint=fingerprint,
        policy_version="planner-certification-v1",
        classification="PASS_FIRST_TRY",
    )
    other = dict(spec)
    other["temperature"] = 1.5
    other_fp = fingerprint_runtime_config(other)
    store = ContentStore(conn, blobs_dir)
    with pytest.raises(QualificationEvidenceError, match="runtime_config_fingerprint_mismatch"):
        persist_planner_qualification_evidence(
            store,
            document,
            expected_runtime_config_fingerprint=other_fp,
        )
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []


# -- 4. qualification attempt provenance survives process restart ------------


def test_qualification_attempt_provenance_survives_process_restart(tmp_path):
    db_path = tmp_path / "state.db"
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    conn = connect(db_path)
    migrate(conn)
    WorkersRepo(conn).register(worker_id="w1", kind="fake", network_class="local")
    evidence = _pass_result()
    issued = _certify(conn, blobs, (evidence,))
    assert issued.ok
    evidence_ref = issued.certificate.evidence_ref
    fingerprint = issued.certificate.runtime_config_fingerprint
    original_transport = evidence.provenance[0].tool_call_transport
    original_request_fp = evidence.provenance[0].request_fingerprint
    conn.close()

    reopened = connect(db_path)
    try:
        document = read_planner_qualification_evidence(
            reopened,
            blobs,
            evidence_ref,
            expected_runtime_config_fingerprint=fingerprint,
        )
        cert = RoleCertificatesRepo(reopened).get(issued.certificate.certificate_id)
        assert cert is not None
        assert cert.evidence_ref == evidence_ref
        assert cert.runtime_config_fingerprint == fingerprint
        assert fingerprint_runtime_config(document["runtime_config_spec"]) == fingerprint
        attempt = document["instances"][0]["provenance"][0]
        assert attempt["request_fingerprint"] == original_request_fp
        assert attempt["task_fingerprint"] == evidence.provenance[0].task_fingerprint
        repo_fp = evidence.provenance[0].repo_context_fingerprint
        assert attempt["repo_context_fingerprint"] == repo_fp
        assert attempt["schema_fingerprint"] == evidence.provenance[0].schema_fingerprint
        assert attempt["tool_call_transport"] == original_transport
        assert attempt["full_input_preservation_verified"] is False
    finally:
        reopened.close()


# -- 5. no raw prompt / model text leaks into the qualification document -----


def test_no_raw_prompt_or_model_text_in_qualification_evidence(conn, blobs_dir):
    evidence = _pass_result()
    result = _certify(conn, blobs_dir, (evidence,))
    raw = ContentStore(conn, blobs_dir).read(result.certificate.evidence_ref)
    payload = raw.decode("utf-8")
    assert _SECRET_PROMPT not in payload
    assert _SECRET_RAW not in payload
    assert "Add a read-only endpoint" not in payload
    document = json.loads(payload)
    dumped = json.dumps(document)
    assert _SECRET_PROMPT not in dumped
    assert _SECRET_RAW not in dumped
    for forbidden in (
        "raw",
        "original_request",
        "prompt",
        "text",
        "error",
        "detail",
        "required_correction",
        "observed_behaviour",
        "original_transport_text",
        "prior_attempt_feedback",
    ):
        assert forbidden not in document
        assert forbidden not in document["instances"][0]
        assert forbidden not in document["instances"][0]["provenance"][0]


# -- 6. native / normalized transport distinction is retained ----------------


def test_native_vs_normalized_transport_is_retained_in_evidence(conn, blobs_dir):
    native = _pass_result(transport=ToolCallTransport.NATIVE)
    result = _certify(conn, blobs_dir, (native,))
    document = read_planner_qualification_evidence(
        conn,
        blobs_dir,
        result.certificate.evidence_ref,
        expected_runtime_config_fingerprint=result.certificate.runtime_config_fingerprint,
    )
    assert document["instances"][0]["provenance"][0]["tool_call_transport"] == "NATIVE"
    assert document["normalizer_id"] is None

    normalized_profile = RuntimeContextProfile(
        model_tag=_PROFILE.model_tag,
        effective_context_tokens=_PROFILE.effective_context_tokens,
        output_token_budget=_PROFILE.output_token_budget,
        model_digest=_PROFILE.model_digest,
        endpoint=_PROFILE.endpoint,
        runtime_version=_PROFILE.runtime_version,
        temperature=_PROFILE.temperature,
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
    )
    WorkersRepo(conn).register(worker_id="w-norm", kind="fake", network_class="local")
    normalized = _pass_result(
        profile=normalized_profile,
        transport=ToolCallTransport.NORMALIZED,
    )
    norm_result = _certify(conn, blobs_dir, (normalized,), worker_id="w-norm")
    assert norm_result.ok
    norm_doc = read_planner_qualification_evidence(
        conn,
        blobs_dir,
        norm_result.certificate.evidence_ref,
        expected_runtime_config_fingerprint=norm_result.certificate.runtime_config_fingerprint,
    )
    assert norm_doc["instances"][0]["provenance"][0]["tool_call_transport"] == "NORMALIZED"
    assert norm_doc["normalizer_id"] == "qwen_textual_tool_v1"
    assert norm_doc["normalizer_version"] == 1
    assert result.certificate.runtime_config_fingerprint != (
        norm_result.certificate.runtime_config_fingerprint
    )


# -- 7. issuance refuses missing durable qualification evidence --------------


def test_certificate_issuance_refuses_missing_durable_store(conn):
    evidence = _pass_result()
    result = certify_planner_from_qualification(
        conn,
        worker_id="w1",
        results=(evidence,),
        early_stopped=False,
        blobs_dir="",
    )
    assert not result.ok
    assert result.reason == "missing_durable_qualification_evidence"
    assert result.certificate is None
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []
    _assert_no_unrelated_authority(conn)


def test_certificate_issuance_refuses_when_persist_fails(conn, blobs_dir, monkeypatch):
    evidence = _pass_result()

    def _boom(*_args, **_kwargs):
        raise QualificationEvidenceError("missing_durable_qualification_evidence")

    monkeypatch.setattr(
        "code_slayer.planning.planner_certification.persist_planner_qualification_evidence",
        _boom,
    )
    result = _certify(conn, blobs_dir, (evidence,))
    assert not result.ok
    assert result.reason == "missing_durable_qualification_evidence"
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []
    _assert_no_unrelated_authority(conn)


# -- 8. no trust / permission / baseline / unrelated role certificate --------


def test_planner_certification_creates_no_unrelated_authority(conn, blobs_dir):
    evidence = _pass_result()
    result = _certify(conn, blobs_dir, (evidence,))
    assert result.ok
    assert result.certificate.role == "PLANNER"
    planner_certs = RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER")
    assert len(planner_certs) == 1
    _assert_no_unrelated_authority(conn)
    meta = ContentStore(conn, blobs_dir).get_meta(result.certificate.evidence_ref)
    assert meta.source_kind == QUALIFICATION_EVIDENCE_KIND
    assert meta.exportable is False


def test_qualification_evidence_blob_does_not_grant_authority(conn, blobs_dir):
    """Persisting the evidence document alone, without certification,
    must not create any certificate, trust event, or permission."""
    evidence = _pass_result()
    spec = agreed_runtime_config_spec((evidence,))
    fingerprint = fingerprint_runtime_config(spec)
    document = build_planner_qualification_evidence_document(
        results=(evidence,),
        runtime_config_spec=spec,
        runtime_config_fingerprint=fingerprint,
        policy_version="planner-certification-v1",
        classification="PASS_FIRST_TRY",
    )
    persist_planner_qualification_evidence(
        ContentStore(conn, blobs_dir),
        document,
        expected_runtime_config_fingerprint=fingerprint,
    )
    assert RoleCertificatesRepo(conn).list_for_worker_role("w1", "PLANNER") == []
    _assert_no_unrelated_authority(conn)


def test_old_certificates_are_not_rewritten(conn, blobs_dir):
    first = _certify(conn, blobs_dir, (_pass_result(),))
    first_id = first.certificate.certificate_id
    first_ref = first.certificate.evidence_ref
    first_issued = first.certificate.issued_at
    second = _certify(conn, blobs_dir, (_pass_result(),))
    repo = RoleCertificatesRepo(conn)
    original = repo.get(first_id)
    assert original is not None
    assert original.evidence_ref == first_ref
    assert original.issued_at == first_issued
    assert original.certificate_id != second.certificate.certificate_id
    assert len(repo.list_for_worker_role("w1", "PLANNER")) == 2


def test_document_records_attempt_count_correction_and_classification(conn, blobs_dir):
    from code_slayer.planning.planner import PlannerFailureCategory

    malformed = PlannerResponse(
        PlannerOutcome.MALFORMED,
        error="invalid_transport_response:text_response",
        failure_category=PlannerFailureCategory.NON_TOOL_RESPONSE,
        raw=_SECRET_RAW,
    )
    planner = FakePlanner([malformed, _structured()])
    evidence = run_planner_case_with_correction(
        planner,
        _REQUEST,
        qualification_class="C",
        context_profile=_PROFILE,
    )
    result = _certify(conn, blobs_dir, (evidence,))
    assert result.ok
    assert result.certificate.classification == "PASS_AFTER_FEEDBACK"
    document = read_planner_qualification_evidence(
        conn,
        blobs_dir,
        result.certificate.evidence_ref,
        expected_runtime_config_fingerprint=result.certificate.runtime_config_fingerprint,
    )
    assert document["final_classification"] == "PASS_AFTER_FEEDBACK"
    assert document["attempt_count"] == 2
    assert document["correction_used"] is True
    assert document["instances"][0]["qualification_class"] == "C"
    assert document["instances"][0]["attempt_count"] == 2
    assert _SECRET_RAW not in json.dumps(document)
    # blobs_dir type is retained for restart-style callers
    assert isinstance(blobs_dir, Path)
