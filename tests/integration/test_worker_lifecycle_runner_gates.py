"""H.3 Stage 2: `LocalWorkerRunner` archived-worker execution gates.

Covers the two backend execution boundaries H.3 requires below the
HTTP layer: `start()` never creates a new run for an ARCHIVED worker,
and `resume()` never reaches worker/analyst inference for one, on
every durable run status individually -- a terminal run always stays
readable, and refusing a non-terminal run never mutates it (so a later
`resume()` after reactivation can still continue exactly where it left
off). See `runner.local_worker_runner`'s own `resume()`/`start()`
docstrings for the exact policy this proves.

Note on reachability: `workers.lifecycle.archive_worker()`'s own
active-work precheck already refuses to archive a worker with any
ANALYZING/READY/RUNNING run (see `test_worker_lifecycle_service.py`),
so "an ARCHIVED worker with a READY/RUNNING run" cannot normally arise
through the canonical archive path -- EXCEPT for READY, which the
BLOCKED_ON_QUESTIONS branch's own `adapter=None` re-evaluation can
legitimately create post-archive (see `test_resume_blocked_on_
questions_...transitions_to_ready_while_archived` below): a worker can
be archived while dormant in BLOCKED_ON_QUESTIONS, then a human answer
arrives and `resume(adapter=None)` (e.g. a plain page load) suppresses
the gate into READY without ever touching a worker -- so the READY
gate is proven both via that realistic sequence and via a direct
`WorkersRepo` bypass. The RUNNING gate has no such reachable sequence
(claiming into RUNNING with an adapter would already have been
refused by the READY gate one step earlier), so it is tested only via
a direct `WorkersRepo` bypass, documented there as pure defense-in-
depth for a state the canonical archive path itself already prevents
-- never a claim that this codebase can interrupt an inference already
genuinely in flight.
"""

from __future__ import annotations

import pytest

from code_slayer.runner import LocalWorkerRunner, RunStatus
from code_slayer.store.runner_repo import RunnerRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.conformance import run_conformance_suite
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.fake_prompt_analyst import FakePromptAnalyst
from code_slayer.workers.lifecycle import archive_worker, reactivate_worker
from code_slayer.workers.promotion import promote_from_conformance
from code_slayer.workers.prompt_analysis import (
    Ambiguity,
    AmbiguityRiskClass,
    EvidenceSource,
    PromptAnalysis,
)
from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind, WorkerToolCall
from code_slayer.workers.question_gate import ResolutionKind

WORKER_ID = "lifecycle-worker"
ROLE = "coder"
PROMPT = "Read the README.md file."


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


@pytest.fixture
def runner(git_repo_with_commit):
    r = LocalWorkerRunner(git_repo_with_commit)
    WorkersRepo(r._control_conn).register(worker_id=WORKER_ID, kind="fake", network_class="local")
    # Real, durable GUARDED trust for read_file, earned through the
    # actual conformance/promotion APIs -- needed for any scenario
    # below that reaches genuine COMPLETED execution.
    adapter = FakeWorkerAdapter(_passing_conformance_responses())
    suite = run_conformance_suite(r._control_conn, adapter, worker_id=WORKER_ID, role=ROLE)
    assert suite.ok and suite.status == "PASSED", suite
    result = promote_from_conformance(
        r._control_conn, worker_id=WORKER_ID, role=ROLE, capability="read_file",
        run_id=suite.run_id,
    )
    assert result.ok, result.reason
    yield r
    r.close()


def _read_call(path="README.md"):
    return WorkerResponse(
        kind=WorkerResponseKind.TOOL_CALL,
        tool_call=WorkerToolCall(tool="read_file", params={"path": path}),
    )


def _text(content="ok"):
    return WorkerResponse(kind=WorkerResponseKind.TEXT, text=content)


def _simple_analyst():
    return FakePromptAnalyst([PromptAnalysis(original_prompt=PROMPT)])


# -- start(): no run created at all for an ARCHIVED worker -------------------


def test_start_refuses_archived_worker_and_creates_no_run(runner):
    archive_worker(runner._control_conn, worker_id=WORKER_ID)
    result = runner.start(
        original_prompt=PROMPT, worker_id=WORKER_ID, role=ROLE, prompt_analyst=_simple_analyst(),
    )
    assert result.status == RunStatus.FAILED
    assert result.reason == "worker_archived"
    rows = RunnerRepo(runner._control_conn).list_for_worker(WORKER_ID)
    assert rows == []


def test_start_refuses_archived_worker_even_with_an_adapter_supplied(runner):
    """The archived gate is checked before the Prompt Analyst is even
    invoked, let alone the worker adapter."""
    archive_worker(runner._control_conn, worker_id=WORKER_ID)
    adapter = FakeWorkerAdapter([])  # any call raises
    result = runner.start(
        original_prompt=PROMPT, worker_id=WORKER_ID, role=ROLE,
        prompt_analyst=_simple_analyst(), adapter=adapter,
    )
    assert result.status == RunStatus.FAILED
    assert result.reason == "worker_archived"
    assert adapter.calls == ()


def test_start_succeeds_normally_after_reactivation(runner):
    archive_worker(runner._control_conn, worker_id=WORKER_ID)
    reactivate_worker(runner._control_conn, worker_id=WORKER_ID)
    result = runner.start(
        original_prompt=PROMPT, worker_id=WORKER_ID, role=ROLE, prompt_analyst=_simple_analyst(),
    )
    assert result.status == RunStatus.READY


def test_start_is_unaffected_for_an_unknown_worker(runner):
    """H.3 never changes the existing unknown-worker behavior at this
    layer -- only a REGISTERED, ARCHIVED worker is refused here."""
    result = runner.start(
        original_prompt=PROMPT, worker_id="never-registered", role=ROLE,
        prompt_analyst=_simple_analyst(),
    )
    assert result.status == RunStatus.READY
    assert result.reason != "worker_archived"


# -- resume(): terminal runs stay readable regardless of lifecycle ----------


def test_resume_of_a_completed_run_is_unaffected_by_archive(runner):
    result = runner.start(
        original_prompt=PROMPT, worker_id=WORKER_ID, role=ROLE, prompt_analyst=_simple_analyst(),
        adapter=FakeWorkerAdapter([_read_call(), _text("done")]),
    )
    assert result.status == RunStatus.COMPLETED
    archive_worker(runner._control_conn, worker_id=WORKER_ID)
    resumed = runner.resume(result.run_id, adapter=FakeWorkerAdapter([]))
    assert resumed.status == RunStatus.COMPLETED
    assert resumed.final_text == "done"


# -- resume(): BLOCKED_ON_QUESTIONS ------------------------------------------


def _blocked_run(runner):
    ambiguity = Ambiguity(
        id="scope", question="Which scope?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    analyst = FakePromptAnalyst([PromptAnalysis(original_prompt=PROMPT, ambiguities=(ambiguity,))])
    return runner.start(
        original_prompt=PROMPT, worker_id=WORKER_ID, role=ROLE, prompt_analyst=analyst,
    )


def test_resume_blocked_on_questions_without_adapter_is_allowed_even_if_archived(runner):
    """No worker/model call is possible without an adapter -- the gate
    only fires when `adapter is not None`."""
    result = _blocked_run(runner)
    assert result.status == RunStatus.BLOCKED_ON_QUESTIONS
    # BLOCKED_ON_QUESTIONS is not "active work" for archive purposes
    # (see the module docstring's reachability note), so this succeeds.
    archived = archive_worker(runner._control_conn, worker_id=WORKER_ID)
    assert archived.ok and archived.changed
    resumed = runner.resume(result.run_id)
    assert resumed.status == RunStatus.BLOCKED_ON_QUESTIONS
    assert resumed.reason != "worker_archived"


def test_resume_blocked_on_questions_with_adapter_refuses_when_archived(runner):
    result = _blocked_run(runner)
    archived = archive_worker(runner._control_conn, worker_id=WORKER_ID)
    assert archived.ok and archived.changed
    adapter = FakeWorkerAdapter([])
    resumed = runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.BLOCKED_ON_QUESTIONS
    assert resumed.reason == "worker_archived"
    assert adapter.calls == ()
    # The refusal never mutated the durable run.
    row = RunnerRepo(runner._control_conn).get(result.run_id)
    assert row.status == RunStatus.BLOCKED_ON_QUESTIONS.value
    assert row.reason != "worker_archived"


def test_resume_blocked_on_questions_transitions_to_ready_while_archived(runner):
    """The realistic, legitimately reachable sequence: a worker is
    archived while dormant on a question, a human answer arrives, and
    a plain `resume(adapter=None)` (e.g. a page load) suppresses the
    gate straight to READY -- without ever needing an adapter, so this
    is correctly allowed even though the worker is archived. This is
    exactly what the next test's READY-branch gate exists to catch."""
    result = _blocked_run(runner)
    archived = archive_worker(runner._control_conn, worker_id=WORKER_ID)
    assert archived.ok and archived.changed
    runner.record_user_resolution(
        result.run_id, "scope", "the whole repo",
        resolution_kind=ResolutionKind.FACT, source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    resumed = runner.resume(result.run_id)
    assert resumed.status == RunStatus.READY
    assert resumed.reason != "worker_archived"

    # And the resulting READY run, while archived, still refuses to
    # actually execute.
    adapter = FakeWorkerAdapter([])
    blocked = runner.resume(result.run_id, adapter=adapter)
    assert blocked.status == RunStatus.READY
    assert blocked.reason == "worker_archived"
    assert adapter.calls == ()

    reactivate_worker(runner._control_conn, worker_id=WORKER_ID)
    completed = runner.resume(
        result.run_id, adapter=FakeWorkerAdapter([_read_call(), _text("ok")]),
    )
    assert completed.status == RunStatus.COMPLETED


# -- resume(): READY (direct bypass, for the gate in isolation) -------------


def _ready_run(runner):
    return runner.start(
        original_prompt=PROMPT, worker_id=WORKER_ID, role=ROLE, prompt_analyst=_simple_analyst(),
    )


def test_resume_ready_with_adapter_refuses_when_archived_via_direct_bypass(runner):
    """`workers.lifecycle.archive_worker()`'s own active-work precheck
    refuses whenever a READY run exists (see the module docstring), so
    this uses `WorkersRepo.archive()` directly to construct the state
    -- proving `resume()`'s OWN gate holds independently of that
    precheck, not merely as a side effect of it."""
    result = _ready_run(runner)
    assert result.status == RunStatus.READY
    worker, changed = WorkersRepo(runner._control_conn).archive(WORKER_ID)
    assert changed and worker.lifecycle_state == "ARCHIVED"
    adapter = FakeWorkerAdapter([])
    resumed = runner.resume(result.run_id, adapter=adapter)
    assert resumed.status == RunStatus.READY
    assert resumed.reason == "worker_archived"
    assert adapter.calls == ()
    row = RunnerRepo(runner._control_conn).get(result.run_id)
    assert row.status == RunStatus.READY.value  # never claimed into RUNNING


def test_resume_ready_without_adapter_is_unaffected_by_archive(runner):
    result = _ready_run(runner)
    WorkersRepo(runner._control_conn).archive(WORKER_ID)
    resumed = runner.resume(result.run_id)
    assert resumed.status == RunStatus.READY
    assert resumed.reason != "worker_archived"


def test_resume_ready_completes_normally_after_reactivation(runner):
    result = _ready_run(runner)
    WorkersRepo(runner._control_conn).archive(WORKER_ID)
    WorkersRepo(runner._control_conn).reactivate(WORKER_ID)
    resumed = runner.resume(result.run_id, adapter=FakeWorkerAdapter([_read_call(), _text("ok")]))
    assert resumed.status == RunStatus.COMPLETED


# -- resume(): RUNNING (direct bypass -- pure defense-in-depth) -------------


def test_resume_running_with_adapter_refuses_when_archived_via_direct_bypass(runner):
    """No legitimate sequence through this codebase's own APIs leaves a
    RUNNING run for an archived worker (claiming into RUNNING with a
    real adapter would already have been refused by the READY gate one
    step earlier -- see the module docstring). This constructs the
    state directly to prove `resume()`'s RUNNING-branch gate holds on
    its own terms regardless -- defense-in-depth, not a claim that this
    codebase can interrupt an inference already genuinely in flight in
    another thread/process (it cannot, and does not attempt to)."""
    ready = _ready_run(runner)
    claimed = runner._claim_for_execution(ready.run_id)
    assert claimed is not None and claimed.status == RunStatus.RUNNING.value
    worker, changed = WorkersRepo(runner._control_conn).archive(WORKER_ID)
    assert changed and worker.lifecycle_state == "ARCHIVED"
    adapter = FakeWorkerAdapter([])
    resumed = runner.resume(ready.run_id, adapter=adapter)
    assert resumed.status == RunStatus.RUNNING
    assert resumed.reason == "worker_archived"
    assert adapter.calls == ()


def test_resume_running_without_adapter_is_unaffected_by_archive(runner):
    ready = _ready_run(runner)
    runner._claim_for_execution(ready.run_id)
    WorkersRepo(runner._control_conn).archive(WORKER_ID)
    resumed = runner.resume(ready.run_id)
    assert resumed.status == RunStatus.RUNNING
    assert resumed.reason != "worker_archived"


# -- archive()/reactivate() pass-through methods -----------------------------


def test_runner_archive_worker_pass_through(runner):
    result = runner.archive_worker(WORKER_ID)
    assert result.ok and result.changed
    assert WorkersRepo(runner._control_conn).get(WORKER_ID).lifecycle_state == "ARCHIVED"


def test_runner_reactivate_worker_pass_through(runner):
    runner.archive_worker(WORKER_ID)
    result = runner.reactivate_worker(WORKER_ID)
    assert result.ok and result.changed
    assert WorkersRepo(runner._control_conn).get(WORKER_ID).lifecycle_state == "ACTIVE"
