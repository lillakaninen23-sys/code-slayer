"""`LocalWorkerRunner`: the persistent, resumable Phase 7.7 application
service composing Prompt Analyst, Question Gate, control-plane trust,
job-worktree isolation, and the existing guarded worker execution path
into one restartable runner.

Covers (see the numbered list in the Phase 7.7 task): exact original
prompt/hash durability across restart; that the Prompt Analyst and the
hardened Question Gate are genuinely invoked, never skipped; that the
analyst cannot manufacture trusted resolution evidence; ASK blocking
before any inference and surviving a full process/connection restart;
explicit human FACT/AUTHORIZATION resolution semantics (right kind
required, right ambiguity id required); SUPPRESS proceeding to the real
guarded worker path; persistent control-plane trust surviving restart
without being copied into a job worktree's own database; exact
capability-scope/LOCKED enforcement; exact durable tool output reaching
the continuation; idempotent resume of a completed run; job-worktree
creation/reuse for a mutating policy and denial without exact mutation
trust, with the primary repository proven untouched; stale-lease safety;
concurrent-resume race safety; fail-closed mid-turn recovery; and audit
chain validity.
"""

from __future__ import annotations

import threading

import pytest

from code_slayer.audit.verify import verify_chain
from code_slayer.lease.manager import LeaseHandle, LeaseManager
from code_slayer.runner import LocalWorkerRunner, RunStatus
from code_slayer.store import location
from code_slayer.store.db import connect as db_connect
from code_slayer.store.db import transaction
from code_slayer.store.runner_repo import RunnerRepo
from code_slayer.store.tool_operations_repo import ToolOperationsRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.conformance import run_conformance_suite
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.fake_prompt_analyst import FakePromptAnalyst
from code_slayer.workers.promotion import promote_from_conformance
from code_slayer.workers.prompt_analysis import (
    Ambiguity,
    AmbiguityRiskClass,
    EvidenceSource,
    PromptAnalysis,
    hash_original_prompt,
)
from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind, WorkerToolCall
from code_slayer.workers.question_gate import ResolutionKind
from code_slayer.workers.trust import TrustLevel, WorkerTrustManager

WORKER_ID = "test-worker"
ROLE = "coder"
README_BYTES = b"hello\n"


def _passing_conformance_responses():
    return [
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="hi there"),
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="clean output"),
        WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="read_file", params={"path": "README.md"}),
        ),
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="continuing"),
        WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="read_file", params={"path": "README.md"}),
        ),
    ]


def _grant_guarded_via_conformance(conn, *, worker_id=WORKER_ID, role=ROLE):
    """Real, durable trust earned through the actual conformance/
    promotion APIs -- never a manually inserted trust row."""
    adapter = FakeWorkerAdapter(_passing_conformance_responses())
    suite = run_conformance_suite(conn, adapter, worker_id=worker_id, role=role)
    assert suite.ok and suite.status == "PASSED", suite
    result = promote_from_conformance(
        conn, worker_id=worker_id, role=role, capability="read_file", run_id=suite.run_id,
    )
    assert result.ok, result.reason


def _read_call(path="README.md"):
    return WorkerResponse(
        kind=WorkerResponseKind.TOOL_CALL,
        tool_call=WorkerToolCall(tool="read_file", params={"path": path}),
    )


def _text(content="ok"):
    return WorkerResponse(kind=WorkerResponseKind.TEXT, text=content)


@pytest.fixture
def primary(git_repo_with_commit):
    return git_repo_with_commit


@pytest.fixture
def runner(primary):
    r = LocalWorkerRunner(primary)
    WorkersRepo(r._control_conn).register(worker_id=WORKER_ID, kind="fake", network_class="local")
    yield r
    r.close()


@pytest.fixture
def guarded_runner(runner):
    _grant_guarded_via_conformance(runner._control_conn)
    return runner


# --- 1/2. exact original prompt / hash durability ---------------------------

def test_exact_original_prompt_persists_unchanged(runner):
    prompt = "  Read the README.md file, please.\t\n"
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    row = RunnerRepo(runner._control_conn).get(result.run_id)
    from code_slayer.workers.prompt_provenance import read_original_prompt

    restored = read_original_prompt(
        runner._control_conn, runner._control_blobs_dir, row.original_prompt_hash,
    )
    assert restored == prompt  # byte-for-byte, no normalization


def test_prompt_hash_survives_restart(primary):
    prompt = "Read the README.md file."
    r1 = LocalWorkerRunner(primary)
    WorkersRepo(r1._control_conn).register(
        worker_id=WORKER_ID, kind="fake", network_class="local",
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    result = r1.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    row_before = RunnerRepo(r1._control_conn).get(result.run_id)
    r1.close()

    r2 = LocalWorkerRunner(primary)
    row_after = RunnerRepo(r2._control_conn).get(result.run_id)
    assert row_after.original_prompt_hash == row_before.original_prompt_hash
    assert row_after.original_prompt_hash == hash_original_prompt(prompt)
    r2.close()


# --- 3/4. PromptAnalyst is actually invoked / analysis durably recorded ----

def test_prompt_analyst_is_actually_invoked(runner):
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    runner.start(original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst)
    assert len(analyst.calls) == 1
    assert analyst.calls[0][0] == prompt


def test_analysis_is_durably_recorded(runner):
    prompt = "Read the README.md file."
    ambiguity = Ambiguity(
        id="x", question="q?", rationale="r", risk_class=AmbiguityRiskClass.ROUTINE,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    row = RunnerRepo(runner._control_conn).get(result.run_id)
    assert row.analysis_content_hash is not None
    from code_slayer.workers.prompt_provenance import read_prompt_analysis

    restored = read_prompt_analysis(
        runner._control_conn, runner._control_blobs_dir, row.analysis_content_hash,
    )
    assert restored.ambiguities[0].id == "x"


# --- 5/6. hardened QuestionGate invoked; analyst cannot manufacture evidence -

def test_hardened_question_gate_is_actually_invoked_and_asks_on_ambiguity(runner):
    prompt = "Delete old files."
    ambiguity = Ambiguity(
        id="delete-scope", question="Should this permanently delete files?",
        rationale="Irreversible.", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    assert result.status == RunStatus.BLOCKED_ON_QUESTIONS
    assert result.questions == (ambiguity.question,)


def test_analyst_cannot_manufacture_trusted_resolution_evidence(runner):
    """The analyst proposes both the ambiguity AND the substring/evidence
    key that would resolve it -- the runner never promotes an analyst's
    own hint into ResolutionEvidence, so this must still ASK."""
    prompt = "Delete the old database once the migration finishes."
    ambiguity = Ambiguity(
        id="which-database", question="Which database should be destroyed?",
        rationale="Destroying the wrong database is catastrophic.",
        risk_class=AmbiguityRiskClass.DESTRUCTIVE,
        resolved_by_prompt_substring="the",  # trivially present, proves nothing
        evidence_keys=("self-proposed-key",),
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    assert result.status == RunStatus.BLOCKED_ON_QUESTIONS
    assert result.questions == (ambiguity.question,)


# --- 7/8. ASK blocks before inference; survives restart ---------------------

def test_ask_blocks_before_any_worker_inference(runner):
    prompt = "Delete old files."
    ambiguity = Ambiguity(
        id="delete-scope", question="Should this permanently delete files?",
        rationale="Irreversible.", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    adapter = FakeWorkerAdapter([])  # any call raises FakeWorkerAdapterError
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        adapter=adapter,
    )
    assert result.status == RunStatus.BLOCKED_ON_QUESTIONS
    assert len(adapter.calls) == 0


def test_ask_survives_complete_process_and_connection_restart(primary):
    prompt = "Delete old files."
    ambiguity = Ambiguity(
        id="delete-scope", question="Should this permanently delete files?",
        rationale="Irreversible.", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    r1 = LocalWorkerRunner(primary)
    WorkersRepo(r1._control_conn).register(
        worker_id=WORKER_ID, kind="fake", network_class="local",
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = r1.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    assert result.status == RunStatus.BLOCKED_ON_QUESTIONS
    r1.close()

    r2 = LocalWorkerRunner(primary)
    status = r2.status(result.run_id)
    assert status.status == RunStatus.BLOCKED_ON_QUESTIONS
    assert status.questions == (ambiguity.question,)
    adapter = FakeWorkerAdapter([])
    resumed = r2.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.BLOCKED_ON_QUESTIONS  # still no resolution supplied
    assert len(adapter.calls) == 0
    r2.close()


# --- 9/10/11/12. explicit human resolution semantics ------------------------

def test_explicit_human_fact_resolution_unblocks_material_ambiguity(runner):
    prompt = "Add a script for the project's package manager."
    ambiguity = Ambiguity(
        id="package-manager", question="Which package manager?",
        rationale="r", risk_class=AmbiguityRiskClass.MATERIAL,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    assert result.status == RunStatus.BLOCKED_ON_QUESTIONS

    runner.record_user_resolution(
        result.run_id, "package-manager", "npm",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    resumed = runner.resume(result.run_id)
    assert resumed.status == RunStatus.READY


def test_explicit_human_authorization_unblocks_destructive_ambiguity(runner):
    prompt = "Delete old files."
    ambiguity = Ambiguity(
        id="delete-scope", question="Should this permanently delete files?",
        rationale="Irreversible.", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    assert result.status == RunStatus.BLOCKED_ON_QUESTIONS

    runner.record_user_resolution(
        result.run_id, "delete-scope", "Yes, delete permanently, I authorize it.",
        resolution_kind=ResolutionKind.AUTHORIZATION, source=EvidenceSource.ORIGINAL_PROMPT,
    )
    resumed = runner.resume(result.run_id)
    assert resumed.status == RunStatus.READY


def test_wrong_resolution_kind_remains_blocked(runner):
    """A FACT cannot authorize a DESTRUCTIVE ambiguity -- only AUTHORIZATION
    can (P7.6 hardening); the human resolution API enforces the same
    risk-specific authority the gate itself does."""
    prompt = "Delete old files."
    ambiguity = Ambiguity(
        id="delete-scope", question="Should this permanently delete files?",
        rationale="Irreversible.", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )

    runner.record_user_resolution(
        result.run_id, "delete-scope", "yes",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    resumed = runner.resume(result.run_id)
    assert resumed.status == RunStatus.BLOCKED_ON_QUESTIONS
    assert resumed.questions == (ambiguity.question,)


def test_unrelated_resolution_id_remains_blocked(runner):
    prompt = "Delete old files."
    ambiguity = Ambiguity(
        id="delete-scope", question="Should this permanently delete files?",
        rationale="Irreversible.", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )

    runner.record_user_resolution(
        result.run_id, "totally-different-ambiguity-id", "Yes, I authorize it.",
        resolution_kind=ResolutionKind.AUTHORIZATION, source=EvidenceSource.ORIGINAL_PROMPT,
    )
    resumed = runner.resume(result.run_id)
    assert resumed.status == RunStatus.BLOCKED_ON_QUESTIONS
    assert resumed.questions == (ambiguity.question,)


def test_human_resolution_rejects_repository_and_runtime_sources(runner):
    prompt = "x"
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    with pytest.raises(ValueError):
        runner.record_user_resolution(
            result.run_id, "x", "answer", resolution_kind=ResolutionKind.FACT,
            source=EvidenceSource.REPOSITORY,
        )


# --- 13. SUPPRESS proceeds to worker stage ----------------------------------

def test_suppress_proceeds_to_worker_stage(guarded_runner):
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    adapter = FakeWorkerAdapter([_read_call(), _text("The README says hello.")])
    result = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        adapter=adapter,
    )
    assert result.status == RunStatus.COMPLETED
    assert result.final_text == "The README says hello."
    assert len(adapter.calls) == 2


# --- 14/15. control-plane trust survives restart; no copying ---------------

def test_persistent_control_plane_trust_survives_restart(primary):
    r1 = LocalWorkerRunner(primary)
    WorkersRepo(r1._control_conn).register(
        worker_id=WORKER_ID, kind="fake", network_class="local",
    )
    _grant_guarded_via_conformance(r1._control_conn)
    assert WorkerTrustManager(r1._control_conn).current_trust(
        WORKER_ID, ROLE, "read_file",
    ) == TrustLevel.GUARDED
    r1.close()

    r2 = LocalWorkerRunner(primary)
    assert WorkerTrustManager(r2._control_conn).current_trust(
        WORKER_ID, ROLE, "read_file",
    ) == TrustLevel.GUARDED
    r2.close()


def test_job_execution_uses_control_plane_trust_without_copying_rows(guarded_runner):
    """A mutating run's job worktree gets its own separate state.db
    (Phase 7.5c); that database must never gain a copy of the
    control-plane's trust row."""
    prompt = "Refactor the module."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    result = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        requires_mutation=True, adapter=FakeWorkerAdapter([]),
    )
    # Denied (no mutation trust ever exists), but a job worktree was
    # still created per the required flow -- reopen its own database and
    # confirm no trust row exists there.
    row = RunnerRepo(guarded_runner._control_conn).get(result.run_id)
    assert row.job_worktree_path is not None
    job_conn = db_connect(location.db_path(row.repo_id, row.execution_worktree_id))
    try:
        count = job_conn.execute("SELECT count(*) FROM worker_trust_events").fetchone()[0]
        assert count == 0
    finally:
        job_conn.close()


# --- 16/17. exact capability scope; LOCKED denied ---------------------------

def test_exact_capability_scope_remains_enforced(runner):
    """GUARDED for read_file must never authorize write_file -- the
    unmodified Phase 7.2 exact-scope semantics still hold end to end."""
    _grant_guarded_via_conformance(runner._control_conn)
    assert WorkerTrustManager(runner._control_conn).current_trust(
        WORKER_ID, ROLE, "write_file",
    ) == TrustLevel.LOCKED

    prompt = "Refactor the module."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        requires_mutation=True, adapter=FakeWorkerAdapter([]),
    )
    assert result.status == RunStatus.DENIED_TRUST


def test_locked_capability_is_denied(runner):
    """No trust granted at all -- read-only run must be denied via the
    existing guarded execution path, never silently allowed."""
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    adapter = FakeWorkerAdapter([_read_call()])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        adapter=adapter,
    )
    assert result.status == RunStatus.DENIED_TRUST
    assert "trust_denied" in result.reason


# --- 18/19. qualified read_file reaches guarded ToolExecutor; exact output -

def test_qualified_read_file_reaches_existing_guarded_tool_executor_path(guarded_runner):
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    adapter = FakeWorkerAdapter([_read_call(), _text("ok")])
    result = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        adapter=adapter,
    )
    assert result.status == RunStatus.COMPLETED
    row = RunnerRepo(guarded_runner._control_conn).get(result.run_id)
    assert row.tool_operation_id is not None
    op = ToolOperationsRepo(guarded_runner._control_conn).get(row.tool_operation_id)
    assert op.tool_name == "read_file"
    assert op.status == "SUCCEEDED"


def test_exact_durable_read_output_reaches_continuation(guarded_runner):
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    adapter = FakeWorkerAdapter([_read_call(), _text("ok")])
    guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        adapter=adapter,
    )
    assert len(adapter.calls) == 2
    prior = adapter.calls[1].prior_tool_result
    assert prior is not None
    assert prior.output_summary == README_BYTES.decode()


# --- 20/21. completion survives restart; idempotent resume ------------------

def test_completion_survives_restart(primary):
    r1 = LocalWorkerRunner(primary)
    WorkersRepo(r1._control_conn).register(
        worker_id=WORKER_ID, kind="fake", network_class="local",
    )
    _grant_guarded_via_conformance(r1._control_conn)
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    adapter = FakeWorkerAdapter([_read_call(), _text("The README says hello.")])
    result = r1.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        adapter=adapter,
    )
    assert result.status == RunStatus.COMPLETED
    r1.close()

    r2 = LocalWorkerRunner(primary)
    status = r2.status(result.run_id)
    assert status.status == RunStatus.COMPLETED
    assert status.final_text == "The README says hello."
    r2.close()


def test_resume_of_completed_run_performs_zero_new_inference_or_tool_execution(guarded_runner):
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    adapter = FakeWorkerAdapter([_read_call(), _text("ok")])
    result = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        adapter=adapter,
    )
    assert result.status == RunStatus.COMPLETED
    ops_before = [
        dict(r) for r in guarded_runner._control_conn.execute("SELECT * FROM tool_operations")
    ]

    resumed = guarded_runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.COMPLETED
    assert len(adapter.calls) == 2  # no new calls
    ops_after = [
        dict(r) for r in guarded_runner._control_conn.execute("SELECT * FROM tool_operations")
    ]
    assert ops_after == ops_before


# --- 22/23/24. mutating policy -> job worktree; denial; primary untouched --

def test_mutating_policy_creates_a_managed_job_worktree(guarded_runner):
    prompt = "Refactor the module."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    result = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        requires_mutation=True, adapter=FakeWorkerAdapter([]),
    )
    row = RunnerRepo(guarded_runner._control_conn).get(result.run_id)
    assert row.job_worktree_path is not None
    from pathlib import Path

    assert Path(row.job_worktree_path).is_dir()
    assert row.execution_worktree_id != guarded_runner._primary.worktree_id


def test_mutation_without_exact_mutation_trust_is_denied(guarded_runner):
    """GUARDED for read_file only -- write_file trust does not exist, and
    no path in this codebase can currently grant it (workers.promotion
    refuses every mutating capability). Mutation must be denied."""
    prompt = "Refactor the module."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    result = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        requires_mutation=True, adapter=FakeWorkerAdapter([]),
    )
    assert result.status == RunStatus.DENIED_TRUST


def test_primary_repository_remains_untouched_by_mutating_denial(primary, guarded_runner):
    import os

    from tests.repo_helpers import filesystem_snapshot

    def _working_tree_snapshot(root):
        # Excludes `.git/` internals: creating a linked job worktree
        # legitimately adds Git's own bookkeeping under the primary's
        # `.git/worktrees/<id>/` (shared object/ref storage, Phase
        # 7.5c) -- what must stay byte-for-byte untouched is the
        # primary's actual working-tree content.
        return {
            path: value for path, value in filesystem_snapshot(root).items()
            if not path.startswith(".git" + os.sep) and path != ".git"
        }

    before = _working_tree_snapshot(primary)
    prompt = "Refactor the module."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        requires_mutation=True, adapter=FakeWorkerAdapter([]),
    )
    assert _working_tree_snapshot(primary) == before


# --- 25. resume does not create a duplicate job worktree --------------------

def test_resume_does_not_create_duplicate_job_worktree(guarded_runner):
    prompt = "Refactor the module."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    result = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        requires_mutation=True, adapter=FakeWorkerAdapter([]),
    )
    row_after_first = RunnerRepo(guarded_runner._control_conn).get(result.run_id)

    resumed = guarded_runner.resume(result.run_id, adapter=FakeWorkerAdapter([]))
    assert resumed.status == RunStatus.DENIED_TRUST
    row_after_resume = RunnerRepo(guarded_runner._control_conn).get(result.run_id)
    assert row_after_resume.job_worktree_path == row_after_first.job_worktree_path


# --- 26. stale/invalid lease remains safe -----------------------------------

def test_stale_lease_remains_safe(guarded_runner):
    """If another task/lease already occupies the primary worktree's one
    active-task slot (INV-2), the runner must never steal or bypass it
    unsafely -- it fails closed rather than proceeding, and the worker
    is never invoked."""
    from code_slayer.store.task_repo import TaskRepo

    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    ready = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    assert ready.status == RunStatus.READY

    # A foreign task/lease already occupies the primary worktree's one
    # active-task slot (INV-2: at most one non-terminal task per
    # worktree) -- created directly, before this run ever gets a chance
    # to set up its own execution plane.
    foreign_task = TaskRepo(guarded_runner._control_conn).create(
        description="pre-existing foreign task", repo_root=str(guarded_runner._primary.repo_root),
        repo_id=guarded_runner._primary.repo_id, worktree_id=guarded_runner._primary.worktree_id,
        config={"tool_policy": {"scope": ["."]}},
    )
    foreign = LeaseManager(guarded_runner._control_conn).acquire(
        worktree_id=guarded_runner._primary.worktree_id, task_id=foreign_task.task_id,
        worker_id="someone-else", worker_session_id="someone-elses-session",
    )
    assert foreign.decision.value == "ALLOW"

    adapter = FakeWorkerAdapter([_read_call(), _text("ok")])
    result = guarded_runner.resume(ready.run_id, adapter=adapter)
    assert result.status == RunStatus.INTERRUPTED_RESUMABLE
    assert "execution_plane_unavailable" in result.reason
    assert len(adapter.calls) == 0


def test_lease_held_by_a_different_owner_is_never_stolen(guarded_runner):
    """Once this run's own task/lease exist, a takeover by a different
    identity in between must be respected -- the runner's own
    acquire-or-renew logic denies rather than steals it."""
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    ready = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    completed = guarded_runner.resume(
        ready.run_id, adapter=FakeWorkerAdapter([_read_call(), _text("ok")]),
    )
    assert completed.status == RunStatus.COMPLETED

    # A different owner takes over this exact worktree's lease (its
    # generation/identity now disagrees with this run's own).
    row = RunnerRepo(guarded_runner._control_conn).get(ready.run_id)
    released = LeaseManager(guarded_runner._control_conn).release(
        LeaseHandle(
            row.execution_worktree_id, row.task_id, WORKER_ID, ready.run_id, 1,
            "2026-01-01T00:00:00.000000Z",
        ),
    )
    assert released.decision.value == "ALLOW"
    takeover = LeaseManager(guarded_runner._control_conn).acquire(
        worktree_id=row.execution_worktree_id, task_id=row.task_id,
        worker_id="someone-else", worker_session_id="someone-elses-session",
    )
    assert takeover.decision.value == "ALLOW"

    # Rewind this run back to RUNNING with no tool_operation_id, forcing
    # the "safe to retry" mid-turn-recovery path to re-attempt lease
    # acquisition against a worktree now genuinely owned elsewhere.
    with transaction(guarded_runner._control_conn):
        guarded_runner._control_conn.execute(
            "UPDATE runner_runs SET status = 'RUNNING', tool_operation_id = NULL WHERE run_id = ?",
            (ready.run_id,),
        )
    adapter = FakeWorkerAdapter([_text("should never be requested")])
    result = guarded_runner.resume(ready.run_id, adapter=adapter)
    assert result.status == RunStatus.INTERRUPTED_RESUMABLE
    assert "lease_unavailable" in result.reason
    assert len(adapter.calls) == 0


# --- 27. concurrent resume cannot execute one step twice --------------------

def test_concurrent_resume_cannot_execute_one_step_twice(primary):
    r_setup = LocalWorkerRunner(primary)
    WorkersRepo(r_setup._control_conn).register(
        worker_id=WORKER_ID, kind="fake", network_class="local",
    )
    _grant_guarded_via_conformance(r_setup._control_conn)
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    ready = r_setup.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    assert ready.status == RunStatus.READY
    r_setup.close()

    adapter_a = FakeWorkerAdapter([_read_call(), _text("from A")])
    adapter_b = FakeWorkerAdapter([_read_call(), _text("from B")])
    # Each thread opens its own LocalWorkerRunner (and so its own sqlite
    # connection) *inside* that thread -- a Python sqlite3 connection
    # object is only ever usable from the thread that created it, which
    # incidentally mirrors how two genuinely separate resume() processes
    # would each hold their own connection to the same database file.
    errors: list[BaseException] = []

    def _resume_a():
        try:
            r = LocalWorkerRunner(primary)
            try:
                r.resume(ready.run_id, adapter=adapter_a)
            finally:
                r.close()
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors` below
            errors.append(exc)

    def _resume_b():
        try:
            r = LocalWorkerRunner(primary)
            try:
                r.resume(ready.run_id, adapter=adapter_b)
            finally:
                r.close()
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors` below
            errors.append(exc)

    t1 = threading.Thread(target=_resume_a)
    t2 = threading.Thread(target=_resume_b)
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, errors
    calls_a = len(adapter_a.calls)
    calls_b = len(adapter_b.calls)
    # Exactly one of the two actually executed the bounded turn (two
    # calls: initial + continuation); the other saw the claim already
    # taken and made zero worker calls.
    assert {calls_a, calls_b} == {0, 2}

    final = LocalWorkerRunner(primary)
    final_status = final.status(ready.run_id)
    assert final_status.status == RunStatus.COMPLETED
    final.close()


# --- 28. safe mid-turn recovery: fail closed, never duplicate --------------

def test_mid_turn_crash_with_completed_tool_operation_fails_closed(guarded_runner):
    """Simulates a crash after ToolExecutor durably SUCCEEDED but before
    this run's own bookkeeping reached a terminal status: resume() must
    never re-invoke the worker/tool in that state -- it fails closed to
    INTERRUPTED_RESUMABLE instead of guessing or duplicating the effect."""
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    ready = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    assert ready.status == RunStatus.READY

    # Drive a real, successful bounded turn so a genuine tool_operation
    # exists, then rewind the run's own status back to RUNNING with that
    # operation id still attached -- exactly the durable state a crash
    # right after ToolExecutor succeeded, but before this run reached a
    # terminal status, would leave behind.
    adapter = FakeWorkerAdapter([_read_call(), _text("ok")])
    completed = guarded_runner.resume(ready.run_id, adapter=adapter)
    assert completed.status == RunStatus.COMPLETED
    row = RunnerRepo(guarded_runner._control_conn).get(ready.run_id)
    assert row.tool_operation_id is not None
    with transaction(guarded_runner._control_conn):
        guarded_runner._control_conn.execute(
            "UPDATE runner_runs SET status = 'RUNNING' WHERE run_id = ?", (ready.run_id,),
        )

    crash_adapter = FakeWorkerAdapter([_text("should never be requested")])
    result = guarded_runner.resume(ready.run_id, adapter=crash_adapter)
    assert result.status == RunStatus.INTERRUPTED_RESUMABLE
    assert result.reason == "mid_turn_recovery_not_supported_tool_already_executed"
    assert len(crash_adapter.calls) == 0  # never re-invoked


def test_mid_turn_crash_before_any_tool_execution_safely_retries(guarded_runner):
    """If the crash happened before the tool ever ran (no tool_operation_id
    recorded yet), retrying the whole bounded turn from scratch is safe."""
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    ready = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    claimed = guarded_runner.resume(ready.run_id, adapter=None)
    assert claimed.status == RunStatus.READY  # rolled back, no task set up yet since adapter=None

    # Manually claim RUNNING with no tool_operation_id, simulating a
    # crash before the execution plane was even set up.
    with transaction(guarded_runner._control_conn):
        guarded_runner._control_conn.execute(
            "UPDATE runner_runs SET status = 'RUNNING' WHERE run_id = ?", (ready.run_id,),
        )
    adapter = FakeWorkerAdapter([_read_call(), _text("recovered fine")])
    result = guarded_runner.resume(ready.run_id, adapter=adapter)
    assert result.status == RunStatus.COMPLETED
    assert result.final_text == "recovered fine"


def test_mid_turn_crash_with_unresolved_operation_fails_closed(guarded_runner):
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    ready = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    claimed = guarded_runner.resume(ready.run_id, adapter=None)
    assert claimed.status == RunStatus.READY
    # Force a task to exist by claiming with a real adapter that stalls
    # after tool execution is impossible to simulate deterministically
    # without a real crash; instead directly seed an unresolved
    # tool_operations row for a freshly created task via the runner's own
    # setup path, then mark the run RUNNING.
    row = RunnerRepo(guarded_runner._control_conn).get(ready.run_id)
    assert row.task_id is None
    from code_slayer.repo.baseline import InspectionService
    from code_slayer.store.task_repo import TaskRepo
    from code_slayer.store.tool_operations_repo import ToolOperationsRepo

    task = TaskRepo(guarded_runner._control_conn).create(
        description="x", repo_root=str(guarded_runner._primary.repo_root),
        repo_id=guarded_runner._primary.repo_id,
        worktree_id=guarded_runner._primary.worktree_id,
        task_id=ready.run_id, config={"tool_policy": {"scope": ["."]}},
    )
    service = InspectionService(
        guarded_runner._control_conn, blobs_dir=guarded_runner._control_blobs_dir,
    )
    service.start(task.task_id)
    service.capture(task.task_id)
    with transaction(guarded_runner._control_conn):
        ToolOperationsRepo(guarded_runner._control_conn).start_in_transaction(
            task_id=task.task_id, worktree_id=task.worktree_id, worker_id=WORKER_ID,
            worker_session_id=ready.run_id, tool_name="read_file", risk_class="READ_ONLY",
            request_hash="deadbeef", target_resource="README.md",
        )
        guarded_runner._control_conn.execute(
            "UPDATE runner_runs SET task_id = ?, execution_worktree_id = ?, status = 'RUNNING' "
            "WHERE run_id = ?",
            (task.task_id, task.worktree_id, ready.run_id),
        )
    adapter = FakeWorkerAdapter([_text("should never be requested")])
    result = guarded_runner.resume(ready.run_id, adapter=adapter)
    assert result.status == RunStatus.INTERRUPTED_RESUMABLE
    assert result.reason == "unresolved_tool_operation_requires_reconciliation"
    assert len(adapter.calls) == 0


# --- 29. audit chain / provenance remains valid ------------------------------

def test_audit_chain_remains_valid_across_a_full_run(guarded_runner):
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    adapter = FakeWorkerAdapter([_read_call(), _text("ok")])
    guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        adapter=adapter,
    )
    assert verify_chain(guarded_runner._control_conn, task_id=None).ok


def test_run_level_audit_events_recorded(guarded_runner):
    prompt = "Read the README.md file."
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)])
    adapter = FakeWorkerAdapter([_read_call(), _text("ok")])
    result = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        adapter=adapter,
    )
    rows = [dict(r) for r in guarded_runner._control_conn.execute(
        "SELECT event_type FROM audit_events WHERE task_id IS NULL ORDER BY seq",
    )]
    event_types = [r["event_type"] for r in rows]
    assert "RUN_STARTED" in event_types
    assert "PROMPT_ANALYSIS_RECORDED" in event_types
    assert "QUESTION_GATE_DECISION" in event_types
    assert "RUN_FINISHED" in event_types
    assert result.status == RunStatus.COMPLETED
