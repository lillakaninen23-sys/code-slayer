"""Common runtime identity vs role/evaluation identity.

Proves the v2 split: Baseline Security and every production role share
one exact-match common runtime identity, while Planner (and future
roles) bind a separate evaluation profile. Historical v1
runtime-config-spec fingerprints are never reinterpreted. No live
certificate is issued; identity constructors and eligibility reads have
no trust/permission/certificate side effects.
"""

from __future__ import annotations

import inspect
import json
import sqlite3

import pytest

from code_slayer.planning.planner_certification import PLANNER_CERTIFICATION_POLICY_VERSION
from code_slayer.planning.qualification_evidence import (
    QUALIFICATION_EVIDENCE_KIND,
    QUALIFICATION_EVIDENCE_SPEC_VERSION_V1,
    QualificationEvidenceError,
    persist_planner_qualification_evidence,
    read_planner_qualification_evidence,
)
from code_slayer.store import db as db_module
from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import connect, transaction, utcnow_iso
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.production_eligibility import (
    EligibilityDecision,
    evaluate_production_eligibility,
)
from code_slayer.workers.role_qualification import (
    ProductionRole,
    RoleEvaluationIdentity,
    RoleQualificationOutcome,
    canonical_role_evaluation_spec,
    fingerprint_role_evaluation,
    record_role_certificate,
    role_evaluation_identity_from_config,
)
from code_slayer.workers.security_baseline import (
    HardDisqualifierCategory,
    RuntimeProfileIdentity,
    SecurityBaselineOutcome,
    canonical_runtime_config_spec,
    canonical_runtime_identity_spec,
    fingerprint_runtime_config,
    fingerprint_runtime_identity,
    record_baseline_certificate,
    runtime_profile_binding_from_stored,
    runtime_profile_identity_from_config,
)
from code_slayer.workers.trust import TrustLevel, WorkerTrustManager

POLICY_VERSION = PLANNER_CERTIFICATION_POLICY_VERSION
HISTORICAL_V1_FINGERPRINT = "6db4abb1296545e63bf422fb40d52222bc44cc4e4e20287ae8f4f006d2832855"
HISTORICAL_CERT_ID = "eee8d68ea3884bddb4c350b29f5a2be3"
HISTORICAL_EVIDENCE_REF = "d1f13df24cd3aab14f4b4cd2e7a7da906395bd304b4d972e5869c2986512ba87"


@pytest.fixture
def registered_worker(db_conn) -> str:
    WorkersRepo(db_conn).register(worker_id="w1", kind="fake", network_class="local")
    return "w1"


def _common(**overrides) -> RuntimeProfileIdentity:
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


def _eval_for(profile, **overrides):
    kwargs = dict(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        output_token_budget=4096,
        tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
        policy_version=POLICY_VERSION,
    )
    kwargs.update(overrides)
    return role_evaluation_identity_from_config(**kwargs)


def _security_pass(conn, worker_id, profile, *, evidence_ref="sec-ev"):
    return record_baseline_certificate(
        conn,
        worker_id=worker_id,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref=evidence_ref,
        reason="ok",
    )


def _role_pass(conn, worker_id, profile, role_evaluation, *, evidence_ref="role-ev"):
    return record_role_certificate(
        conn,
        worker_id=worker_id,
        role=role_evaluation.role,
        runtime_profile=profile,
        policy_version=role_evaluation.policy_version,
        outcome=RoleQualificationOutcome.PASS,
        classification="PASS_FIRST_TRY",
        evidence_ref=evidence_ref,
        reason="ok",
        role_evaluation=role_evaluation,
    )


# -- 1. same runtime, different Planner output-token budgets -----------------


def test_output_token_budget_does_not_change_common_runtime_identity():
    a = _common()
    b_budget = 1024
    identity_spec = canonical_runtime_identity_spec(
        model_tag=a.model_tag,
        model_digest=a.model_digest,
        endpoint=a.endpoint,
        runtime_version=a.runtime_version,
        normalizer_id=a.normalizer_id,
        normalizer_version=a.normalizer_version,
        effective_context_tokens=16384,
        temperature=0.0,
    )
    assert "output_token_budget" not in identity_spec
    eval_4096 = _eval_for(a, output_token_budget=4096)
    eval_1024 = _eval_for(a, output_token_budget=b_budget)
    assert a.runtime_identity_fingerprint == fingerprint_runtime_identity(identity_spec)
    assert eval_4096.runtime_identity_fingerprint == eval_1024.runtime_identity_fingerprint
    assert eval_4096.role_evaluation_fingerprint != eval_1024.role_evaluation_fingerprint
    v1_4096 = fingerprint_runtime_config(
        canonical_runtime_config_spec(
            model_tag=a.model_tag,
            model_digest=a.model_digest,
            endpoint=a.endpoint,
            runtime_version=a.runtime_version,
            normalizer_id=a.normalizer_id,
            normalizer_version=a.normalizer_version,
            effective_context_tokens=16384,
            output_token_budget=4096,
            temperature=0.0,
            tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
        ),
    )
    v1_1024 = fingerprint_runtime_config(
        canonical_runtime_config_spec(
            model_tag=a.model_tag,
            model_digest=a.model_digest,
            endpoint=a.endpoint,
            runtime_version=a.runtime_version,
            normalizer_id=a.normalizer_id,
            normalizer_version=a.normalizer_version,
            effective_context_tokens=16384,
            output_token_budget=1024,
            temperature=0.0,
            tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
        ),
    )
    assert v1_4096 != v1_1024
    assert v1_4096 != a.runtime_identity_fingerprint


# -- 2. same runtime, different tool-choice evaluation configuration ---------


def test_tool_choice_enforcement_does_not_change_common_runtime_identity():
    a = _common()
    eval_advisory = _eval_for(a, tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED")
    eval_required = _eval_for(a, tool_choice_enforcement="REQUIRED_STRUCTURED_TOOL")
    assert eval_advisory.runtime_identity_fingerprint == eval_required.runtime_identity_fingerprint
    assert eval_advisory.runtime_identity_fingerprint == a.runtime_identity_fingerprint
    assert eval_advisory.role_evaluation_fingerprint != eval_required.role_evaluation_fingerprint


# -- 3. genuine common-runtime field changes ---------------------------------


def test_common_runtime_identity_changes_with_runtime_invariants():
    base = _common()
    hotter = _common(temperature=1.5)
    smaller_ctx = _common(effective_context_tokens=8192)
    other_digest = _common(model_digest="sha256:other")
    other_endpoint = _common(endpoint="http://other:11434/v1")
    other_runtime = _common(runtime_version="0.17.0")
    native = _common(normalizer_id=None, normalizer_version=None)
    fingerprints = {
        base.runtime_identity_fingerprint,
        hotter.runtime_identity_fingerprint,
        smaller_ctx.runtime_identity_fingerprint,
        other_digest.runtime_identity_fingerprint,
        other_endpoint.runtime_identity_fingerprint,
        other_runtime.runtime_identity_fingerprint,
        native.runtime_identity_fingerprint,
    }
    assert len(fingerprints) == 7
    assert not base.matches(hotter)
    assert not base.matches(native)


# -- 4/5. Baseline Security + Planner share common runtime; eval mismatch ----


def test_security_and_planner_can_share_common_runtime_with_distinct_protocols(
    db_conn,
    registered_worker,
):
    profile = _common()
    planner_eval = _eval_for(profile, output_token_budget=4096)
    security = _security_pass(db_conn, registered_worker, profile)
    role = _role_pass(db_conn, registered_worker, profile, planner_eval)
    decision = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=profile,
        role_evaluation=planner_eval,
        expected_role_policy_version=POLICY_VERSION,
    )
    assert decision.eligible
    assert decision.security_certificate_id == security.certificate.certificate_id
    assert decision.role_certificate_id == role.certificate.certificate_id
    other_eval = _eval_for(profile, output_token_budget=1024)
    mismatch = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=profile,
        role_evaluation=other_eval,
        expected_role_policy_version=POLICY_VERSION,
    )
    assert not mismatch.eligible
    assert mismatch.reason == "role_certificate_evaluation_profile_mismatch"
    assert mismatch.security_certificate_id == security.certificate.certificate_id


# -- 6. old v1 Planner certificate is historical and cannot wildcard ---------


def test_historical_v1_planner_certificate_is_immutable_and_cannot_wildcard(
    db_conn,
    registered_worker,
):
    profile = _common()
    _security_pass(db_conn, registered_worker, profile)
    with transaction(db_conn):
        RoleCertificatesRepo(db_conn).record_in_transaction(
            certificate_id=HISTORICAL_CERT_ID,
            worker_id=registered_worker,
            role=ProductionRole.PLANNER.value,
            policy_version=POLICY_VERSION,
            model_tag=profile.model_tag,
            model_digest=profile.model_digest,
            endpoint=profile.endpoint,
            runtime_version=profile.runtime_version,
            normalizer_id=profile.normalizer_id,
            normalizer_version=profile.normalizer_version,
            runtime_config_fingerprint=HISTORICAL_V1_FINGERPRINT,
            runtime_identity_fingerprint=None,
            role_evaluation_fingerprint=None,
            outcome=RoleQualificationOutcome.PASS.value,
            classification="PASS_FIRST_TRY",
            evidence_ref=HISTORICAL_EVIDENCE_REF,
            reason="historical_v1",
            issued_at="2026-04-01T00:00:00.000000Z",
        )
    before = RoleCertificatesRepo(db_conn).get(HISTORICAL_CERT_ID)
    assert before is not None
    assert before.runtime_config_fingerprint == HISTORICAL_V1_FINGERPRINT
    assert before.runtime_identity_fingerprint is None
    assert before.role_evaluation_fingerprint is None
    decision = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=profile,
        role_evaluation=_eval_for(profile),
        expected_role_policy_version=POLICY_VERSION,
    )
    assert not decision.eligible
    assert decision.reason == "role_certificate_profile_mismatch"
    after = RoleCertificatesRepo(db_conn).get(HISTORICAL_CERT_ID)
    assert after == before
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "UPDATE worker_role_certificates SET runtime_identity_fingerprint = ? "
            "WHERE certificate_id = ?",
            (profile.runtime_identity_fingerprint, HISTORICAL_CERT_ID),
        )


# -- 7. hard Security disqualifier still overrides a passing role cert -------


def test_hard_security_disqualifier_overrides_passing_role_certificate(
    db_conn,
    registered_worker,
):
    profile = _common()
    security = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.HARD_DISQUALIFIED,
        evidence_ref="sec-hard",
        reason="attempted_policy_bypass",
        hard_disqualifiers=(HardDisqualifierCategory.POLICY_OR_GATE_BYPASS_ATTEMPT,),
    )
    _role_pass(db_conn, registered_worker, profile, _eval_for(profile))
    decision = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=profile,
        role_evaluation=_eval_for(profile),
        expected_role_policy_version=POLICY_VERSION,
    )
    assert decision == EligibilityDecision(
        False,
        "security_hard_disqualifier",
        security_certificate_id=security.certificate.certificate_id,
        role_certificate_id=None,
    )


# -- 8. missing/ambiguous new identity fields fail closed --------------------


def test_missing_new_identity_fields_fail_closed(db_conn, registered_worker):
    profile = _common()
    incomplete = RuntimeProfileIdentity(
        model_tag=profile.model_tag,
        model_digest=profile.model_digest,
        endpoint=profile.endpoint,
        runtime_version=profile.runtime_version,
        normalizer_id=profile.normalizer_id,
        normalizer_version=profile.normalizer_version,
        runtime_config_fingerprint=HISTORICAL_V1_FINGERPRINT,
    )
    assert not incomplete.is_fully_specified
    decision = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=incomplete,
        role_evaluation=_eval_for(profile),
        expected_role_policy_version=POLICY_VERSION,
    )
    assert decision.reason == "insufficient_runtime_profile_identity"
    with pytest.raises(ValueError, match="sha256"):
        role_evaluation_identity_from_config(
            role=ProductionRole.PLANNER,
            runtime_identity_fingerprint=None,
            output_token_budget=4096,
            tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
            policy_version=POLICY_VERSION,
        )


# -- 9. no model response can supply or spoof either fingerprint -------------


def test_factories_do_not_accept_caller_or_model_supplied_fingerprints():
    common_params = set(inspect.signature(runtime_profile_identity_from_config).parameters)
    eval_params = set(inspect.signature(role_evaluation_identity_from_config).parameters)
    stored_params = set(inspect.signature(runtime_profile_binding_from_stored).parameters)
    for forbidden in (
        "runtime_config_fingerprint",
        "runtime_identity_fingerprint",
        "role_evaluation_fingerprint",
        "fingerprint",
    ):
        assert forbidden not in common_params
    assert "role_evaluation_fingerprint" not in eval_params
    assert "runtime_config_fingerprint" not in eval_params
    assert "effective_context_tokens" not in stored_params
    assert "temperature" not in stored_params
    eligibility_params = set(inspect.signature(evaluate_production_eligibility).parameters)
    assert "outcome" not in eligibility_params
    assert "role_status" not in eligibility_params
    assert "pass_fail" not in eligibility_params


# -- 10. constructors and eligibility reads have no authority side effects ---


def test_identity_constructors_and_eligibility_reads_have_no_side_effects(
    db_conn,
    registered_worker,
):
    profile = _common()
    evaluation = _eval_for(profile)
    assert profile.runtime_identity_fingerprint
    assert evaluation.role_evaluation_fingerprint
    decision = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=profile,
        role_evaluation=evaluation,
        expected_role_policy_version=POLICY_VERSION,
    )
    assert not decision.eligible
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []
    assert RoleCertificatesRepo(db_conn).list_for_worker_role(registered_worker, "PLANNER") == []
    assert db_conn.execute("SELECT count(*) AS c FROM worker_trust_events").fetchone()["c"] == 0
    assert db_conn.execute("SELECT count(*) AS c FROM permission_grants").fetchone()["c"] == 0
    manager = WorkerTrustManager(db_conn)
    assert manager.current_trust(registered_worker, "coder", "read_file") == TrustLevel.LOCKED


# -- 11. migration 0015 is forward-only; existing rows are not rewritten -----


def _apply_through(conn, version: int) -> None:
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


def test_migration_0015_does_not_rewrite_existing_certificate_rows(tmp_path):
    path = tmp_path / "state.db"
    conn = connect(path)
    _apply_through(conn, 14)
    assert db_module.schema_version(conn) == 14
    WorkersRepo(conn).register(worker_id="w1", kind="fake", network_class="local")
    conn.execute(
        "INSERT INTO worker_role_certificates "
        "(certificate_id, worker_id, role, policy_version, model_tag, model_digest, "
        "endpoint, runtime_version, normalizer_id, normalizer_version, "
        "runtime_config_fingerprint, outcome, classification, evidence_ref, reason, issued_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            HISTORICAL_CERT_ID,
            "w1",
            "PLANNER",
            POLICY_VERSION,
            "qwen3-coder-ctx16k:30b",
            "sha256:abc",
            "http://192.168.32.8:11434/v1",
            "0.16.1",
            "qwen_textual_tool_v1",
            1,
            HISTORICAL_V1_FINGERPRINT,
            "PASS",
            "PASS_FIRST_TRY",
            HISTORICAL_EVIDENCE_REF,
            "historical_v1",
            "2026-04-01T00:00:00.000000Z",
        ),
    )
    conn.execute(
        "INSERT INTO worker_baseline_security_certificates "
        "(certificate_id, worker_id, baseline_version, model_tag, model_digest, "
        "endpoint, runtime_version, normalizer_id, normalizer_version, "
        "runtime_config_fingerprint, outcome, hard_disqualifiers_json, "
        "evidence_ref, reason, issued_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "sec-historical",
            "w1",
            "baseline-security-v1",
            "qwen3-coder-ctx16k:30b",
            "sha256:abc",
            "http://192.168.32.8:11434/v1",
            "0.16.1",
            "qwen_textual_tool_v1",
            1,
            HISTORICAL_V1_FINGERPRINT,
            "PASS",
            "[]",
            "sec-ev",
            "ok",
            "2026-04-01T00:00:00.000000Z",
        ),
    )
    before_role = dict(
        conn.execute(
            "SELECT * FROM worker_role_certificates WHERE certificate_id = ?",
            (HISTORICAL_CERT_ID,),
        ).fetchone(),
    )
    before_sec = dict(
        conn.execute(
            "SELECT * FROM worker_baseline_security_certificates WHERE certificate_id = ?",
            ("sec-historical",),
        ).fetchone(),
    )
    assert "runtime_identity_fingerprint" not in before_role
    assert db_module.migrate(conn) == 16
    after_role = dict(
        conn.execute(
            "SELECT * FROM worker_role_certificates WHERE certificate_id = ?",
            (HISTORICAL_CERT_ID,),
        ).fetchone(),
    )
    after_sec = dict(
        conn.execute(
            "SELECT * FROM worker_baseline_security_certificates WHERE certificate_id = ?",
            ("sec-historical",),
        ).fetchone(),
    )
    for key, value in before_role.items():
        assert after_role[key] == value
    for key, value in before_sec.items():
        assert after_sec[key] == value
    assert after_role["runtime_identity_fingerprint"] is None
    assert after_role["role_evaluation_fingerprint"] is None
    assert after_sec["runtime_identity_fingerprint"] is None
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE worker_role_certificates SET role_evaluation_fingerprint = 'x' "
            "WHERE certificate_id = ?",
            (HISTORICAL_CERT_ID,),
        )
    conn.close()


def test_schema_v16_is_known_and_prior_identity_migrations_are_untouched():
    assert db_module.known_schema_version() == 16
    names = [name for _version, name, _sql in db_module._discover_migrations()]
    assert "runtime_profile_normalizer_identity" in names
    assert "runtime_config_fingerprint" in names
    assert "runtime_identity_separation" in names
    assert "certification_runs" in names


# -- 12. durable Planner evidence round-trips both fingerprints --------------


def test_durable_planner_evidence_round_trips_both_fingerprints_and_detects_tampering(
    db_conn,
    tmp_path,
    registered_worker,
):
    from code_slayer.planning.fake_planner import FakePlanner
    from code_slayer.planning.planner import (
        PlannerOutcome,
        PlannerRequest,
        PlannerResponse,
        PlannerStructuredOutput,
    )
    from code_slayer.planning.planner_certification import certify_planner_from_qualification
    from code_slayer.planning.qualification import (
        RuntimeContextProfile,
        run_planner_case_with_correction,
    )

    blobs = tmp_path / "blobs"
    blobs.mkdir()
    profile = RuntimeContextProfile(
        model_tag="devstral:24b",
        effective_context_tokens=8192,
        output_token_budget=1024,
        model_digest="sha256:abc",
        endpoint="http://local:11436/v1",
        runtime_version="0.1.0",
        temperature=0.0,
    )
    planner = FakePlanner(
        [
            PlannerResponse(
                PlannerOutcome.STRUCTURED,
                output=PlannerStructuredOutput(goal="Add the requested read-only endpoint"),
                raw="SECRET-RAW",
            )
        ]
    )
    evidence = run_planner_case_with_correction(
        planner,
        PlannerRequest(original_request="Add a read-only endpoint."),
        qualification_class="C",
        context_profile=profile,
    )
    result = certify_planner_from_qualification(
        db_conn,
        worker_id=registered_worker,
        results=(evidence,),
        early_stopped=False,
        blobs_dir=blobs,
    )
    assert result.ok
    document = read_planner_qualification_evidence(
        db_conn,
        blobs,
        result.certificate.evidence_ref,
        expected_runtime_identity_fingerprint=result.certificate.runtime_identity_fingerprint,
        expected_role_evaluation_fingerprint=result.certificate.role_evaluation_fingerprint,
    )
    assert document["runtime_identity_fingerprint"] == (
        result.certificate.runtime_identity_fingerprint
    )
    assert document["role_evaluation_fingerprint"] == (
        result.certificate.role_evaluation_fingerprint
    )
    assert fingerprint_runtime_identity(document["runtime_identity_spec"]) == (
        document["runtime_identity_fingerprint"]
    )
    assert fingerprint_role_evaluation(document["role_evaluation_spec"]) == (
        document["role_evaluation_fingerprint"]
    )
    assert "SECRET-RAW" not in json.dumps(document)
    tampered = dict(document)
    tampered_eval = dict(document["role_evaluation_spec"])
    tampered_eval["output_token_budget"] = 1
    tampered["role_evaluation_spec"] = tampered_eval
    with pytest.raises(QualificationEvidenceError, match="role_evaluation_fingerprint_mismatch"):
        persist_planner_qualification_evidence(
            ContentStore(db_conn, blobs),
            tampered,
            expected_runtime_identity_fingerprint=result.certificate.runtime_identity_fingerprint,
            expected_role_evaluation_fingerprint=result.certificate.role_evaluation_fingerprint,
        )


def test_historical_v1_evidence_document_remains_readable(db_conn, tmp_path):
    """Existing planner-qualification-evidence-v1 blobs stay verifiable
    under v1 semantics and are never persisted through the v2 writer."""
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    v1_spec = canonical_runtime_config_spec(
        model_tag="qwen3-coder-ctx16k:30b",
        model_digest="sha256:abc",
        endpoint="http://192.168.32.8:11434/v1",
        runtime_version="0.16.1",
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
        effective_context_tokens=16384,
        output_token_budget=4096,
        temperature=0.0,
        tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
    )
    v1_fp = fingerprint_runtime_config(v1_spec)
    document = {
        "spec_version": QUALIFICATION_EVIDENCE_SPEC_VERSION_V1,
        "role": "PLANNER",
        "policy_version": POLICY_VERSION,
        "runtime_config_spec": v1_spec,
        "runtime_config_fingerprint": v1_fp,
        "model_tag": v1_spec["model_tag"],
        "final_classification": "PASS_FIRST_TRY",
        "instance_count": 0,
        "attempt_count": 0,
        "correction_used": False,
        "instances": [],
    }
    store = ContentStore(db_conn, blobs)
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    blob = store.put(
        payload,
        media_type="application/json",
        source_kind=QUALIFICATION_EVIDENCE_KIND,
        exportable=False,
    )
    loaded = read_planner_qualification_evidence(
        db_conn,
        blobs,
        blob.content_hash,
        expected_runtime_config_fingerprint=v1_fp,
    )
    assert loaded["spec_version"] == QUALIFICATION_EVIDENCE_SPEC_VERSION_V1
    assert loaded["runtime_config_fingerprint"] == v1_fp
    with pytest.raises(QualificationEvidenceError, match="unsupported_qualification_evidence_spec"):
        persist_planner_qualification_evidence(
            store,
            document,
            expected_runtime_identity_fingerprint=v1_fp,
            expected_role_evaluation_fingerprint=v1_fp,
        )


def test_generic_role_evaluation_identity_is_not_planner_specific():
    profile = _common()
    planner = _eval_for(profile, role=ProductionRole.PLANNER)
    coder = _eval_for(
        profile,
        role=ProductionRole.CODER,
        policy_version="coder-certification-v1",
    )
    assert planner.runtime_identity_fingerprint == coder.runtime_identity_fingerprint
    assert planner.role_evaluation_fingerprint != coder.role_evaluation_fingerprint
    spec = canonical_role_evaluation_spec(
        role=ProductionRole.REVIEWER,
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        output_token_budget=2048,
        tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
        policy_version="reviewer-certification-v1",
    )
    assert spec["role"] == "REVIEWER"
    assert spec["spec_version"] == "role-evaluation-spec-v1"


# -- adversarial: identities are structurally self-verifying -----------------

_FORGED_SHA256 = "0" * 64


def test_direct_role_evaluation_identity_rejects_mismatched_fingerprint():
    profile = _common()
    genuine = _eval_for(profile)
    with pytest.raises(ValueError, match="role_evaluation_fingerprint"):
        RoleEvaluationIdentity(
            role=genuine.role,
            runtime_identity_fingerprint=genuine.runtime_identity_fingerprint,
            output_token_budget=genuine.output_token_budget,
            tool_choice_enforcement=genuine.tool_choice_enforcement,
            policy_version=genuine.policy_version,
            role_evaluation_fingerprint=_FORGED_SHA256,
        )


def test_role_evaluation_identity_rejects_budget_change_retaining_old_fingerprint():
    profile = _common()
    genuine = _eval_for(profile, output_token_budget=4096)
    with pytest.raises(ValueError, match="role_evaluation_fingerprint"):
        RoleEvaluationIdentity(
            role=genuine.role,
            runtime_identity_fingerprint=genuine.runtime_identity_fingerprint,
            output_token_budget=1024,
            tool_choice_enforcement=genuine.tool_choice_enforcement,
            policy_version=genuine.policy_version,
            role_evaluation_fingerprint=genuine.role_evaluation_fingerprint,
        )


def test_role_evaluation_identity_rejects_tool_choice_change_retaining_old_fingerprint():
    profile = _common()
    genuine = _eval_for(profile, tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED")
    with pytest.raises(ValueError, match="role_evaluation_fingerprint"):
        RoleEvaluationIdentity(
            role=genuine.role,
            runtime_identity_fingerprint=genuine.runtime_identity_fingerprint,
            output_token_budget=genuine.output_token_budget,
            tool_choice_enforcement="REQUIRED_STRUCTURED_TOOL",
            policy_version=genuine.policy_version,
            role_evaluation_fingerprint=genuine.role_evaluation_fingerprint,
        )


def test_current_runtime_identity_cannot_be_forged_with_arbitrary_fingerprint(
    db_conn,
    registered_worker,
):
    profile = _common()
    forged = RuntimeProfileIdentity(
        model_tag=profile.model_tag,
        model_digest=profile.model_digest,
        endpoint=profile.endpoint,
        runtime_version=profile.runtime_version,
        normalizer_id=profile.normalizer_id,
        normalizer_version=profile.normalizer_version,
        runtime_identity_fingerprint=_FORGED_SHA256,
    )
    assert not forged.is_verified_current
    assert not forged.is_fully_specified
    recorded = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=forged,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="forged-sec",
        reason="ok",
    )
    assert not recorded.ok
    assert recorded.reason == "unverified_runtime_identity"
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []
    decision = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=forged,
        role_evaluation=_eval_for(profile),
        expected_role_policy_version=POLICY_VERSION,
    )
    assert decision.reason == "insufficient_runtime_profile_identity"
    with pytest.raises(ValueError, match="runtime_identity_fingerprint"):
        RuntimeProfileIdentity(
            model_tag=profile.model_tag,
            model_digest=profile.model_digest,
            endpoint=profile.endpoint,
            runtime_version=profile.runtime_version,
            normalizer_id=profile.normalizer_id,
            normalizer_version=profile.normalizer_version,
            runtime_identity_fingerprint=_FORGED_SHA256,
            effective_context_tokens=16384,
            temperature=0.0,
        )


def test_context_tokens_change_retaining_old_fingerprint_cannot_pass_eligibility(
    db_conn,
    registered_worker,
):
    profile = _common(effective_context_tokens=16384)
    _security_pass(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, profile, _eval_for(profile))
    with pytest.raises(ValueError, match="runtime_identity_fingerprint"):
        RuntimeProfileIdentity(
            model_tag=profile.model_tag,
            model_digest=profile.model_digest,
            endpoint=profile.endpoint,
            runtime_version=profile.runtime_version,
            normalizer_id=profile.normalizer_id,
            normalizer_version=profile.normalizer_version,
            runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
            effective_context_tokens=8192,
            temperature=0.0,
        )
    smaller = _common(effective_context_tokens=8192)
    decision = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=smaller,
        role_evaluation=_eval_for(smaller),
        expected_role_policy_version=POLICY_VERSION,
    )
    assert decision.reason == "baseline_security_certificate_profile_mismatch"


def test_temperature_change_retaining_old_fingerprint_cannot_pass_eligibility(
    db_conn,
    registered_worker,
):
    profile = _common(temperature=0.0)
    _security_pass(db_conn, registered_worker, profile)
    _role_pass(db_conn, registered_worker, profile, _eval_for(profile))
    with pytest.raises(ValueError, match="runtime_identity_fingerprint"):
        RuntimeProfileIdentity(
            model_tag=profile.model_tag,
            model_digest=profile.model_digest,
            endpoint=profile.endpoint,
            runtime_version=profile.runtime_version,
            normalizer_id=profile.normalizer_id,
            normalizer_version=profile.normalizer_version,
            runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
            effective_context_tokens=16384,
            temperature=1.5,
        )
    hotter = _common(temperature=1.5)
    decision = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=hotter,
        role_evaluation=_eval_for(hotter),
        expected_role_policy_version=POLICY_VERSION,
    )
    assert decision.reason == "baseline_security_certificate_profile_mismatch"


def test_persisted_certificate_reconstruction_is_not_a_current_identity(
    db_conn,
    registered_worker,
):
    profile = _common()
    evaluation = _eval_for(profile)
    security = _security_pass(db_conn, registered_worker, profile)
    role = _role_pass(db_conn, registered_worker, profile, evaluation)
    stored = runtime_profile_binding_from_stored(
        model_tag=security.certificate.model_tag,
        model_digest=security.certificate.model_digest,
        endpoint=security.certificate.endpoint,
        runtime_version=security.certificate.runtime_version,
        normalizer_id=security.certificate.normalizer_id,
        normalizer_version=security.certificate.normalizer_version,
        runtime_config_fingerprint=security.certificate.runtime_config_fingerprint,
        runtime_identity_fingerprint=security.certificate.runtime_identity_fingerprint,
    )
    assert stored.matches(profile)
    assert not stored.is_verified_current
    assert not stored.is_fully_specified
    reconstructed_role = record_role_certificate(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=stored,
        policy_version=POLICY_VERSION,
        outcome=RoleQualificationOutcome.PASS,
        classification="PASS_FIRST_TRY",
        evidence_ref="recon-role",
        reason="ok",
        role_evaluation=evaluation,
    )
    assert not reconstructed_role.ok
    assert reconstructed_role.reason == "unverified_runtime_identity"
    current_decision = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=stored,
        role_evaluation=evaluation,
        expected_role_policy_version=POLICY_VERSION,
    )
    assert current_decision.reason == "insufficient_runtime_profile_identity"
    live = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=profile,
        role_evaluation=evaluation,
        expected_role_policy_version=POLICY_VERSION,
    )
    assert live == EligibilityDecision(
        True,
        "eligible",
        security_certificate_id=security.certificate.certificate_id,
        role_certificate_id=role.certificate.certificate_id,
    )


def test_matching_direct_role_evaluation_construction_is_accepted():
    profile = _common()
    genuine = _eval_for(profile)
    rebuilt = RoleEvaluationIdentity(
        role=genuine.role,
        runtime_identity_fingerprint=genuine.runtime_identity_fingerprint,
        output_token_budget=genuine.output_token_budget,
        tool_choice_enforcement=genuine.tool_choice_enforcement,
        policy_version=genuine.policy_version,
        role_evaluation_fingerprint=genuine.role_evaluation_fingerprint,
    )
    assert rebuilt.matches(genuine)
    assert rebuilt.is_fully_specified
