"""End-to-end `coding.pipeline.run_coding_job()` behavior against real
temporary Git repositories, a real SQLite state database, a real isolated
job worktree, and real (fast, deterministic) subprocess verification --
driven by a scripted fake `WorkerAdapter`, never a live model.

Covers the full pipeline wiring this task's spec asks for: Planner->Coder
handoff validation (insufficient plan fails closed with
`insufficient_coder_scope`), a real bounded Coder mutation reaching
`READY_FOR_HUMAN_MERGE` via Reviewer PASS + Security PASS, a Reviewer
`CHANGES_REQUIRED` verdict driving exactly one bounded repair round, a
Security `FAIL` verdict blocking readiness even after Reviewer PASS and
green verification, the bounded repair budget being exhausted into
`HUMAN_REQUIRED`, an out-of-scope mutation attempt being denied by real
`PolicyEngine` scope enforcement (never reaching disk), and the primary
checkout remaining completely untouched throughout.
"""

from __future__ import annotations

import json
import os

import pytest

from code_slayer.coding.pipeline import CodingJobConfig, run_coding_job
from code_slayer.coding.pipeline_types import CodingJobState
from code_slayer.planning.models import EngineeringPlanContent, PlannedChange
from code_slayer.planning.provenance import store_plan_content
from code_slayer.repo import identity
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.planning_repo import PlanningRepo
from code_slayer.tools import file_tools as files
from code_slayer.workers.protocol import (
    WorkerAdapterError,
    WorkerResponse,
    WorkerResponseKind,
    WorkerToolCall,
)
from tests.repo_helpers import commit, filesystem_snapshot, git


class ScriptedAdapter:
    """A fake `WorkerAdapter`: a fixed, per-role queue of canned
    `WorkerResponse`s, popped in order. Raises `WorkerAdapterError` (a
    real transport-failure shape, never a silent empty response) if a
    role asks for more turns than were scripted -- a test bug, not a
    pipeline bug, should surface loudly."""

    def __init__(self, by_role: dict[str, list[WorkerResponse]]):
        self._queues = {role: list(responses) for role, responses in by_role.items()}
        self.calls: list[str] = []

    def infer(self, request):
        self.calls.append(request.role)
        queue = self._queues.get(request.role)
        if not queue:
            raise WorkerAdapterError(f"no_scripted_response_for_role:{request.role}")
        return queue.pop(0)


def _working_tree_snapshot(root):
    """`filesystem_snapshot`, excluding `.git/` internals -- mirrors
    `tests.integration.test_job_worktree`'s own `working_tree_snapshot()`
    exactly: `repo.identity.resolve()` legitimately writes/reads identity
    bookkeeping under `.git/config`, and creating an isolated job
    worktree legitimately adds a `.git/worktrees/<id>/` registration
    entry -- both are the SHARED, common `.git` directory every linked
    worktree of one repository already shares by design, never the
    primary checkout's own WORKING-TREE files, which is the actual
    invariant this test protects."""
    return {
        path: value for path, value in filesystem_snapshot(root).items()
        if not path.startswith(".git" + os.sep) and path != ".git"
    }


def _text(payload: dict) -> WorkerResponse:
    return WorkerResponse(kind=WorkerResponseKind.TEXT, text=json.dumps(payload))


def _tool_call(tool: str, params: dict) -> WorkerResponse:
    return WorkerResponse(kind=WorkerResponseKind.TOOL_CALL, tool_call=WorkerToolCall(tool, params))


_CODER_FINAL_REPORT = {
    "completed_plan_steps": [
        {"plan_step_description": "add hello doc", "status": "completed", "note": ""},
    ],
    "unresolved_issues": [], "command_suggestions": [], "repair_notes": [],
}

_REVIEWER_PASS = {"verdict": "PASS", "summary": "looks fine", "findings": []}
_SECURITY_PASS = {"verdict": "PASS", "summary": "no issues", "findings": []}
_SECURITY_FAIL = {
    "verdict": "FAIL", "summary": "hardcoded secret",
    "findings": [{"description": "hardcoded secret", "category": "secrets", "blocking": True}],
}


@pytest.fixture
def ready_repo(git_repo_with_commit):
    """A primary repo with a baseline-committed `pyproject.toml`
    (`[tool.ruff]`) so at least one verification command is groundable --
    mirrors `tests.integration.test_finalization_service`'s own fixture
    convention exactly."""
    (git_repo_with_commit / "pyproject.toml").write_text("[tool.ruff]\n")
    commit(git_repo_with_commit, "add ruff config")
    return git_repo_with_commit


def _ready_plan(
    conn, blobs_dir, *, repo_id, worktree_id, goal="Add docs/HELLO.md",
    paths=("docs/HELLO.md",),
):
    now = utcnow_iso()
    with transaction(conn):
        PlanningRepo(conn).create_in_transaction(
            plan_id="plan-1", created_at=now, schema_version="v1", repo_id=repo_id,
            worktree_id=worktree_id, run_id=None,
            request_content_hash=files.digest(b"original request"), predecessor_plan_id=None,
            revision=1, state="DRAFT",
        )
    content = EngineeringPlanContent(
        goal=goal, planned_changes=(PlannedChange(description="add hello doc", paths=paths),),
    )
    blob = store_plan_content(ContentStore(conn, blobs_dir), content)
    with transaction(conn):
        PlanningRepo(conn).update_in_transaction(
            "plan-1", updated_at=utcnow_iso(), state="READY", plan_content_hash=blob.content_hash,
        )
    return PlanningRepo(conn).get("plan-1")


def _draft_plan(conn, blobs_dir, *, repo_id, worktree_id):
    now = utcnow_iso()
    with transaction(conn):
        PlanningRepo(conn).create_in_transaction(
            plan_id="plan-draft", created_at=now, schema_version="v1", repo_id=repo_id,
            worktree_id=worktree_id, run_id=None,
            request_content_hash=files.digest(b"original request"), predecessor_plan_id=None,
            revision=1, state="DRAFT",
        )
    return PlanningRepo(conn).get("plan-draft")


def test_happy_path_reaches_ready_for_human_merge(db_conn, tmp_path, ready_repo):
    info = identity.resolve(ready_repo)
    plan = _ready_plan(
        db_conn, tmp_path / "blobs", repo_id=info.repo_id, worktree_id=info.worktree_id,
    )
    adapter = ScriptedAdapter({
        "coder": [
            _tool_call("create_file", {"path": "docs/HELLO.md", "content": "hello world\n"}),
            _text(_CODER_FINAL_REPORT),
        ],
        "reviewer": [_text(_REVIEWER_PASS)],
        "security": [_text(_SECURITY_PASS)],
    })
    result = run_coding_job(
        ready_repo, control_conn=db_conn, control_blobs_dir=tmp_path / "blobs", plan=plan,
        original_prompt="Add docs/HELLO.md", allowed_scope=(".",), coder_adapter=adapter,
        reviewer_adapter=adapter, security_adapter=adapter,
    )
    assert result.final_state == CodingJobState.READY_FOR_HUMAN_MERGE, result.reason
    assert result.review.verdict.value == "PASS"
    assert result.security.verdict.value == "PASS"
    assert result.repair_attempts == 0
    assert adapter.calls[0] == "coder"


def test_insufficient_plan_fails_closed_before_any_workspace(db_conn, tmp_path, ready_repo):
    info = identity.resolve(ready_repo)
    plan = _draft_plan(
        db_conn, tmp_path / "blobs", repo_id=info.repo_id, worktree_id=info.worktree_id,
    )
    adapter = ScriptedAdapter({})
    result = run_coding_job(
        ready_repo, control_conn=db_conn, control_blobs_dir=tmp_path / "blobs", plan=plan,
        original_prompt="Add docs/HELLO.md", allowed_scope=(".",), coder_adapter=adapter,
        reviewer_adapter=adapter, security_adapter=adapter,
    )
    assert result.final_state == CodingJobState.FAILED
    assert "insufficient_coder_scope" in result.reason
    assert adapter.calls == []
    # No coding_jobs row at all -- nothing durable or irreversible was
    # ever attempted for an insufficient handoff.
    row = db_conn.execute("SELECT COUNT(*) AS n FROM coding_jobs").fetchone()
    assert row["n"] == 0


def test_security_fail_blocks_readiness_even_after_review_and_verification_pass(
    db_conn, tmp_path, ready_repo,
):
    info = identity.resolve(ready_repo)
    plan = _ready_plan(
        db_conn, tmp_path / "blobs", repo_id=info.repo_id, worktree_id=info.worktree_id,
    )
    adapter = ScriptedAdapter({
        "coder": [
            _tool_call("create_file", {"path": "docs/HELLO.md", "content": "hello world\n"}),
            _text(_CODER_FINAL_REPORT),
        ],
        "reviewer": [_text(_REVIEWER_PASS)],
        "security": [_text(_SECURITY_FAIL)],
    })
    result = run_coding_job(
        ready_repo, control_conn=db_conn, control_blobs_dir=tmp_path / "blobs", plan=plan,
        original_prompt="Add docs/HELLO.md", allowed_scope=(".",), coder_adapter=adapter,
        reviewer_adapter=adapter, security_adapter=adapter,
    )
    assert result.final_state == CodingJobState.BLOCKED
    assert result.security.verdict.value == "FAIL"
    assert "security:FAIL" in result.reason


def test_reviewer_changes_required_drives_exactly_one_bounded_repair_round(
    db_conn, tmp_path, ready_repo,
):
    info = identity.resolve(ready_repo)
    plan = _ready_plan(
        db_conn, tmp_path / "blobs", repo_id=info.repo_id, worktree_id=info.worktree_id,
    )
    changes_required = {
        "verdict": "CHANGES_REQUIRED", "summary": "needs a trailing newline",
        "findings": [{
            "description": "missing trailing newline", "severity": "minor",
            "path": "docs/HELLO.md",
        }],
    }
    adapter = ScriptedAdapter({
        "coder": [
            _tool_call("create_file", {"path": "docs/HELLO.md", "content": "hello world"}),
            _text(_CODER_FINAL_REPORT),
            # Repair round: model reads current content, then rewrites it.
            _tool_call("read_file", {"path": "docs/HELLO.md"}),
            _text(_CODER_FINAL_REPORT),
        ],
        "reviewer": [_text(changes_required), _text(_REVIEWER_PASS)],
        "security": [_text(_SECURITY_PASS)],
    })
    result = run_coding_job(
        ready_repo, control_conn=db_conn, control_blobs_dir=tmp_path / "blobs", plan=plan,
        original_prompt="Add docs/HELLO.md", allowed_scope=(".",), coder_adapter=adapter,
        reviewer_adapter=adapter, security_adapter=adapter,
        config=CodingJobConfig(max_repair_attempts=2),
    )
    assert result.final_state == CodingJobState.READY_FOR_HUMAN_MERGE, result.reason
    assert result.repair_attempts == 1
    assert adapter.calls.count("reviewer") == 2


def test_repair_budget_exhaustion_reaches_human_required_not_an_infinite_loop(
    db_conn, tmp_path, ready_repo,
):
    """A Reviewer that never approves must bound the repair loop exactly
    at `max_repair_attempts` (reusing `finalization.service.Finalizer`'s
    own, already-tested counter -- no second budget invented here) and
    land the job in `HUMAN_REQUIRED`, never silently retry forever and
    never a fabricated PASS."""
    info = identity.resolve(ready_repo)
    plan = _ready_plan(
        db_conn, tmp_path / "blobs", repo_id=info.repo_id, worktree_id=info.worktree_id,
    )
    changes_required = {
        "verdict": "CHANGES_REQUIRED", "summary": "still not good enough", "findings": [],
    }
    adapter = ScriptedAdapter({
        "coder": [
            _tool_call("create_file", {"path": "docs/HELLO.md", "content": "v0"}),
            _text(_CODER_FINAL_REPORT),
            _tool_call("read_file", {"path": "docs/HELLO.md"}),
            _text(_CODER_FINAL_REPORT),
        ],
        "reviewer": [_text(changes_required), _text(changes_required)],
        "security": [],
    })
    result = run_coding_job(
        ready_repo, control_conn=db_conn, control_blobs_dir=tmp_path / "blobs", plan=plan,
        original_prompt="Add docs/HELLO.md", allowed_scope=(".",), coder_adapter=adapter,
        reviewer_adapter=adapter, security_adapter=adapter,
        config=CodingJobConfig(max_repair_attempts=1),
    )
    assert result.final_state == CodingJobState.HUMAN_REQUIRED, result.reason
    assert result.repair_attempts == 1
    assert adapter.calls.count("reviewer") == 2
    assert adapter.calls.count("security") == 0


def test_unauthorized_scope_mutation_is_denied_before_it_ever_reaches_disk(
    db_conn, tmp_path, ready_repo,
):
    """A Coder tool call outside `allowed_scope` must be refused by the
    real `PolicyEngine`/`ToolExecutor` -- the file must never be written
    at all, and the job must fail rather than silently drop the attempt
    and succeed anyway."""
    info = identity.resolve(ready_repo)
    plan = _ready_plan(
        db_conn, tmp_path / "blobs", repo_id=info.repo_id, worktree_id=info.worktree_id,
        paths=("secrets/HELLO.md",),
    )
    adapter = ScriptedAdapter({
        "coder": [
            _tool_call("create_file", {"path": "secrets/HELLO.md", "content": "leak\n"}),
            _text(_CODER_FINAL_REPORT),
            _text(_CODER_FINAL_REPORT),
        ],
        "reviewer": [], "security": [],
    })
    result = run_coding_job(
        ready_repo, control_conn=db_conn, control_blobs_dir=tmp_path / "blobs", plan=plan,
        original_prompt="Add secrets/HELLO.md", allowed_scope=("docs",), coder_adapter=adapter,
        reviewer_adapter=adapter, security_adapter=adapter,
    )
    assert not (ready_repo / "secrets").exists()
    assert result.final_state in (CodingJobState.FAILED, CodingJobState.BLOCKED)


def test_primary_checkout_is_never_mutated(db_conn, tmp_path, ready_repo):
    before_head = git(ready_repo, "rev-parse", "HEAD")
    before_status = git(ready_repo, "status", "--porcelain")
    before_snapshot = _working_tree_snapshot(ready_repo)

    info = identity.resolve(ready_repo)
    plan = _ready_plan(
        db_conn, tmp_path / "blobs", repo_id=info.repo_id, worktree_id=info.worktree_id,
    )
    adapter = ScriptedAdapter({
        "coder": [
            _tool_call("create_file", {"path": "docs/HELLO.md", "content": "hello world\n"}),
            _text(_CODER_FINAL_REPORT),
        ],
        "reviewer": [_text(_REVIEWER_PASS)], "security": [_text(_SECURITY_PASS)],
    })
    result = run_coding_job(
        ready_repo, control_conn=db_conn, control_blobs_dir=tmp_path / "blobs", plan=plan,
        original_prompt="Add docs/HELLO.md", allowed_scope=(".",), coder_adapter=adapter,
        reviewer_adapter=adapter, security_adapter=adapter,
    )
    assert result.final_state == CodingJobState.READY_FOR_HUMAN_MERGE, result.reason
    assert git(ready_repo, "rev-parse", "HEAD") == before_head
    assert git(ready_repo, "status", "--porcelain") == before_status
    assert _working_tree_snapshot(ready_repo) == before_snapshot
