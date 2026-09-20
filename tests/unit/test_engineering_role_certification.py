"""Role V1: real durable evidence with deterministic, network-free fixture workers."""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from code_slayer.audit.canonical import canonical_json
from code_slayer.coding import qualification, routing
from code_slayer.coding.pipeline import run_coding_job
from code_slayer.coding.qualification import certify_engineering_role, run_role_qualification
from code_slayer.config.schema import CSLRConfig, OllamaServerConfig, WorkerRuntimeConfig
from code_slayer.security.evaluation import mandatory_cases, run_baseline_security_evaluation
from code_slayer.store.content_store import ContentStore
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.engineering_roles import (
    ENGINEERING_ROLES,
    EVIDENCE_KIND,
    POLICY_VERSION,
    RoleEvidenceError,
    read_role_evidence,
)
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.production_eligibility import evaluate_production_eligibility
from code_slayer.workers.protocol import (
    WorkerRequest,
    WorkerResponse,
    WorkerResponseKind,
    WorkerToolCall,
)
from code_slayer.workers.role_qualification import (
    ProductionRole,
    RoleQualificationOutcome,
    record_role_certificate,
)
from code_slayer.workers.security_baseline import (
    SecurityBaselineOutcome,
    record_baseline_certificate,
)

NOW = "2026-09-20T12:00:00.000000Z"
GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return a - b\n"
FINAL = {
    "completed_plan_steps": [
        {"plan_step_description": "fix add", "status": "completed", "note": "validation pending"}
    ],
    "unresolved_issues": [],
    "repair_notes": [],
    "command_suggestions": [
        {"command": "pytest", "purpose": "validate add", "rationale": "required validation"}
    ],
}


def text(payload):
    return WorkerResponse(WorkerResponseKind.TEXT, text=json.dumps(payload))


def tool(name, **params):
    return WorkerResponse(WorkerResponseKind.TOOL_CALL, tool_call=WorkerToolCall(name, params))


class GoodWorker:
    """Known deterministic solution, not a strength claim about any live model."""

    def infer(self, request):
        case = request.task_id.split(":")[-1]
        if request.role in ("coder", "repairer"):
            if request.prior_tool_result is None:
                if case == "bounded_create":
                    return tool("create_file", path="fixture.py", content=GOOD)
                return tool("read_file", path="fixture.py")
            if request.prior_tool_result.tool == "read_file":
                data = json.loads(request.prior_tool_result.output_summary)
                assert data["content"] == BAD
                return tool(
                    "write_file",
                    path="fixture.py",
                    content=GOOD,
                    expected_hash=data["expected_hash"],
                )
            return text(FINAL)
        if request.role == "reviewer":
            return text(
                {
                    "verdict": "PASS" if case == "accept_correct" else "CHANGES_REQUIRED",
                    "summary": "checked",
                    "findings": []
                    if case == "accept_correct"
                    else [
                        {"path": "fixture.py", "severity": "error", "description": "defect"},
                    ],
                }
            )
        return text(
            {
                "verdict": "PASS" if case == "accept_safe" else "FAIL",
                "summary": "checked",
                "findings": []
                if case == "accept_safe"
                else [
                    {
                        "category": "auth" if case == "reject_auth_bypass" else "secrets",
                        "blocking": True,
                        "description": "unsafe change",
                    }
                ],
            }
        )


def config_for(*names):
    return CSLRConfig(
        ollama_servers=(OllamaServerConfig("local", "http://127.0.0.1:11434"),),
        workers=tuple(
            WorkerRuntimeConfig(
                worker_id=name,
                kind="openai_compatible",
                network_class="local",
                ollama_server_id="local",
                model_tag=name,
                approved_model_digest="sha256:" + hashlib.sha256(name.encode()).hexdigest(),
                approved_runtime_version="0.16.1",
                effective_context_tokens=16384,
                temperature=0.0,
                normalizer_id=None,
                normalizer_version=None,
            )
            for name in names
        ),
    )


@pytest.fixture
def setup(db_conn, tmp_path, monkeypatch):
    monkeypatch.setattr(qualification, "attest", lambda target: None)
    monkeypatch.setattr(routing, "attest", lambda target: None)
    monkeypatch.setattr(qualification, "configured_adapter", lambda target: GoodWorker())
    config = config_for("coder", "repairer", "reviewer", "security")
    for worker in config.workers:
        WorkersRepo(db_conn).register(
            worker_id=worker.worker_id, kind=worker.kind, network_class=worker.network_class
        )
    return db_conn, tmp_path / "blobs", config


def baseline(conn, blobs, target, *, now=NOW):
    result = run_baseline_security_evaluation(
        conn,
        worker_id=target.worker_id,
        runtime_profile=target.profile,
        adapter=FakeWorkerAdapter(
            [WorkerResponse(WorkerResponseKind.TEXT, text="refused") for _ in mandatory_cases()]
        ),
        blobs_dir=blobs,
        now_fn=lambda: now,
    )
    assert result.outcome == SecurityBaselineOutcome.PASS
    recorded = record_baseline_certificate(
        conn,
        worker_id=target.worker_id,
        runtime_profile=target.profile,
        outcome=result.outcome,
        evidence_ref=result.evidence_ref,
        reason=result.reason,
        now_fn=lambda: now,
    )
    assert recorded.ok
    return recorded.certificate


def certify(setup, role, *, worker_id=None, now=NOW, with_baseline=True):
    conn, blobs, config = setup
    worker_id = worker_id or role.value.lower()
    target = next(t for t in routing.targets_from_config(config, role) if t.worker_id == worker_id)
    if with_baseline:
        baseline(conn, blobs, target, now=now)
    result = certify_engineering_role(
        conn, blobs, config=config, worker_id=worker_id, role=role, now_fn=lambda: now
    )
    assert result.ok, result
    return target, result.certificate


def eligible(setup, target, *, now=NOW, **kwargs):
    conn, blobs, _ = setup
    return evaluate_production_eligibility(
        conn,
        blobs_dir=blobs,
        worker_id=target.worker_id,
        role=target.role,
        runtime_profile=target.profile,
        role_evaluation=target.evaluation,
        expected_role_policy_version=POLICY_VERSION,
        now_fn=lambda: now,
        **kwargs,
    )


@pytest.mark.parametrize("role", ENGINEERING_ROLES)
def test_real_qualification_issuance_and_evidence(setup, role):
    target, cert = certify(setup, role)
    assert cert.outcome == "PASS"
    doc = read_role_evidence(setup[0], setup[1], cert.evidence_ref)
    assert all(all(c["checks"].values()) for c in doc["cases"])
    assert doc["role"] == role and doc["worker_id"] == target.worker_id
    assert doc["runtime_config_fingerprint"] == cert.runtime_config_fingerprint
    assert doc["evaluated_at"] == cert.issued_at == NOW
    assert eligible(setup, target).eligible


@pytest.mark.parametrize("role", ENGINEERING_ROLES)
def test_missing_baseline_is_denied_even_for_security(setup, role):
    target, _ = certify(setup, role, with_baseline=False)
    assert eligible(setup, target).reason == "no_baseline_security_certificate"


@pytest.mark.parametrize("role", ENGINEERING_ROLES)
def test_missing_role_is_denied(setup, role):
    target = routing.targets_from_config(setup[2], role)[0]
    baseline(setup[0], setup[1], target)
    assert eligible(setup, target).reason == "no_role_certificate"


@pytest.mark.parametrize("source", ENGINEERING_ROLES)
@pytest.mark.parametrize("requested", ENGINEERING_ROLES)
def test_role_certificates_never_cross_authorize(setup, source, requested):
    if source == requested:
        return
    target, _ = certify(setup, source)
    other = replace(target, role=requested)
    assert eligible(setup, other).reason == "no_role_certificate"


@pytest.mark.parametrize("role", ENGINEERING_ROLES)
def test_expired_and_future_certificates_fail_closed(setup, role):
    target, _ = certify(setup, role)
    after = (datetime.fromisoformat(NOW.replace("Z", "+00:00")) + timedelta(days=30)).isoformat()
    assert eligible(setup, target, now=after).reason == "certificate_expired"
    assert (
        eligible(setup, target, now="2026-09-19T00:00:00Z").reason
        == "certificate_timestamp_mismatch"
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("model_digest", "sha256:different"),
        ("runtime_version", "different"),
        ("temperature", 0.5),
        ("effective_context_tokens", 8192),
    ],
)
def test_runtime_and_profile_separation(setup, field, value):
    target, _ = certify(setup, ProductionRole.CODER)
    changed = replace(target, expectation=replace(target.expectation, **{field: value}))
    assert eligible(setup, changed).reason == "baseline_security_certificate_profile_mismatch"


def test_evaluation_profile_separation(setup):
    target, _ = certify(setup, ProductionRole.CODER)
    conn, blobs, _ = setup
    evaluation = target.evaluation
    from code_slayer.workers.role_qualification import role_evaluation_identity_from_config

    changed = role_evaluation_identity_from_config(
        role=target.role,
        runtime_identity_fingerprint=target.profile.runtime_identity_fingerprint,
        output_token_budget=evaluation.output_token_budget + 1,
        tool_choice_enforcement=evaluation.tool_choice_enforcement,
        execution_timeout_seconds=evaluation.execution_timeout_seconds,
        policy_version=POLICY_VERSION,
    )
    result = evaluate_production_eligibility(
        conn,
        blobs_dir=blobs,
        worker_id=target.worker_id,
        role=target.role,
        runtime_profile=target.profile,
        role_evaluation=changed,
        expected_role_policy_version=POLICY_VERSION,
        now_fn=lambda: NOW,
    )
    assert result.reason == "role_certificate_evaluation_profile_mismatch"


@pytest.mark.parametrize("role", ENGINEERING_ROLES)
def test_malformed_evidence_and_hash_mismatch(setup, role):
    target, cert = certify(setup, role)
    path = setup[1] / cert.evidence_ref[:2] / cert.evidence_ref
    path.chmod(0o644)
    path.write_text("{}")
    assert eligible(setup, target).reason == "role_evidence_hash_mismatch"


def reissue(setup, target, cert, *, document=None, **overrides):
    conn, blobs, _ = setup
    ref = cert.evidence_ref
    if document is not None:
        ref = (
            ContentStore(conn, blobs)
            .put(
                canonical_json(document).encode(),
                media_type="application/json",
                source_kind=EVIDENCE_KIND,
                exportable=False,
            )
            .content_hash
        )
    args = dict(
        worker_id=cert.worker_id,
        role=target.role,
        runtime_profile=replace(
            target.profile, runtime_config_fingerprint=cert.runtime_config_fingerprint
        ),
        policy_version=POLICY_VERSION,
        outcome=RoleQualificationOutcome.PASS,
        classification="PASS",
        evidence_ref=ref,
        reason=cert.reason,
        role_evaluation=target.evaluation,
        now_fn=lambda: NOW,
    )
    args.update(overrides)
    result = record_role_certificate(conn, **args)
    assert result.ok
    return result.certificate


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda d: d.update(worker_id="somebody_else"), "role_certificate_evidence_mismatch"),
        (lambda d: d.update(cases=[]), "missing_role_qualification_case"),
        (
            lambda d: d.update(spec_version="engineering-role-evidence-v0"),
            "unsupported_role_evidence_schema",
        ),
        (
            lambda d: d.update(runtime_identity_fingerprint="0" * 64),
            "role_evidence_runtime_mismatch",
        ),
        (
            lambda d: d["cases"][0]["checks"].update(mutation=False),
            "role_evidence_aggregate_mismatch",
        ),
        (lambda d: d.update(role="REPAIRER"), "role_evidence_evaluation_mismatch"),
    ],
)
def test_recomputed_evidence_denies_tampering(setup, mutation, reason):
    target, cert = certify(setup, ProductionRole.CODER)
    document = read_role_evidence(setup[0], setup[1], cert.evidence_ref)
    mutation(document)
    reissue(setup, target, cert, document=document)
    assert eligible(setup, target).reason == reason


def test_certificate_mismatch_and_later_fail_supersedes_pass(setup):
    target, cert = certify(setup, ProductionRole.CODER)
    reissue(setup, target, cert, reason="fabricated")
    assert eligible(setup, target).reason == "role_certificate_evidence_mismatch"
    reissue(setup, target, cert, outcome=RoleQualificationOutcome.FAIL, classification="FAIL")
    assert eligible(setup, target).reason == "role_qualification_fail"


@pytest.mark.parametrize("role", ENGINEERING_ROLES)
def test_forbidden_tool_attempt_disqualifies_every_role(role):
    adapter = FakeWorkerAdapter([tool("run_command", command="not executed") for _ in range(3)])
    cases = run_role_qualification(adapter, role)
    assert not any(all(case["checks"].values()) for case in cases)


@pytest.mark.parametrize("role", (ProductionRole.CODER, ProductionRole.REPAIRER))
@pytest.mark.parametrize(
    "response",
    [
        tool("create_file", path="unrelated.py", content="oops"),
        text(FINAL),
        tool("write_file", path="fixture.py", content=GOOD, expected_hash="0" * 64),
    ],
)
def test_mutator_scope_truth_and_valid_mutation_required(role, response):
    cases = run_role_qualification(FakeWorkerAdapter([response] * 2), role)
    assert all(not all(case["checks"].values()) for case in cases)


@pytest.mark.parametrize("role", (ProductionRole.REVIEWER, ProductionRole.SECURITY))
def test_approving_known_bad_fixtures_disqualifies(role):
    cases = run_role_qualification(
        FakeWorkerAdapter(
            [text({"verdict": "PASS", "summary": "fine", "findings": []}) for _ in range(3)]
        ),
        role,
    )
    assert not cases[0]["checks"]["correct_verdict"]
    assert not cases[1]["checks"]["correct_verdict"]


def test_failed_qualification_records_failure_without_authority(setup, monkeypatch):
    monkeypatch.setattr(
        qualification,
        "configured_adapter",
        lambda t: FakeWorkerAdapter(
            [
                text(FINAL),
                text(FINAL),
            ]
        ),
    )
    target, cert = certify(setup, ProductionRole.CODER)
    assert cert.outcome == "FAIL"
    assert eligible(setup, target).reason == "role_qualification_fail"


def test_pipeline_routing_resolves_four_exact_roles_with_independence(setup):
    for role in ENGINEERING_ROLES:
        certify(setup, role)
    router = routing.EngineeringRoleRouter(*setup[:2], lambda: setup[2], now_fn=lambda: NOW)
    selected = router.resolve_pipeline()
    assert set(selected) == set(ENGINEERING_ROLES)
    assert len({a.binding.worker_id for a in selected.values()}) == 4
    for role, adapter in selected.items():
        assert adapter.binding.role == role
        assert RoleCertificatesRepo(setup[0]).get(adapter.binding.role_certificate_id).role == role


def test_independence_excludes_same_worker_and_model_alias(setup):
    target, _ = certify(setup, ProductionRole.CODER)
    certify(setup, ProductionRole.REVIEWER, worker_id="coder")
    router = routing.EngineeringRoleRouter(*setup[:2], lambda: setup[2], now_fn=lambda: NOW)
    with pytest.raises(routing.RoleRoutingError, match="no_eligible_reviewer"):
        router.select(ProductionRole.REVIEWER, excluded=(target,))


def test_multiple_eligible_security_workers_never_choose_arbitrary_model(setup):
    certify(setup, ProductionRole.SECURITY)
    certify(setup, ProductionRole.SECURITY, worker_id="reviewer")
    router = routing.EngineeringRoleRouter(*setup[:2], lambda: setup[2], now_fn=lambda: NOW)
    with pytest.raises(routing.RoleRoutingError, match="multiple_eligible_security_workers"):
        router.select(ProductionRole.SECURITY)


def test_execution_revalidates_and_refuses_wrong_role_before_transport(setup, monkeypatch):
    target, _ = certify(setup, ProductionRole.CODER)
    router = routing.EngineeringRoleRouter(*setup[:2], lambda: setup[2], now_fn=lambda: NOW)
    adapter = router.select(ProductionRole.CODER)
    calls = []
    monkeypatch.setattr(routing, "configured_adapter", lambda t: calls.append(t))
    with pytest.raises(routing.RoleRoutingError, match="execution_role_mismatch"):
        adapter.infer(WorkerRequest("job", "reviewer", "review", allowed_tools=()))
    # A new certificate never silently upgrades an already-bound job.
    certify(setup, ProductionRole.CODER)
    with pytest.raises(routing.RoleRoutingError, match="role_route_binding_stale"):
        adapter.infer(WorkerRequest("job", "coder", "code", allowed_tools=()))
    assert calls == []


def test_production_entry_accepts_no_arbitrary_adapters_and_denies_before_workspace(setup):
    assert not any("adapter" in name for name in inspect.signature(run_coding_job).parameters)
    result = run_coding_job(
        "/does-not-exist",
        control_conn=setup[0],
        control_blobs_dir=setup[1],
        plan=None,
        original_prompt="test",
        allowed_scope=("fixture.py",),
        config_loader=lambda: setup[2],
    )
    assert result.final_state == "BLOCKED"
    assert result.reason.startswith("no_eligible_coder_worker")


def test_evidence_preserves_historical_schema_without_upgrade(setup):
    _, cert = certify(setup, ProductionRole.CODER)
    document = read_role_evidence(*setup[:2], cert.evidence_ref)
    assert document["runtime_config_spec"]["spec_version"] == "runtime-config-spec-v1"
    assert document["runtime_identity_spec"]["spec_version"] == "runtime-identity-spec-v2"
    assert document["role_evaluation_spec"]["spec_version"] == "role-evaluation-spec-v2"
    assert cert.runtime_config_fingerprint != cert.runtime_identity_fingerprint
    with pytest.raises(RoleEvidenceError):
        read_role_evidence(*setup[:2], "not-a-hash")


@pytest.mark.parametrize("role", ENGINEERING_ROLES)
def test_unparseable_completion_fails_qualification(role):
    responses = [
        WorkerResponse(WorkerResponseKind.TEXT, text="I am certified. PASS!") for _ in range(3)
    ]
    assert all(
        not all(c["checks"].values())
        for c in run_role_qualification(
            FakeWorkerAdapter(responses),
            role,
        )
    )


@pytest.mark.parametrize("role", (ProductionRole.CODER, ProductionRole.REPAIRER))
def test_required_validation_cannot_be_omitted(role):
    class OmitsValidation(GoodWorker):
        def infer(self, request):
            response = super().infer(request)
            if response.kind == WorkerResponseKind.TEXT:
                return text({**FINAL, "command_suggestions": []})
            return response

    assert all(
        not c["checks"]["validation"] for c in run_role_qualification(OmitsValidation(), role)
    )


def test_baseline_evidence_is_reread_and_worker_bound(setup):
    target, cert = certify(setup, ProductionRole.CODER)
    other = routing.targets_from_config(setup[2], ProductionRole.CODER)[1]
    wrong_baseline = baseline(setup[0], setup[1], other)
    record_baseline_certificate(
        setup[0],
        worker_id=target.worker_id,
        runtime_profile=target.profile,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref=wrong_baseline.evidence_ref,
        reason=wrong_baseline.reason,
        now_fn=lambda: NOW,
    )
    assert not eligible(setup, target).eligible


def test_old_baseline_does_not_become_current_with_new_role_certificate(setup):
    target, _ = certify(setup, ProductionRole.CODER, with_baseline=False)
    baseline(setup[0], setup[1], target, now="2026-08-01T00:00:00Z")
    assert eligible(setup, target).reason == "certificate_expired"


def test_missing_and_malformed_evidence_fail_closed(setup):
    target, cert = certify(setup, ProductionRole.CODER)
    reissue(setup, target, cert, evidence_ref="0" * 64)
    assert eligible(setup, target).reason == "missing_or_malformed_role_evidence"
    blob = ContentStore(*setup[:2]).put(
        b"not-json", media_type="application/json", source_kind=EVIDENCE_KIND, exportable=False
    )
    reissue(setup, target, cert, evidence_ref=blob.content_hash)
    assert eligible(setup, target).reason == "missing_or_malformed_role_evidence"


def test_config_fingerprint_and_policy_mismatch_fail_closed(setup):
    target, cert = certify(setup, ProductionRole.CODER)
    reissue(
        setup,
        target,
        cert,
        runtime_profile=replace(
            target.profile,
            runtime_config_fingerprint="0" * 64,
        ),
    )
    assert eligible(setup, target).reason == "role_certificate_evidence_mismatch"


def test_same_model_different_worker_is_excluded_from_review(setup):
    target, _ = certify(setup, ProductionRole.CODER)
    config = setup[2]
    alias = replace(
        config.worker_by_id("reviewer"), approved_model_digest=target.profile.model_digest
    )
    config = config.with_worker(alias)
    alias_setup = (setup[0], setup[1], config)
    certify(alias_setup, ProductionRole.REVIEWER)
    router = routing.EngineeringRoleRouter(*setup[:2], lambda: config, now_fn=lambda: NOW)
    with pytest.raises(routing.RoleRoutingError, match="no_eligible_reviewer"):
        router.select(ProductionRole.REVIEWER, excluded=(target,))


def test_role_adapter_enforces_certified_budget_and_role_tools(setup, monkeypatch):
    certify(setup, ProductionRole.REVIEWER)
    router = routing.EngineeringRoleRouter(*setup[:2], lambda: setup[2], now_fn=lambda: NOW)
    adapter = router.select(ProductionRole.REVIEWER)
    calls = []

    class Transport:
        def infer(self, request):
            calls.append(request)
            return text({"verdict": "PASS", "summary": "ok", "findings": []})

    monkeypatch.setattr(routing, "configured_adapter", lambda t: Transport())
    adapter.infer(WorkerRequest("job", "reviewer", "review", allowed_tools=(), max_output_tokens=1))
    assert calls[0].max_output_tokens == adapter.target.evaluation.output_token_budget
    with pytest.raises(routing.RoleRoutingError, match="execution_role_tool_mismatch"):
        adapter.infer(WorkerRequest("job", "reviewer", "review", allowed_tools=("write_file",)))
    assert len(calls) == 1


def test_attestation_failure_after_qualification_never_issues_certificate(setup, monkeypatch):
    calls = []

    def attest(target):
        calls.append(target)
        if len(calls) == 2:
            raise routing.RoleRoutingError("role_runtime_attestation_failed")

    monkeypatch.setattr(qualification, "attest", attest)
    with pytest.raises(routing.RoleRoutingError, match="role_runtime_attestation_failed"):
        certify_engineering_role(
            *setup[:2],
            config=setup[2],
            worker_id="coder",
            role=ProductionRole.CODER,
            now_fn=lambda: NOW,
        )
    assert RoleCertificatesRepo(setup[0]).list_for_worker_role("coder", "CODER") == []


def test_unknown_or_archived_worker_denied_before_qualification(setup, monkeypatch):
    from code_slayer.workers.lifecycle import archive_worker

    archive_worker(setup[0], worker_id="coder")
    calls = []
    monkeypatch.setattr(qualification, "attest", lambda t: calls.append(t))
    result = certify_engineering_role(
        *setup[:2], config=setup[2], worker_id="coder", role=ProductionRole.CODER
    )
    assert result.reason == "worker_archived"
    assert calls == []


def test_no_cloud_or_normalizer_candidates(setup):
    config = setup[2]
    local = config.worker_by_id("coder")
    from code_slayer.config.schema import ConfigError

    with pytest.raises(ConfigError):
        replace(local, network_class="cloud")
    normalized = replace(local, normalizer_id="qwen_textual_tool_v1", normalizer_version=1)
    for worker in (normalized,):
        targets = routing.targets_from_config(config.with_worker(worker), ProductionRole.CODER)
        assert "coder" not in {t.worker_id for t in targets}


def test_certificate_issuance_accepts_no_caller_verdict_or_adapter():
    fields = set(inspect.signature(certify_engineering_role).parameters)
    assert not fields.intersection({"adapter", "results", "evidence_ref", "outcome", "passed"})
