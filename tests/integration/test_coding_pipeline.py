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
from pathlib import Path

import pytest

from code_slayer.coding.pipeline import CodingJobConfig
from code_slayer.coding.pipeline_types import CodingJobState
from code_slayer.coding.testing import run_coding_job_for_testing as run_coding_job
from code_slayer.planning.models import (
    AffectedFile,
    AffectedFileAction,
    EngineeringPlanContent,
    PlannedChange,
)
from code_slayer.planning.provenance import store_plan_content
from code_slayer.repo import identity
from code_slayer.store import location
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import connect, transaction, utcnow_iso
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


def _task_state(result) -> str:
    """Independently read the underlying `tasks.state` for a finished
    job's own execution-plane task -- opens the isolated job worktree's
    own database directly (never trusts anything in-process), exactly
    the "actual git/DB state is authoritative" discipline this whole
    branch is built on."""
    job_worktree = Path(result.job_worktree_path)
    job_identity = identity.resolve(job_worktree)
    exec_conn = connect(location.db_path(job_identity.repo_id, job_identity.worktree_id))
    try:
        row = exec_conn.execute(
            "SELECT state FROM tasks WHERE task_id = ?", (result.task_id,),
        ).fetchone()
    finally:
        exec_conn.close()
    assert row is not None
    return row["state"]


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
    convention exactly. Also baseline-commits a `docs/` directory:
    `tools.file_tools.parent_fd()` never auto-creates a missing parent
    directory (each path component must already exist, opened with
    `O_NOFOLLOW`) -- every test in this module targets `docs/HELLO.md`,
    so without this the tests' own `create_file` calls would silently
    fail (denied/errored, never reaching disk) and every assertion that
    only checked `result.final_state` rather than the file's own content
    would still pass for the wrong reason. Also baseline-commits a
    `secrets/` directory for the same reason, so an out-of-scope
    `create_file` attempt targeting it is denied specifically by
    `policy.engine.PolicyEngine`'s own `outside_task_scope` scope check,
    never masked by an incidental `invalid_context_or_request` from a
    missing parent directory."""
    (git_repo_with_commit / "pyproject.toml").write_text("[tool.ruff]\n")
    (git_repo_with_commit / "docs").mkdir()
    (git_repo_with_commit / "docs" / ".gitkeep").write_text("")
    (git_repo_with_commit / "secrets").mkdir()
    (git_repo_with_commit / "secrets" / ".gitkeep").write_text("")
    commit(git_repo_with_commit, "add ruff config, docs/, and secrets/ directories")
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
        # The real, evidence-validated authorization source
        # `coding.handoff.validate_mutation_scope()` now reads --
        # `planned_changes[].paths` above is kept only as an unrelated,
        # unvalidated field real Planner output also carries, never the
        # scope-authorization source (see that function's own docstring).
        affected_files=tuple(
            AffectedFile(
                path=path, action=AffectedFileAction.CREATE, reason="add hello doc",
                exists_in_repository=False,
            )
            for path in paths
        ),
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
        original_prompt="Add docs/HELLO.md", allowed_scope=("docs/HELLO.md",),
        coder_adapter=adapter, reviewer_adapter=adapter, security_adapter=adapter,
    )
    assert result.final_state == CodingJobState.READY_FOR_HUMAN_MERGE, result.reason
    assert result.review.verdict.value == "PASS"
    assert result.security.verdict.value == "PASS"
    assert result.repair_attempts == 0
    assert adapter.calls[0] == "coder"
    # A real mutation actually happened -- never merely inferred from
    # `final_state` alone, which a silently-failed write (e.g. a missing
    # parent directory) could otherwise still reach if nothing checked
    # the file's own content on disk.
    assert Path(result.job_worktree_path, "docs", "HELLO.md").read_text() == "hello world\n"


def test_insufficient_plan_fails_closed_before_any_workspace(db_conn, tmp_path, ready_repo):
    info = identity.resolve(ready_repo)
    plan = _draft_plan(
        db_conn, tmp_path / "blobs", repo_id=info.repo_id, worktree_id=info.worktree_id,
    )
    adapter = ScriptedAdapter({})
    result = run_coding_job(
        ready_repo, control_conn=db_conn, control_blobs_dir=tmp_path / "blobs", plan=plan,
        original_prompt="Add docs/HELLO.md", allowed_scope=("docs/HELLO.md",),
        coder_adapter=adapter, reviewer_adapter=adapter, security_adapter=adapter,
    )
    assert result.final_state == CodingJobState.FAILED
    assert "insufficient_coder_scope" in result.reason


def test_scope_broader_than_planner_authorization_fails_closed_before_workspace(
    db_conn, tmp_path, ready_repo,
):
    """Fix 4: `allowed_scope` broader than what the validated plan's own
    `planned_changes[].paths` declared must fail closed -- before any
    workspace is ever created, exactly like an insufficient plan does --
    never silently broadened to whatever the caller happened to pass."""
    info = identity.resolve(ready_repo)
    plan = _ready_plan(
        db_conn, tmp_path / "blobs", repo_id=info.repo_id, worktree_id=info.worktree_id,
        paths=("docs/HELLO.md",),
    )
    adapter = ScriptedAdapter({})
    result = run_coding_job(
        ready_repo, control_conn=db_conn, control_blobs_dir=tmp_path / "blobs", plan=plan,
        original_prompt="Add docs/HELLO.md", allowed_scope=(".",), coder_adapter=adapter,
        reviewer_adapter=adapter, security_adapter=adapter,
    )
    assert result.final_state == CodingJobState.FAILED
    assert "insufficient_coder_scope" in result.reason
    assert "scope_exceeds_planner_authorization" in result.reason
    assert adapter.calls == []
    row = db_conn.execute("SELECT COUNT(*) AS n FROM coding_jobs").fetchone()
    assert row["n"] == 0
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
        original_prompt="Add docs/HELLO.md", allowed_scope=("docs/HELLO.md",),
        coder_adapter=adapter, reviewer_adapter=adapter, security_adapter=adapter,
    )
    assert result.final_state == CodingJobState.BLOCKED
    assert result.security.verdict.value == "FAIL"
    assert "security:FAIL" in result.reason
    # Fix 2 state-consistency: coding_jobs.state=BLOCKED cannot coexist
    # with the underlying TaskState still sitting at READY_FOR_CHECKPOINT
    # (checkpoint-eligible again via any future caller with a fresh
    # lease) -- it must have been transitioned to FAILED too.
    assert _task_state(result) == "FAILED"


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
        original_prompt="Add docs/HELLO.md", allowed_scope=("docs/HELLO.md",),
        coder_adapter=adapter, reviewer_adapter=adapter, security_adapter=adapter,
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
        original_prompt="Add docs/HELLO.md", allowed_scope=("docs/HELLO.md",),
        coder_adapter=adapter, reviewer_adapter=adapter, security_adapter=adapter,
        config=CodingJobConfig(max_repair_attempts=1),
    )
    assert result.final_state == CodingJobState.HUMAN_REQUIRED, result.reason
    assert result.repair_attempts == 1
    assert adapter.calls.count("reviewer") == 2
    assert adapter.calls.count("security") == 0


def test_unauthorized_scope_write_is_denied_by_policy_before_it_ever_reaches_disk(
    db_conn, tmp_path, ready_repo,
):
    """A Coder tool call outside `allowed_scope` must be refused by the
    real `PolicyEngine`/`ToolExecutor` -- proven directly, not inferred
    from the scripted adapter running out of responses. The Coder is
    scripted to attempt the out-of-scope write FIRST, then the legitimate
    in-scope one, then finish -- so the whole job succeeds normally
    around the denial, which is exactly what proves the denial was a
    real, targeted policy refusal rather than merely "the turn failed for
    some other reason": if scope enforcement were broken, this job would
    either write `secrets/HELLO.md` for real, or fail the whole turn."""
    info = identity.resolve(ready_repo)
    plan = _ready_plan(
        db_conn, tmp_path / "blobs", repo_id=info.repo_id, worktree_id=info.worktree_id,
        paths=("docs/HELLO.md",),
    )
    adapter = ScriptedAdapter({
        "coder": [
            _tool_call("create_file", {"path": "secrets/HELLO.md", "content": "leak\n"}),
            _tool_call("create_file", {"path": "docs/HELLO.md", "content": "hello world\n"}),
            _text(_CODER_FINAL_REPORT),
        ],
        "reviewer": [_text(_REVIEWER_PASS)], "security": [_text(_SECURITY_PASS)],
    })
    result = run_coding_job(
        ready_repo, control_conn=db_conn, control_blobs_dir=tmp_path / "blobs", plan=plan,
        original_prompt="Add docs/HELLO.md", allowed_scope=("docs/HELLO.md",),
        coder_adapter=adapter, reviewer_adapter=adapter, security_adapter=adapter,
    )
    assert result.final_state == CodingJobState.READY_FOR_HUMAN_MERGE, result.reason

    # Independently inspect the ISOLATED JOB WORKTREE (never the primary
    # checkout, which the mutation never touches regardless of whether
    # scope enforcement works) for the actual on-disk proof.
    job_worktree = Path(result.job_worktree_path)
    assert not (job_worktree / "secrets" / "HELLO.md").exists()
    assert list((job_worktree / "secrets").iterdir()) == [job_worktree / "secrets" / ".gitkeep"]
    assert (job_worktree / "docs" / "HELLO.md").read_text() == "hello world\n"

    # Independently inspect the real, durable tool_operations/audit_events
    # journal in the job's own execution-plane database for the exact
    # policy denial -- the actual code-owned evidence the write was
    # refused by `policy.engine.PolicyEngine`, never a side effect someone
    # could mistake for something else.
    job_identity = identity.resolve(job_worktree)
    exec_conn = connect(location.db_path(job_identity.repo_id, job_identity.worktree_id))
    try:
        denials = exec_conn.execute(
            "SELECT payload_json FROM audit_events WHERE event_type = 'POLICY_DENIED'",
        ).fetchall()
    finally:
        exec_conn.close()
    assert any(
        "outside_task_scope" in row["payload_json"] and "create_file" in row["payload_json"]
        for row in denials
    ), [row["payload_json"] for row in denials]
    assert len(denials) == 1


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
        original_prompt="Add docs/HELLO.md", allowed_scope=("docs/HELLO.md",),
        coder_adapter=adapter, reviewer_adapter=adapter, security_adapter=adapter,
    )
    assert result.final_state == CodingJobState.READY_FOR_HUMAN_MERGE, result.reason
    assert git(ready_repo, "rev-parse", "HEAD") == before_head
    assert git(ready_repo, "status", "--porcelain") == before_status
    assert _working_tree_snapshot(ready_repo) == before_snapshot


def _happy_job(db_conn, tmp_path, ready_repo, adapter):
    info = identity.resolve(ready_repo)
    plan = _ready_plan(
        db_conn, tmp_path / "blobs", repo_id=info.repo_id, worktree_id=info.worktree_id,
    )
    return run_coding_job(
        ready_repo, control_conn=db_conn, control_blobs_dir=tmp_path / "blobs", plan=plan,
        original_prompt="Add docs/HELLO.md", allowed_scope=("docs/HELLO.md",),
        coder_adapter=adapter, reviewer_adapter=adapter, security_adapter=adapter,
    )


def _happy_adapter():
    return ScriptedAdapter({
        "coder": [
            _tool_call("create_file", {"path": "docs/HELLO.md", "content": "hello world\n"}),
            _text(_CODER_FINAL_REPORT),
        ],
        "reviewer": [_text(_REVIEWER_PASS)],
        "security": [_text(_SECURITY_PASS)],
    })


def test_checkpoint_not_created_never_reaches_ready_for_human_merge(
    db_conn, tmp_path, ready_repo, monkeypatch,
):
    """Fix 1: a non-`CREATED` `CheckpointAdvanceOutcome` -- here `DENIED`,
    but the pipeline's own branch treats every non-`CREATED` value
    identically -- must never let the job reach `READY_FOR_HUMAN_MERGE`,
    regardless of Security having already passed. `advance_ready_for_
    checkpoint` itself is unmodified, already-tested code
    (`finalization.lifecycle`); this test targets `coding.pipeline`'s own
    NEW branching logic around its result, so it substitutes a
    deterministic fake for exactly that one call rather than trying to
    engineer a genuine `CheckpointManager` failure."""
    from code_slayer.coding import pipeline as pipeline_module
    from code_slayer.finalization.lifecycle import CheckpointAdvanceOutcome, CheckpointAdvanceResult

    def _fake_advance_ready_for_checkpoint(*args, **kwargs):
        return CheckpointAdvanceResult(CheckpointAdvanceOutcome.DENIED, "simulated_denial")

    monkeypatch.setattr(
        pipeline_module, "advance_ready_for_checkpoint", _fake_advance_ready_for_checkpoint,
    )
    result = _happy_job(db_conn, tmp_path, ready_repo, _happy_adapter())
    assert result.final_state == CodingJobState.BLOCKED, result.reason
    assert "checkpoint_not_created" in result.reason
    assert "DENIED" in result.reason
    row = db_conn.execute(
        "SELECT state, final_reason FROM coding_jobs WHERE job_id = ?", (result.job_id,),
    ).fetchone()
    assert row["state"] == "BLOCKED"
    assert row["final_reason"] == result.reason
    # Fix 2 state-consistency: the underlying task -- which never
    # actually left READY_FOR_CHECKPOINT, since checkpoint creation was
    # faked to be denied -- must have been transitioned to FAILED too,
    # never left checkpoint-eligible.
    assert _task_state(result) == "FAILED"


def test_completion_not_confirmed_never_reaches_ready_for_human_merge(
    db_conn, tmp_path, ready_repo, monkeypatch,
):
    """Fix 1: a real `CREATED` checkpoint whose subsequent completion
    advance is NOT `COMPLETED` (here `DENIED`, e.g. `checkpointed_
    completion_guard`'s own real content-fingerprint TOCTOU check
    refusing it) must also never let the job reach `READY_FOR_HUMAN_
    MERGE` -- proves the pipeline does not merely check the checkpoint
    step and ignore completion's own, previously-discarded, return
    value."""
    from code_slayer.coding import pipeline as pipeline_module
    from code_slayer.finalization.lifecycle import CompletionAdvanceOutcome, CompletionAdvanceResult

    def _fake_advance_checkpointed_completion(*args, **kwargs):
        return CompletionAdvanceResult(
            CompletionAdvanceOutcome.DENIED, "simulated_completion_denial",
        )

    monkeypatch.setattr(
        pipeline_module, "advance_checkpointed_completion", _fake_advance_checkpointed_completion,
    )
    result = _happy_job(db_conn, tmp_path, ready_repo, _happy_adapter())
    assert result.final_state == CodingJobState.BLOCKED, result.reason
    assert "completion_not_confirmed" in result.reason
    assert "DENIED" in result.reason
    # Fix 2 state-consistency: the real checkpoint DID get created here
    # (only completion was faked to fail), so the task actually reached
    # CHECKPOINTED before this branch ran -- it must still have been
    # transitioned onward to FAILED, never left sitting at CHECKPOINTED
    # (which `finalization.dispatcher.TaskLifecycleExecutor` would
    # otherwise treat as eligible for a bare completion retry with no
    # awareness this job was ever blocked).
    assert _task_state(result) == "FAILED"


def test_matching_candidate_reaches_ready_for_human_merge(db_conn, tmp_path, ready_repo):
    """Fix 1, positive case: checkpoint `CREATED` + completion
    `COMPLETED` (the real, unmocked outcomes for a healthy job) is the
    ONLY path that reaches `READY_FOR_HUMAN_MERGE` -- already exercised
    by every other happy-path test in this module; restated here
    explicitly as the direct counterpart to the two failure-outcome
    tests above."""
    result = _happy_job(db_conn, tmp_path, ready_repo, _happy_adapter())
    assert result.final_state == CodingJobState.READY_FOR_HUMAN_MERGE, result.reason


def test_stale_candidate_after_review_and_security_pass_blocks_readiness(
    db_conn, tmp_path, ready_repo, monkeypatch,
):
    """Fix 3: if the candidate the pipeline is about to checkpoint no
    longer matches what Reviewer/Security actually approved, it must
    fail closed -- never merge a PASS(candidate A) into readiness for a
    different candidate B. `coding.mutation_guard.compute_diff_text()`
    is called three times in a healthy, non-repair run (once for
    Reviewer, once for Security, once for the final pre-checkpoint
    recheck); this test lets the first two return the real diff Reviewer/
    Security actually saw and approved, then returns tampered text only
    for the third (final) call -- simulating exactly "candidate changed
    after both PASS verdicts were already given"."""
    from code_slayer.coding import pipeline as pipeline_module

    real_compute_diff_text = pipeline_module.compute_diff_text
    calls = {"n": 0}

    def _tampering_compute_diff_text(*args, **kwargs):
        calls["n"] += 1
        text = real_compute_diff_text(*args, **kwargs)
        if calls["n"] >= 3:
            return text + "\n# candidate changed after approval\n"
        return text

    monkeypatch.setattr(pipeline_module, "compute_diff_text", _tampering_compute_diff_text)
    result = _happy_job(db_conn, tmp_path, ready_repo, _happy_adapter())
    assert result.final_state == CodingJobState.BLOCKED, result.reason
    assert "stale_evidence" in result.reason
    assert calls["n"] >= 3
    # Fix 2 state-consistency: same requirement as the Security-FAIL case
    # above -- a stale-candidate BLOCK must not leave the underlying task
    # sitting at READY_FOR_CHECKPOINT.
    assert _task_state(result) == "FAILED"


def test_set_state_persists_final_reason_and_emits_a_terminated_audit_event(db_conn):
    """Fix 2, direct/isolated proof: `_set_state(..., reason=...)` -- the
    argument that was previously accepted and silently discarded, never
    reaching `coding_jobs.final_reason` at all -- must now durably persist
    it through `CodingJobsRepo`'s own closed-set field validation, and
    must record a `CODING_JOB_TERMINATED` audit event carrying the same
    reason. Exercised directly against `_set_state()` itself (not through
    a full pipeline run) so this proves the mechanism in isolation,
    independent of which call site happens to trigger it."""
    from code_slayer.coding.pipeline import _set_state
    from code_slayer.coding.pipeline_types import CodingJobState
    from code_slayer.store.coding_jobs_repo import CodingJobsRepo
    from code_slayer.store.db import transaction, utcnow_iso

    with transaction(db_conn):
        PlanningRepo(db_conn).create_in_transaction(
            plan_id="plan-x", created_at=utcnow_iso(), schema_version="v1", repo_id="r1",
            worktree_id="w1", run_id=None, request_content_hash=files.digest(b"req"),
            predecessor_plan_id=None, revision=1, state="READY",
        )
        CodingJobsRepo(db_conn).create_in_transaction(
            job_id="job-x", plan_id="plan-x", repo_id="r1", primary_worktree_id="w1",
            created_at=utcnow_iso(), original_prompt_hash=files.digest(b"prompt"),
            base_revision="deadbeef", max_repair_attempts=2, state=CodingJobState.CREATED.value,
        )

    _set_state(
        db_conn, "job-x", CodingJobState.BLOCKED, reason="workspace_preflight_failed:simulated",
    )

    row = db_conn.execute(
        "SELECT state, final_reason FROM coding_jobs WHERE job_id = 'job-x'",
    ).fetchone()
    assert row["state"] == "BLOCKED"
    assert row["final_reason"] == "workspace_preflight_failed:simulated"

    events = db_conn.execute(
        "SELECT payload_json FROM audit_events WHERE event_type = 'CODING_JOB_TERMINATED'",
    ).fetchall()
    assert len(events) == 1
    assert "workspace_preflight_failed:simulated" in events[0]["payload_json"]
    assert "job-x" in events[0]["payload_json"]


def test_set_state_with_no_reason_writes_no_event_and_preserves_prior_final_reason(db_conn):
    """An ordinary in-flight progress transition (no `reason` given) must
    not overwrite a previously-recorded `final_reason` with an empty
    string, and must not emit a spurious `CODING_JOB_TERMINATED` event."""
    from code_slayer.coding.pipeline import _set_state
    from code_slayer.coding.pipeline_types import CodingJobState
    from code_slayer.store.coding_jobs_repo import CodingJobsRepo
    from code_slayer.store.db import transaction, utcnow_iso

    with transaction(db_conn):
        PlanningRepo(db_conn).create_in_transaction(
            plan_id="plan-y", created_at=utcnow_iso(), schema_version="v1", repo_id="r1",
            worktree_id="w1", run_id=None, request_content_hash=files.digest(b"req"),
            predecessor_plan_id=None, revision=1, state="READY",
        )
        CodingJobsRepo(db_conn).create_in_transaction(
            job_id="job-y", plan_id="plan-y", repo_id="r1", primary_worktree_id="w1",
            created_at=utcnow_iso(), original_prompt_hash=files.digest(b"prompt"),
            base_revision="deadbeef", max_repair_attempts=2, state=CodingJobState.CREATED.value,
        )

    _set_state(db_conn, "job-y", CodingJobState.BLOCKED, reason="a real reason")
    _set_state(db_conn, "job-y", CodingJobState.RUNNING)  # no reason -- an ordinary progress step

    row = db_conn.execute(
        "SELECT state, final_reason FROM coding_jobs WHERE job_id = 'job-y'",
    ).fetchone()
    assert row["state"] == "RUNNING"
    assert row["final_reason"] == "a real reason"

    events = db_conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE event_type = 'CODING_JOB_TERMINATED'",
    ).fetchone()
    assert events["n"] == 1
