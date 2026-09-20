"""Production entry integration: real certificate gates and real isolated pipeline."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from code_slayer.coding import pipeline as pipeline_module
from code_slayer.coding import routing
from code_slayer.coding.pipeline import run_coding_job
from code_slayer.repo import identity
from code_slayer.store.db import utcnow_iso
from code_slayer.workers.engineering_roles import ENGINEERING_ROLES
from code_slayer.workers.lifecycle import archive_worker
from code_slayer.workers.protocol import WorkerAdapterError
from code_slayer.workers.role_qualification import ProductionRole
from tests.integration import test_coding_pipeline as pipeline_tests
from tests.integration.test_coding_pipeline import (
    _CODER_FINAL_REPORT,
    _REVIEWER_PASS,
    _SECURITY_PASS,
    _ready_plan,
    _task_state,
    _text,
    _tool_call,
)
from tests.unit import test_engineering_role_certification as certification_tests
from tests.unit.test_engineering_role_certification import certify


@pytest.fixture
def certification_setup(db_conn, tmp_path, monkeypatch):
    return certification_tests.setup.__wrapped__(db_conn, tmp_path, monkeypatch)


@pytest.fixture
def ready_repo(git_repo_with_commit):
    return pipeline_tests.ready_repo.__wrapped__(git_repo_with_commit)


def _run_certified_job(
    certification_setup,
    ready_repo,
    monkeypatch,
    queues,
    *,
    on_infer=None,
    config_box=None,
    arm_final_revalidate=None,
):
    conn, blobs, config = certification_setup
    for role in ENGINEERING_ROLES:
        certify(certification_setup, role, now=utcnow_iso())
    info = identity.resolve(ready_repo)
    plan = _ready_plan(conn, blobs, repo_id=info.repo_id, worktree_id=info.worktree_id)
    holder = config_box if config_box is not None else {"config": config}
    holder["config"] = holder.get("config", config)
    calls = []

    class Transport:
        def __init__(self, target):
            self.target = target

        def infer(self, request):
            if on_infer is not None:
                on_infer(self.target, request)
            calls.append((self.target.worker_id, request.role))
            if self.target.role == ProductionRole.REPAIRER and not queues.get(
                ProductionRole.REPAIRER
            ):
                if request.prior_tool_result is None:
                    return _tool_call("read_file", {"path": "docs/HELLO.md"})
                if request.prior_tool_result.tool == "read_file":
                    data = json.loads(request.prior_tool_result.output_summary)
                    assert data["content"] == "hello"
                    assert data["expected_hash"]
                    assert data["truncated"] is False
                    return _tool_call(
                        "write_file",
                        {
                            "path": "docs/HELLO.md",
                            "content": "hello\n",
                            "expected_hash": data["expected_hash"],
                        },
                    )
                return _text(_CODER_FINAL_REPORT)
            return queues[self.target.role].pop(0)

    monkeypatch.setattr(routing, "configured_adapter", Transport)
    if arm_final_revalidate is not None:
        original_vci = pipeline_module.verify_candidate_identity
        original_revalidate = routing.EngineeringRoleRouter.revalidate

        def wrapped_vci(*args, **kwargs):
            result = original_vci(*args, **kwargs)
            arm_final_revalidate["armed"] = True
            return result

        def revalidate(self, target, binding):
            if arm_final_revalidate.get("armed"):
                raise routing.RoleRoutingError("simulated_final_revalidation_failure")
            return original_revalidate(self, target, binding)

        monkeypatch.setattr(pipeline_module, "verify_candidate_identity", wrapped_vci)
        monkeypatch.setattr(routing.EngineeringRoleRouter, "revalidate", revalidate)

    result = run_coding_job(
        ready_repo,
        control_conn=conn,
        control_blobs_dir=blobs,
        plan=plan,
        original_prompt="Add docs/HELLO.md",
        allowed_scope=("docs/HELLO.md",),
        config_loader=lambda: holder["config"],
    )
    return result, calls


def _happy_queues(*, repair=False):
    finding = {
        "verdict": "CHANGES_REQUIRED",
        "summary": "add newline",
        "findings": [
            {"path": "docs/HELLO.md", "severity": "minor", "description": "missing newline"},
        ],
    }
    return {
        ProductionRole.CODER: [
            _tool_call("create_file", {"path": "docs/HELLO.md", "content": "hello"}),
            _text(_CODER_FINAL_REPORT),
        ],
        ProductionRole.REPAIRER: [],
        ProductionRole.REVIEWER: ([_text(finding)] if repair else []) + [_text(_REVIEWER_PASS)],
        ProductionRole.SECURITY: [_text(_SECURITY_PASS)],
    }


@pytest.mark.parametrize("repair", [False, True])
def test_production_pipeline_uses_certified_roles_and_separate_repairer(
    certification_setup,
    ready_repo,
    monkeypatch,
    repair,
):
    result, calls = _run_certified_job(
        certification_setup, ready_repo, monkeypatch, _happy_queues(repair=repair),
    )
    assert result.final_state == "READY_FOR_HUMAN_MERGE", result.reason
    assert result.repair_attempts == int(repair)
    assert (("repairer", "repairer") in calls) == repair
    assert ("coder", "reviewer") not in calls and ("coder", "security") not in calls
    assert Path(result.job_worktree_path, "docs/HELLO.md").read_text() == (
        "hello\n" if repair else "hello"
    )
    conn = certification_setup[0]
    events = conn.execute("SELECT payload_json FROM audit_events WHERE event_type='WORKER_STARTED'")
    binding = next(json.loads(row[0])["role_bindings"] for row in events)
    assert set(binding) == {r.value for r in ENGINEERING_ROLES}
    assert binding["REPAIRER"]["worker_id"] == "repairer"
    assert all(b["security_certificate_id"] and b["role_certificate_id"] for b in binding.values())


def test_revocation_during_inference_contains_tool_response_before_mutation(
    certification_setup,
    ready_repo,
    monkeypatch,
):
    conn, blobs, config = certification_setup

    def on_infer(target, request):
        if request.role == "coder":
            archive_worker(conn, worker_id="coder")

    result, calls = _run_certified_job(
        certification_setup, ready_repo, monkeypatch, _happy_queues(), on_infer=on_infer,
    )
    assert result.final_state == "FAILED"
    assert "worker_archived" in result.reason
    assert not Path(result.job_worktree_path, "docs/HELLO.md").exists()
    assert ("repairer", "repairer") not in calls


def test_final_role_revalidation_failure_fails_task_not_checkpoint_eligible(
    certification_setup, ready_repo, monkeypatch,
):
    result, _ = _run_certified_job(
        certification_setup, ready_repo, monkeypatch, _happy_queues(),
        arm_final_revalidate={},
    )
    assert result.final_state == "BLOCKED"
    assert result.reason == "simulated_final_revalidation_failure"
    assert result.review is not None and result.security is not None
    assert result.repair_attempts == 0
    assert _task_state(result) == "FAILED"
    assert _task_state(result) != "READY_FOR_CHECKPOINT"


def test_reviewer_archived_during_inference_never_invokes_repairer(
    certification_setup, ready_repo, monkeypatch,
):
    conn = certification_setup[0]

    def on_infer(target, request):
        if request.role == "reviewer":
            archive_worker(conn, worker_id="reviewer")

    result, calls = _run_certified_job(
        certification_setup, ready_repo, monkeypatch, _happy_queues(), on_infer=on_infer,
    )
    assert result.final_state == "BLOCKED"
    assert "worker_archived" in result.reason
    assert ("repairer", "repairer") not in calls
    assert _task_state(result) == "FAILED"


def test_reviewer_certificate_superseded_during_inference_never_invokes_repairer(
    certification_setup, ready_repo, monkeypatch,
):
    def on_infer(target, request):
        if request.role == "reviewer":
            certify(certification_setup, ProductionRole.REVIEWER, now=utcnow_iso())

    result, calls = _run_certified_job(
        certification_setup, ready_repo, monkeypatch, _happy_queues(), on_infer=on_infer,
    )
    assert result.final_state == "BLOCKED"
    assert result.reason == "role_route_binding_stale"
    assert ("repairer", "repairer") not in calls
    assert _task_state(result) == "FAILED"


def test_reviewer_runtime_config_change_during_inference_never_invokes_repairer(
    certification_setup, ready_repo, monkeypatch,
):
    box = {"config": certification_setup[2]}

    def on_infer(target, request):
        if request.role == "reviewer":
            worker = box["config"].worker_by_id("reviewer")
            box["config"] = box["config"].with_worker(replace(worker, temperature=0.5))

    result, calls = _run_certified_job(
        certification_setup, ready_repo, monkeypatch, _happy_queues(),
        on_infer=on_infer, config_box=box,
    )
    assert result.final_state == "BLOCKED"
    assert result.reason in {"role_route_binding_stale", "role_worker_not_configured"}
    assert ("repairer", "repairer") not in calls
    assert _task_state(result) == "FAILED"


def test_reviewer_transport_failure_never_invokes_repairer(
    certification_setup, ready_repo, monkeypatch,
):
    def on_infer(target, request):
        if request.role == "reviewer":
            raise WorkerAdapterError("simulated_reviewer_transport_failure")

    result, calls = _run_certified_job(
        certification_setup, ready_repo, monkeypatch, _happy_queues(), on_infer=on_infer,
    )
    assert result.final_state == "BLOCKED"
    assert "simulated_reviewer_transport_failure" in result.reason
    assert ("repairer", "repairer") not in calls
    assert _task_state(result) == "FAILED"


def test_malformed_reviewer_output_never_invokes_repairer(
    certification_setup, ready_repo, monkeypatch,
):
    queues = _happy_queues()
    queues[ProductionRole.REVIEWER] = [
        _text({"not": "a reviewer result"}),
    ]
    result, calls = _run_certified_job(
        certification_setup, ready_repo, monkeypatch, queues,
    )
    assert result.final_state == "BLOCKED"
    assert result.reason.startswith("reviewer_unavailable:")
    assert ("repairer", "repairer") not in calls
    assert _task_state(result) == "FAILED"


def test_authorized_reviewer_blocked_verdict_never_invokes_repairer(
    certification_setup, ready_repo, monkeypatch,
):
    queues = _happy_queues()
    queues[ProductionRole.REVIEWER] = [
        _text({"verdict": "BLOCKED", "summary": "do not auto-repair", "findings": []}),
    ]
    result, calls = _run_certified_job(
        certification_setup, ready_repo, monkeypatch, queues,
    )
    assert result.final_state == "BLOCKED"
    assert result.reason == "reviewer_blocked"
    assert ("repairer", "repairer") not in calls
    assert _task_state(result) == "FAILED"


def test_genuine_changes_required_still_invokes_repairer(
    certification_setup, ready_repo, monkeypatch,
):
    result, calls = _run_certified_job(
        certification_setup, ready_repo, monkeypatch, _happy_queues(repair=True),
    )
    assert result.final_state == "READY_FOR_HUMAN_MERGE", result.reason
    assert result.repair_attempts == 1
    assert ("repairer", "repairer") in calls
    assert Path(result.job_worktree_path, "docs/HELLO.md").read_text() == "hello\n"
