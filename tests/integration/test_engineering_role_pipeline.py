"""Production entry integration: real certificate gates and real isolated pipeline."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from code_slayer.coding import routing
from code_slayer.coding.pipeline import run_coding_job
from code_slayer.repo import identity
from code_slayer.store.db import utcnow_iso
from code_slayer.workers.engineering_roles import ENGINEERING_ROLES
from code_slayer.workers.lifecycle import archive_worker
from code_slayer.workers.role_qualification import ProductionRole
from tests.integration import test_coding_pipeline as pipeline_tests
from tests.integration.test_coding_pipeline import (
    _CODER_FINAL_REPORT,
    _REVIEWER_PASS,
    _SECURITY_PASS,
    _ready_plan,
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


@pytest.mark.parametrize("repair", [False, True])
def test_production_pipeline_uses_certified_roles_and_separate_repairer(
    certification_setup,
    ready_repo,
    monkeypatch,
    repair,
):
    conn, blobs, config = certification_setup
    for role in ENGINEERING_ROLES:
        certify(certification_setup, role, now=utcnow_iso())
    info = identity.resolve(ready_repo)
    plan = _ready_plan(conn, blobs, repo_id=info.repo_id, worktree_id=info.worktree_id)
    finding = {
        "verdict": "CHANGES_REQUIRED",
        "summary": "add newline",
        "findings": [
            {"path": "docs/HELLO.md", "severity": "minor", "description": "missing newline"},
        ],
    }
    queues = {
        ProductionRole.CODER: [
            _tool_call("create_file", {"path": "docs/HELLO.md", "content": "hello"}),
            _text(_CODER_FINAL_REPORT),
        ],
        ProductionRole.REPAIRER: [
            _tool_call(
                "write_file",
                {
                    "path": "docs/HELLO.md",
                    "content": "hello\n",
                    "expected_hash": hashlib.sha256(b"hello").hexdigest(),
                },
            ),
            _text(_CODER_FINAL_REPORT),
        ],
        ProductionRole.REVIEWER: ([_text(finding)] if repair else []) + [_text(_REVIEWER_PASS)],
        ProductionRole.SECURITY: [_text(_SECURITY_PASS)],
    }
    calls = []

    class Transport:
        def __init__(self, target):
            self.target = target

        def infer(self, request):
            assert request.role == self.target.role.value.lower()
            assert request.max_output_tokens == self.target.evaluation.output_token_budget
            if request.role == "repairer":
                assert "missing newline" in request.original_prompt
                assert "docs/HELLO.md" in request.original_prompt
            calls.append((self.target.worker_id, request.role))
            return queues[self.target.role].pop(0)

    monkeypatch.setattr(routing, "configured_adapter", Transport)
    result = run_coding_job(
        ready_repo,
        control_conn=conn,
        control_blobs_dir=blobs,
        plan=plan,
        original_prompt="Add docs/HELLO.md",
        allowed_scope=("docs/HELLO.md",),
        config_loader=lambda: config,
    )
    assert result.final_state == "READY_FOR_HUMAN_MERGE", result.reason
    assert result.repair_attempts == int(repair)
    assert (("repairer", "repairer") in calls) == repair
    assert ("coder", "reviewer") not in calls and ("coder", "security") not in calls
    assert Path(result.job_worktree_path, "docs/HELLO.md").read_text() == (
        "hello\n" if repair else "hello"
    )
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
    for role in ENGINEERING_ROLES:
        certify(certification_setup, role, now=utcnow_iso())
    info = identity.resolve(ready_repo)
    plan = _ready_plan(conn, blobs, repo_id=info.repo_id, worktree_id=info.worktree_id)

    class Transport:
        def infer(self, request):
            archive_worker(conn, worker_id="coder")
            return _tool_call("create_file", {"path": "docs/HELLO.md", "content": "forbidden"})

    monkeypatch.setattr(routing, "configured_adapter", lambda t: Transport())
    result = run_coding_job(
        ready_repo,
        control_conn=conn,
        control_blobs_dir=blobs,
        plan=plan,
        original_prompt="Add docs/HELLO.md",
        allowed_scope=("docs/HELLO.md",),
        config_loader=lambda: config,
    )
    assert result.final_state == "FAILED"
    assert "worker_archived" in result.reason
    assert not Path(result.job_worktree_path, "docs/HELLO.md").exists()
