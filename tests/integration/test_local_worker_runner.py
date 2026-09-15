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

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from code_slayer.audit.verify import verify_chain
from code_slayer.lease.liveness import Liveness
from code_slayer.lease.manager import LeaseManager
from code_slayer.runner import LocalWorkerRunner, RunStatus
from code_slayer.store import location
from code_slayer.store.db import connect as db_connect
from code_slayer.store.db import transaction
from code_slayer.store.lease_repo import LeaseRepo
from code_slayer.store.runner_repo import RunnerRepo
from code_slayer.store.task_repo import TaskRepo
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
    ready = _ready(guarded_runner)
    _crash_attempt(guarded_runner, ready.run_id, "before_operation")
    row = RunnerRepo(guarded_runner._control_conn).get(ready.run_id)
    _age_lease(guarded_runner)
    takeover = LeaseManager(guarded_runner._control_conn).acquire(
        worktree_id=row.execution_worktree_id, task_id=row.task_id,
        worker_id="someone-else", worker_session_id="someone-elses-session",
    )
    assert takeover.decision.value == "ALLOW"
    before = LeaseRepo(guarded_runner._control_conn).get(row.execution_worktree_id)
    adapter = FakeWorkerAdapter([_text("should never be requested")])
    result = guarded_runner.resume(ready.run_id, adapter=adapter)
    assert result.status == RunStatus.RUNNING
    assert LeaseRepo(guarded_runner._control_conn).get(row.execution_worktree_id) == before
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

def _ready(runner):
    prompt = "Read the README.md file."
    return runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE,
        prompt_analyst=FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)]),
    )


_CRASH_ATTEMPT = """
import os, sys
sys.path.insert(0, sys.argv[1])
from code_slayer.runner import LocalWorkerRunner
from code_slayer.tools.executor import ToolExecutor
from code_slayer.audit.writer import AuditWriter
from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind, WorkerToolCall
stage = sys.argv[4]
if stage == "finalization":
    original_append = AuditWriter.append
    def append(self, **kwargs):
        record = original_append(self, **kwargs)
        if kwargs["event_type"] == "RUN_FINISHED":
            os._exit(74)
        return record
    AuditWriter.append = append
if stage == "started":
    def crash(*args, **kwargs):
        os._exit(74)
    ToolExecutor._file_effect = staticmethod(crash)
class Adapter:
    def infer(self, request):
        if stage == "before_operation":
            os._exit(74)
        if request.prior_tool_result is not None:
            if stage == "finalization":
                return WorkerResponse(kind=WorkerResponseKind.TEXT, text="done")
            os._exit(74)
        return WorkerResponse(kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="read_file", params={"path": "README.md"}))
r = LocalWorkerRunner(sys.argv[2])
r.resume(sys.argv[3], adapter=Adapter())
os._exit(1)
"""


def _crash_attempt(runner, run_id, stage):
    crashed = subprocess.run(
        [sys.executable, "-c", _CRASH_ATTEMPT,
         str(Path(__file__).resolve().parents[2] / "src"),
         str(runner._primary.repo_root), run_id, stage],
        capture_output=True, text=True, timeout=15,
    )
    assert crashed.returncode == 74, crashed.stderr
    row = RunnerRepo(runner._control_conn).get(run_id)
    assert row.status == "RUNNING"
    assert row.tool_operation_id is None
    assert TaskRepo(runner._control_conn).get(row.task_id).state == "IMPLEMENTING"
    return row


def _age_lease(runner):
    # Only advance the TTL prerequisite. Liveness still uses the real dead
    # subprocess's recorded PID AND /proc start time, never a fabricated owner.
    with transaction(runner._control_conn):
        runner._control_conn.execute(
            "UPDATE worker_leases SET heartbeat_at = '2000-01-01T00:00:00.000000Z'",
        )


@pytest.mark.parametrize("stage,op_status,reason", [
    ("succeeded", "SUCCEEDED", "mid_turn_recovery_not_supported_tool_already_executed"),
    ("started", "STARTED", "unresolved_tool_operation_requires_reconciliation"),
    ("finalization", "SUCCEEDED", "mid_turn_recovery_not_supported_tool_already_executed"),
])
def test_real_mid_turn_crash_never_replays(guarded_runner, stage, op_status, reason):
    ready = _ready(guarded_runner)
    row = _crash_attempt(guarded_runner, ready.run_id, stage)
    operations = ToolOperationsRepo(guarded_runner._control_conn).list_for_task(row.task_id)
    assert len(operations) == 1 and operations[0].status == op_status
    _age_lease(guarded_runner)
    # A fresh instance/connection has none of the crashed caller's Python state.
    restarted = LocalWorkerRunner(guarded_runner._primary.repo_root)
    try:
        adapter = FakeWorkerAdapter([_read_call(), _text("duplicate")])
        result = restarted.resume(ready.run_id, adapter=adapter)
        assert result.status == RunStatus.INTERRUPTED_RESUMABLE
        assert result.reason == reason
        assert not adapter.calls
        assert ToolOperationsRepo(restarted._control_conn).list_for_task(row.task_id) == operations
        lease = LeaseRepo(restarted._control_conn).get(row.execution_worktree_id)
        assert lease.generation == 2  # proven-gone takeover, never renewal/impersonation
        assert lease.status == "ACTIVE"  # unresolved run retains its reconciliation slot
        assert verify_chain(restarted._control_conn, task_id=row.task_id).ok
    finally:
        restarted.close()


def test_mid_turn_crash_before_any_tool_execution_safely_retries(guarded_runner):
    ready = _ready(guarded_runner)
    row = _crash_attempt(guarded_runner, ready.run_id, "before_operation")
    assert not ToolOperationsRepo(guarded_runner._control_conn).list_for_task(row.task_id)
    old = LeaseRepo(guarded_runner._control_conn).get(row.execution_worktree_id)
    unused = FakeWorkerAdapter([])
    # Process death alone does not bypass the established TTL prerequisite.
    assert guarded_runner.resume(ready.run_id, adapter=unused).status == RunStatus.RUNNING
    assert not unused.calls
    _age_lease(guarded_runner)
    adapter = FakeWorkerAdapter([_read_call(), _text("recovered fine")])
    result = guarded_runner.resume(ready.run_id, adapter=adapter)
    assert result.status == RunStatus.COMPLETED
    assert result.final_text == "recovered fine"
    new = LeaseRepo(guarded_runner._control_conn).get(row.execution_worktree_id)
    assert new.worker_session_id != old.worker_session_id
    assert new.generation == old.generation + 1
    assert len(ToolOperationsRepo(guarded_runner._control_conn).list_for_task(row.task_id)) == 1


@pytest.mark.parametrize("liveness", [Liveness.ALIVE, Liveness.UNKNOWN])
def test_runner_takeover_denied_without_proof_of_death(guarded_runner, monkeypatch, liveness):
    from code_slayer.runner import local_worker_runner as module

    ready = _ready(guarded_runner)
    row = _crash_attempt(guarded_runner, ready.run_id, "before_operation")
    _age_lease(guarded_runner)
    monkeypatch.setattr(module, "LeaseManager", lambda conn: LeaseManager(
        conn, liveness_fn=lambda *args: liveness,
    ))
    adapter = FakeWorkerAdapter([_read_call(), _text()])
    result = guarded_runner.resume(ready.run_id, adapter=adapter)
    assert result.status == RunStatus.RUNNING
    assert not adapter.calls
    lease = LeaseRepo(guarded_runner._control_conn).get(row.execution_worktree_id)
    assert lease.status == "QUIESCING"
    assert lease.generation == 1


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


@pytest.mark.parametrize("pause_at", ["claim", "setup", "inference"])
def test_resume_observes_live_owner_without_impersonating(primary, guarded_runner, pause_at):
    ready = _ready(guarded_runner)
    paused = threading.Event()
    proceed = threading.Event()
    results = []
    errors = []

    def pause():
        paused.set()
        assert proceed.wait(10)

    class PausingAdapter(FakeWorkerAdapter):
        def infer(self, request):
            if request.prior_tool_result is None and pause_at == "inference":
                pause()
            return super().infer(request)

    adapter_a = PausingAdapter([_read_call(), _text("A")])
    adapter_b = FakeWorkerAdapter([_read_call(), _text("B")])

    def first():
        r = LocalWorkerRunner(primary)
        try:
            if pause_at in ("claim", "setup"):
                original = r._setup_execution_plane

                def setup(run):
                    if pause_at == "claim":
                        pause()
                    execution = original(run)
                    if pause_at == "setup":
                        pause()
                    return execution
                r._setup_execution_plane = setup
            results.append(r.resume(ready.run_id, adapter=adapter_a))
        except BaseException as exc:
            errors.append(exc)
        finally:
            r.close()

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert paused.wait(10)
        before = LeaseRepo(guarded_runner._control_conn).get(guarded_runner._primary.worktree_id)
        if pause_at == "inference":
            assert before.worker_session_id != ready.run_id
        second = guarded_runner.resume(ready.run_id, adapter=adapter_b)
        assert second.status == RunStatus.RUNNING
        assert not adapter_b.calls
        assert LeaseRepo(guarded_runner._control_conn).get(
            guarded_runner._primary.worktree_id,
        ) == before  # no renewal, identity reconstruction or result overwrite
    finally:
        proceed.set()
        thread.join(10)
    assert not thread.is_alive()
    assert not errors
    assert results[0].status == RunStatus.COMPLETED
    assert len(adapter_a.calls) == 2
    assert len(ToolOperationsRepo(guarded_runner._control_conn).list_for_task(ready.run_id)) == 1
    assert verify_chain(guarded_runner._control_conn, task_id=None).ok
    assert verify_chain(guarded_runner._control_conn, task_id=ready.run_id).ok


@pytest.mark.parametrize("outcome", ["success", "failure", "denial", "mutation_denial"])
def test_terminal_runner_finalizes_task_and_lease(guarded_runner, outcome):
    from code_slayer.workers.protocol import WorkerAdapterError

    prompt = "Read the README.md file."
    responses = {
        "success": [_read_call(), _text()],
        "failure": [WorkerAdapterError("offline")],
        "denial": [_read_call()],
        "mutation_denial": [],
    }[outcome]
    if outcome == "denial":
        WorkerTrustManager(guarded_runner._control_conn).downgrade_to_locked(
            worker_id=WORKER_ID, role=ROLE, capability="read_file", reason="test denial",
        )
    result = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE,
        prompt_analyst=FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)]),
        requires_mutation=outcome == "mutation_denial", adapter=FakeWorkerAdapter(responses),
    )
    assert result.status == {
        "success": RunStatus.COMPLETED, "failure": RunStatus.FAILED,
        "denial": RunStatus.DENIED_TRUST, "mutation_denial": RunStatus.DENIED_TRUST,
    }[outcome]
    row = RunnerRepo(guarded_runner._control_conn).get(result.run_id)
    execution = guarded_runner._reopen_execution_plane(row)
    try:
        assert execution.task.state == ("COMPLETED" if outcome == "success" else "FAILED")
        assert TaskRepo(execution.conn).get_active_for_worktree(row.execution_worktree_id) is None
        lease = LeaseRepo(execution.conn).get(row.execution_worktree_id)
        assert lease.status == "RELEASED"
        assert verify_chain(execution.conn, task_id=row.task_id).ok
    finally:
        execution.close()
    # The terminal run is idempotent; the next ordinary run can use the slot.
    unused = FakeWorkerAdapter([])
    assert guarded_runner.resume(result.run_id, adapter=unused) == result
    assert not unused.calls
    next_run = _ready(guarded_runner)
    completed = guarded_runner.resume(next_run.run_id, adapter=FakeWorkerAdapter([_text("next")]))
    assert completed.status == RunStatus.COMPLETED
    assert verify_chain(guarded_runner._control_conn, task_id=None).ok


def test_unresolved_terminal_outcome_keeps_task_and_lease(guarded_runner, monkeypatch):
    from code_slayer.tools.executor import ToolExecutor

    original_finish = ToolExecutor._finish

    def unknown(self, task_id, operation_id, status, *args, **kwargs):
        return original_finish(self, task_id, operation_id, "UNKNOWN", *args, **kwargs)

    monkeypatch.setattr(ToolExecutor, "_finish", unknown)
    ready = _ready(guarded_runner)
    result = guarded_runner.resume(ready.run_id, adapter=FakeWorkerAdapter([_read_call()]))
    assert result.status == RunStatus.INTERRUPTED_RESUMABLE
    row = RunnerRepo(guarded_runner._control_conn).get(ready.run_id)
    assert TaskRepo(guarded_runner._control_conn).get(row.task_id).state == "IMPLEMENTING"
    assert LeaseRepo(guarded_runner._control_conn).get(row.execution_worktree_id).status == "ACTIVE"
    operation = ToolOperationsRepo(guarded_runner._control_conn).get(row.tool_operation_id)
    assert operation.status == "UNKNOWN"
    assert verify_chain(guarded_runner._control_conn, task_id=row.task_id).ok


@pytest.mark.parametrize("child_liveness", [Liveness.ALIVE, Liveness.UNKNOWN])
def test_runner_takeover_waits_for_recorded_child(guarded_runner, monkeypatch, child_liveness):
    import os

    from code_slayer.lease.liveness import process_start_time
    from code_slayer.runner import local_worker_runner as module

    ready = _ready(guarded_runner)
    row = _crash_attempt(guarded_runner, ready.run_id, "started")
    journal = ToolOperationsRepo(guarded_runner._control_conn)
    operation = journal.list_for_task(row.task_id)[0]
    journal.record_child_pid(operation.operation_id, os.getpid(), process_start_time(os.getpid()))
    _age_lease(guarded_runner)
    monkeypatch.setattr(module, "LeaseManager", lambda conn: LeaseManager(
        conn, child_liveness_fn=lambda *args: child_liveness,
    ))
    adapter = FakeWorkerAdapter([])
    assert guarded_runner.resume(ready.run_id, adapter=adapter).status == RunStatus.RUNNING
    assert not adapter.calls
    lease = LeaseRepo(guarded_runner._control_conn).get(row.execution_worktree_id)
    assert lease.status == "QUIESCING" and lease.generation == 1


def test_split_plane_finalization_crash_does_not_reoccupy_terminal_task(
    guarded_runner, monkeypatch,
):
    prompt = "Refactor the module."
    ready = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, requires_mutation=True,
        prompt_analyst=FakePromptAnalyst([PromptAnalysis(original_prompt=prompt)]),
    )
    original_finish = guarded_runner._finish

    def crash(*args, **kwargs):
        raise RuntimeError("crash between execution finalization and control bookkeeping")

    with monkeypatch.context() as patch:
        patch.setattr(guarded_runner, "_finish", crash)
        with pytest.raises(RuntimeError, match="crash between"):
            guarded_runner.resume(ready.run_id, adapter=FakeWorkerAdapter([]))
    row = RunnerRepo(guarded_runner._control_conn).get(ready.run_id)
    assert row.status == "RUNNING"
    execution = guarded_runner._reopen_execution_plane(row)
    try:
        assert execution.task.state == "FAILED"
        before = LeaseRepo(execution.conn).get(row.execution_worktree_id)
        assert before.status == "RELEASED"
        assert not ToolOperationsRepo(execution.conn).list_for_task(row.task_id)
    finally:
        execution.close()
    assert guarded_runner._finish == original_finish
    restarted = LocalWorkerRunner(guarded_runner._primary.repo_root)
    try:
        unused = FakeWorkerAdapter([])
        result = restarted.resume(ready.run_id, adapter=unused)
        assert result.status == RunStatus.INTERRUPTED_RESUMABLE
        assert result.reason == "terminal_execution_task_requires_reconciliation"
        assert not unused.calls
        execution = restarted._reopen_execution_plane(row)
        try:
            assert LeaseRepo(execution.conn).get(row.execution_worktree_id) == before
            assert verify_chain(execution.conn, task_id=row.task_id).ok
        finally:
            execution.close()
    finally:
        restarted.close()


# --- Phase 7.7d: durable human resolution -> verified worker context -------
#
# The audit finding this section closes: a human answer durably unblocked
# a run (QuestionGate SUPPRESS) but the worker itself only ever received
# the original, still-ambiguous prompt. These tests prove the answer's
# exact durable text -- not merely the fact that *some* resolution
# existed -- reaches the worker as structurally separate, provenance-
# preserving `WorkerSupplementalResolution` context, bound to the exact
# ambiguity it resolves, verified against durable content-store evidence
# before any inference call, and never derived from the Prompt Analyst's
# own advisory hints.

def test_fact_resolution_reaches_worker_as_supplemental_context(runner):
    prompt = "Refactor the ambiguous module."
    ambiguity = Ambiguity(
        id="target-module", question="Which module?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    assert result.status == RunStatus.BLOCKED_ON_QUESTIONS

    runner.record_user_resolution(
        result.run_id, "target-module", "billing.py",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    adapter = FakeWorkerAdapter([_text("done")])
    resumed = runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.COMPLETED
    assert len(adapter.calls) == 1

    request = adapter.calls[0]
    assert request.original_prompt == prompt  # byte-for-byte, never rewritten
    assert len(request.supplemental_resolutions) == 1
    supplemental = request.supplemental_resolutions[0]
    assert supplemental.ambiguity_id == "target-module"
    assert supplemental.content == "billing.py"  # the actual durable answer, not just its id
    assert supplemental.kind.value == "FACT"
    assert supplemental.source.value == "DURABLE_TASK_EVIDENCE"
    assert supplemental.content_hash  # provenance retained


def test_authorization_resolution_reaches_worker_with_kind_preserved(runner):
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
        result.run_id, "delete-scope", "Yes, delete permanently, I authorize it.",
        resolution_kind=ResolutionKind.AUTHORIZATION, source=EvidenceSource.ORIGINAL_PROMPT,
    )
    adapter = FakeWorkerAdapter([_text("done")])
    resumed = runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.COMPLETED
    supplemental = adapter.calls[0].supplemental_resolutions[0]
    assert supplemental.kind.value == "AUTHORIZATION"
    assert supplemental.content == "Yes, delete permanently, I authorize it."
    assert supplemental.source.value == "ORIGINAL_PROMPT"


def test_resolution_survives_full_close_reopen_and_resume(primary):
    prompt = "Refactor the ambiguous module."
    ambiguity = Ambiguity(
        id="target-module", question="Which module?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    r1 = LocalWorkerRunner(primary)
    WorkersRepo(r1._control_conn).register(worker_id=WORKER_ID, kind="fake", network_class="local")
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = r1.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    assert result.status == RunStatus.BLOCKED_ON_QUESTIONS
    r1.record_user_resolution(
        result.run_id, "target-module", "billing.py",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    r1.close()

    # A completely fresh runner/connection -- no Python object memory
    # from r1 survives.
    r2 = LocalWorkerRunner(primary)
    try:
        adapter = FakeWorkerAdapter([_text("done")])
        resumed = r2.resume(result.run_id, adapter=adapter)
        assert resumed.status == RunStatus.COMPLETED
        supplemental = adapter.calls[0].supplemental_resolutions[0]
        assert supplemental.content == "billing.py"
        assert supplemental.ambiguity_id == "target-module"
    finally:
        r2.close()


def test_missing_resolution_blob_fails_closed_before_worker_inference(runner):
    prompt = "Refactor the ambiguous module."
    ambiguity = Ambiguity(
        id="target-module", question="Which module?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    runner.record_user_resolution(
        result.run_id, "target-module", "billing.py",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    row = runner._control_conn.execute(
        "SELECT answer_content_hash FROM runner_human_resolutions WHERE run_id = ?",
        (result.run_id,),
    ).fetchone()
    content_hash = row["answer_content_hash"]
    blob_path = runner._control_blobs_dir / content_hash[:2] / content_hash
    blob_path.unlink()  # the metadata row still claims this evidence exists

    adapter = FakeWorkerAdapter([_text("should never be reached")])
    with pytest.raises(RuntimeError, match="unreadable"):
        runner.resume(result.run_id, adapter=adapter)
    assert not adapter.calls


def test_corrupt_resolution_blob_fails_closed_before_worker_inference(runner):
    prompt = "Refactor the ambiguous module."
    ambiguity = Ambiguity(
        id="target-module", question="Which module?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    runner.record_user_resolution(
        result.run_id, "target-module", "billing.py",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    row = runner._control_conn.execute(
        "SELECT answer_content_hash FROM runner_human_resolutions WHERE run_id = ?",
        (result.run_id,),
    ).fetchone()
    content_hash = row["answer_content_hash"]
    blob_path = runner._control_blobs_dir / content_hash[:2] / content_hash
    blob_path.chmod(0o644)
    blob_path.write_bytes(b"tampered content, not what was actually recorded")
    blob_path.chmod(0o444)

    adapter = FakeWorkerAdapter([_text("should never be reached")])
    with pytest.raises(RuntimeError, match="content hash mismatch"):
        runner.resume(result.run_id, adapter=adapter)
    assert not adapter.calls


def test_resolution_for_ambiguity_a_does_not_leak_into_ambiguity_b(runner):
    prompt = "Refactor and rename."
    amb_a = Ambiguity(
        id="a", question="Which module?", rationale="r", risk_class=AmbiguityRiskClass.MATERIAL,
    )
    amb_b = Ambiguity(
        id="b", question="Which new name?", rationale="r", risk_class=AmbiguityRiskClass.MATERIAL,
    )
    analyst = FakePromptAnalyst(
        [PromptAnalysis(original_prompt=prompt, ambiguities=(amb_a, amb_b))],
    )
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    assert result.status == RunStatus.BLOCKED_ON_QUESTIONS
    assert set(result.questions) == {amb_a.question, amb_b.question}

    runner.record_user_resolution(
        result.run_id, "a", "billing.py",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    still_blocked = runner.resume(result.run_id, adapter=FakeWorkerAdapter([]))
    assert still_blocked.status == RunStatus.BLOCKED_ON_QUESTIONS
    assert still_blocked.questions == (amb_b.question,)

    runner.record_user_resolution(
        result.run_id, "b", "new_billing.py",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    adapter = FakeWorkerAdapter([_text("done")])
    resumed = runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.COMPLETED
    by_id = {s.ambiguity_id: s.content for s in adapter.calls[0].supplemental_resolutions}
    assert by_id == {"a": "billing.py", "b": "new_billing.py"}  # never swapped/merged


def test_unrelated_ambiguity_id_resolution_is_not_forwarded(runner):
    prompt = "Refactor the module."
    ambiguity = Ambiguity(
        id="target-module", question="Which module?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    # A resolution for an ambiguity id that is not, and never was, part
    # of this run's own current analysis -- must never be forwarded.
    runner.record_user_resolution(
        result.run_id, "totally-unrelated-id", "should never appear anywhere",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    still_blocked = runner.resume(result.run_id, adapter=FakeWorkerAdapter([]))
    assert still_blocked.status == RunStatus.BLOCKED_ON_QUESTIONS  # the real ambiguity is untouched

    runner.record_user_resolution(
        result.run_id, "target-module", "billing.py",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    adapter = FakeWorkerAdapter([_text("done")])
    resumed = runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.COMPLETED
    ids = [s.ambiguity_id for s in adapter.calls[0].supplemental_resolutions]
    assert ids == ["target-module"]


def test_multiple_resolutions_ordered_by_ambiguity_order_not_insertion_order(runner):
    prompt = "Do two things."
    amb_a = Ambiguity(id="a", question="A?", rationale="r", risk_class=AmbiguityRiskClass.MATERIAL)
    amb_b = Ambiguity(id="b", question="B?", rationale="r", risk_class=AmbiguityRiskClass.MATERIAL)
    analyst = FakePromptAnalyst(
        [PromptAnalysis(original_prompt=prompt, ambiguities=(amb_a, amb_b))],
    )
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    # Recorded in reverse order (b before a) -- the output must still
    # follow PromptAnalysis.ambiguities' own deterministic order.
    runner.record_user_resolution(
        result.run_id, "b", "answer-b",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    runner.record_user_resolution(
        result.run_id, "a", "answer-a",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    adapter = FakeWorkerAdapter([_text("done")])
    resumed = runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.COMPLETED
    ordered_ids = [s.ambiguity_id for s in adapter.calls[0].supplemental_resolutions]
    assert ordered_ids == ["a", "b"]


def test_revised_resolution_answer_wins_over_earlier_one(runner):
    prompt = "Refactor the ambiguous module."
    ambiguity = Ambiguity(
        id="target-module", question="Which module?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    runner.record_user_resolution(
        result.run_id, "target-module", "first-guess.py",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    runner.record_user_resolution(
        result.run_id, "target-module", "billing.py",  # a human revising their own earlier answer
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    adapter = FakeWorkerAdapter([_text("done")])
    resumed = runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.COMPLETED
    supplemental = adapter.calls[0].supplemental_resolutions
    assert len(supplemental) == 1
    assert supplemental[0].content == "billing.py"  # RunnerRepo's own "most recent row wins" rule

    # Both attempts remain durably preserved -- append-only, never overwritten.
    raw_rows = runner._control_conn.execute(
        "SELECT answer_content_hash FROM runner_human_resolutions WHERE run_id = ? ORDER BY id",
        (result.run_id,),
    ).fetchall()
    assert len(raw_rows) == 2


def test_analyst_hint_never_becomes_supplemental_context(runner):
    prompt = "Delete the old database once the migration finishes."
    ambiguity = Ambiguity(
        id="which-database", question="Which database should be destroyed?",
        rationale="Destroying the wrong database is catastrophic.",
        risk_class=AmbiguityRiskClass.DESTRUCTIVE,
        resolved_by_prompt_substring="the old database",
        evidence_keys=("self-proposed-key",),
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    assert result.status == RunStatus.BLOCKED_ON_QUESTIONS  # the hint alone never suppresses

    runner.record_user_resolution(
        result.run_id, "which-database", "staging_db, I authorize deleting it",
        resolution_kind=ResolutionKind.AUTHORIZATION, source=EvidenceSource.ORIGINAL_PROMPT,
    )
    adapter = FakeWorkerAdapter([_text("done")])
    resumed = runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.COMPLETED
    supplemental = adapter.calls[0].supplemental_resolutions
    assert len(supplemental) == 1
    assert supplemental[0].content == "staging_db, I authorize deleting it"
    assert "self-proposed-key" not in supplemental[0].content
    assert supplemental[0].content != "the old database"


def test_authorization_resolution_does_not_grant_mutation_trust(guarded_runner):
    """The human AUTHORIZATION only ever becomes inert context data --
    it grants no trust, capability, or lease by itself. `guarded_runner`
    holds real, conformance-earned GUARDED trust for `read_file` only;
    `write_file`/mutation trust never exists, and an explicit human
    authorization for a completely different ambiguity must never
    change that."""
    prompt = "Delete old files."
    ambiguity = Ambiguity(
        id="delete-scope", question="Should this permanently delete files?",
        rationale="Irreversible.", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
        requires_mutation=True,
    )
    assert result.status == RunStatus.BLOCKED_ON_QUESTIONS
    guarded_runner.record_user_resolution(
        result.run_id, "delete-scope", "Yes, I authorize permanent deletion.",
        resolution_kind=ResolutionKind.AUTHORIZATION, source=EvidenceSource.ORIGINAL_PROMPT,
    )
    adapter = FakeWorkerAdapter([])
    resumed = guarded_runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.DENIED_TRUST
    assert not adapter.calls
    assert WorkerTrustManager(guarded_runner._control_conn).current_trust(
        WORKER_ID, ROLE, "write_file",
    ) == TrustLevel.LOCKED


def test_supplemental_context_does_not_bypass_policy_engine(guarded_runner):
    """A human FACT resolution claiming an out-of-scope/absolute path is
    still just context data for the model to (mis)use -- `PolicyEngine`/
    `ToolExecutor` still independently refuse the actual tool call."""
    prompt = "Read a file."
    ambiguity = Ambiguity(
        id="path-choice", question="Which file?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = guarded_runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    guarded_runner.record_user_resolution(
        result.run_id, "path-choice", "/etc/passwd",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    adapter = FakeWorkerAdapter([_read_call(path="/etc/passwd")])
    resumed = guarded_runner.resume(result.run_id, adapter=adapter)
    assert resumed.status != RunStatus.COMPLETED  # the absolute path is still refused


def test_resume_of_completed_resolved_run_does_not_repeat_inference(runner):
    prompt = "Refactor the ambiguous module."
    ambiguity = Ambiguity(
        id="target-module", question="Which module?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    runner.record_user_resolution(
        result.run_id, "target-module", "billing.py",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    adapter = FakeWorkerAdapter([_text("done")])
    resumed = runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.COMPLETED
    assert len(adapter.calls) == 1

    resumed_again = runner.resume(result.run_id, adapter=adapter)
    assert resumed_again.status == RunStatus.COMPLETED
    assert len(adapter.calls) == 1  # idempotent -- no new inference


def test_audit_records_which_resolution_ids_informed_the_turn(runner):
    prompt = "Refactor the ambiguous module."
    ambiguity = Ambiguity(
        id="target-module", question="Which module?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=prompt, ambiguities=(ambiguity,))])
    result = runner.start(
        original_prompt=prompt, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )
    runner.record_user_resolution(
        result.run_id, "target-module", "billing.py",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    adapter = FakeWorkerAdapter([_text("done")])
    resumed = runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.COMPLETED

    import json as _json

    row = runner._control_conn.execute(
        "SELECT payload_json FROM audit_events WHERE event_type = 'WORKER_TOOL_CALL_EVALUATED' "
        "ORDER BY id LIMIT 1",
    ).fetchone()
    payload = _json.loads(row["payload_json"])
    assert payload["supplemental_resolution_ambiguity_ids"] == ["target-module"]
    assert len(payload["supplemental_resolution_content_hashes"]) == 1
    assert payload["supplemental_resolution_content_hashes"][0]  # a real, non-empty hash
    # No raw answer text ever lands in the audit payload -- hash/id only.
    assert "billing.py" not in _json.dumps(payload)
