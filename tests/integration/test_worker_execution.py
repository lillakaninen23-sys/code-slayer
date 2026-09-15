"""End-to-end guarded worker tool execution (Phase 7.5a): a real,
structurally-validated `WorkerToolCall` wired through the real
`PolicyEngine`/`ToolExecutor` against real temporary Git repositories —
never a scratch driver, never a fabricated result. `FakeWorkerAdapter`
supplies the model side deterministically/offline; everything downstream
of `validate_response()` is the real, unmodified production stack."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from code_slayer.audit.verify import verify_chain
from code_slayer.core import TaskState, TaskStateMachine
from code_slayer.lease.manager import LeaseManager
from code_slayer.policy.engine import Decision
from code_slayer.repo import identity
from code_slayer.repo.baseline import InspectionService
from code_slayer.store.content_store import ContentStore
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.worker_trust_repo import TrustLevel
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.tools.executor import ToolExecutor
from code_slayer.workers.execution import execute_guarded_turn
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind, WorkerToolCall
from code_slayer.workers.trust import WorkerTrustManager
from tests.repo_helpers import acquire_lease

WORKER_ID = "test-guarded-worker"
ROLE = "coder"
README_BYTES = b"hello\n"  # matches conftest's git_repo_with_commit fixture


@pytest.fixture
def turn_context(db_conn, git_repo_with_commit, tmp_path):
    root = git_repo_with_commit
    info = identity.resolve(root)
    directory = tmp_path / "evidence"
    task = TaskRepo(db_conn).create(
        description="worker turn", repo_root=str(root),
        repo_id=info.repo_id, worktree_id=info.worktree_id,
        config={"tool_policy": {"scope": ["."]}},
    )
    service = InspectionService(db_conn, blobs_dir=directory)
    service.start(task.task_id)
    service.capture(task.task_id)
    machine = TaskStateMachine(db_conn)
    for to_state, expected in (
        (TaskState.PLANNING, TaskState.BASELINED),
        (TaskState.PLANNED, TaskState.PLANNING),
        (TaskState.IMPLEMENTING, TaskState.PLANNED),
    ):
        machine.transition(
            task.task_id, expected_state=expected, to_state=to_state, reason="progressing",
        )
    lease = acquire_lease(db_conn, task, worker_id=WORKER_ID, worker_session_id="turn-session")
    WorkersRepo(db_conn).register(worker_id=WORKER_ID, kind="fake", network_class="local")
    return root, task.task_id, lease, directory


def _grant_guarded(db_conn, *, worker_id=WORKER_ID, role=ROLE, capability="read_file"):
    result = WorkerTrustManager(db_conn).promote_to_guarded(
        worker_id=worker_id, role=role, capability=capability,
        reason="test_grant", evidence_ref="test-evidence",
    )
    assert result.ok, result.reason


def operations(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM tool_operations WHERE task_id = ? ORDER BY started_at", (task_id,),
    )]


def event_types(conn, task_id):
    return [
        r["event_type"] for r in conn.execute(
            "SELECT * FROM audit_events WHERE task_id = ? ORDER BY seq", (task_id,),
        )
    ]


def _read_call(path="README.md"):
    return WorkerResponse(
        kind=WorkerResponseKind.TOOL_CALL,
        tool_call=WorkerToolCall(tool="read_file", params={"path": path}),
    )


def _text(content="ok"):
    return WorkerResponse(kind=WorkerResponseKind.TEXT, text=content)


# --- 1/2/3/4. the full happy path -------------------------------------------

def test_valid_guarded_read_file_call_reaches_real_tool_executor(db_conn, turn_context):
    root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    adapter = FakeWorkerAdapter([_read_call(), _text("The README says hello.")])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert outcome.ok
    assert outcome.executed
    assert outcome.final_text == "The README says hello."
    assert outcome.tool_result is not None
    assert outcome.tool_result.status == "SUCCEEDED"


def test_real_tool_executor_returns_actual_file_evidence(db_conn, turn_context):
    _root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    adapter = FakeWorkerAdapter([_read_call(), _text("ok")])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert outcome.tool_result.output_hash == hashlib.sha256(README_BYTES).hexdigest()
    ops = operations(db_conn, task_id)
    assert len(ops) == 1
    assert ops[0]["status"] == "SUCCEEDED"
    assert ops[0]["tool_name"] == "read_file"
    assert ops[0]["worker_id"] == WORKER_ID  # the lease's own identity, real evidence


# --- Phase 7.5b: bound to exact ToolExecutor evidence, never a second read -

def test_worker_continuation_immune_to_post_read_file_mutation(db_conn, turn_context, monkeypatch):
    """The key TOCTOU acceptance test (Phase 7.5b): `ToolExecutor` reads
    content A; the repository file is then changed to content B *after*
    `ToolExecutor.execute()` has already finished, before the worker's
    continuation inference runs. The continuation must still receive
    EXACTLY content A from durable `ToolExecutor` evidence -- never B,
    and without this module ever reading the repository a second time."""
    root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    original_execute = ToolExecutor.execute

    def _execute_then_mutate_repo(self, task_id_, request):
        result = original_execute(self, task_id_, request)
        if request.tool == "read_file":
            # Simulate an external actor changing the repository file in
            # the window between ToolExecutor's own authorized read and
            # whatever the worker orchestration does next.
            (root / request.path).write_bytes(b"CONTENT B -- changed after the authorized read")
        return result

    monkeypatch.setattr(ToolExecutor, "execute", _execute_then_mutate_repo)
    adapter = FakeWorkerAdapter([_read_call(), _text("ok")])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert outcome.ok
    assert outcome.executed
    prior = adapter.calls[1].prior_tool_result
    assert prior is not None
    assert prior.output_summary == README_BYTES.decode()  # exactly content A
    assert "CONTENT B" not in prior.output_summary
    # The repository file really was changed -- proving the correct
    # result above did not come from (coincidentally) still-matching
    # repository state.
    assert (root / "README.md").read_bytes() == b"CONTENT B -- changed after the authorized read"


def test_missing_durable_evidence_fails_closed_without_rereading_repository(
    db_conn, turn_context, monkeypatch,
):
    root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    original_execute = ToolExecutor.execute

    def _execute_then_delete_evidence(self, task_id_, request):
        result = original_execute(self, task_id_, request)
        if request.tool == "read_file" and result.output_hash:
            blob_path = Path(directory) / result.output_hash[:2] / result.output_hash
            blob_path.chmod(0o644)
            blob_path.unlink()
        return result

    monkeypatch.setattr(ToolExecutor, "execute", _execute_then_delete_evidence)
    adapter = FakeWorkerAdapter([_read_call()])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert not outcome.ok
    assert outcome.reason == "evidence_verification_failed"
    assert outcome.executed
    assert outcome.tool_result is not None
    assert outcome.tool_result.status == "SUCCEEDED"  # the read itself genuinely succeeded
    # The repository file itself is untouched and still perfectly
    # readable -- this failure is solely about the missing durable
    # evidence, never about repository access, and no fallback read of
    # it was attempted to try to recover.
    assert (root / "README.md").read_bytes() == README_BYTES
    assert len(adapter.calls) == 1  # continuation never runs after evidence failure


def test_corrupted_durable_evidence_fails_closed(db_conn, turn_context, monkeypatch):
    root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    original_execute = ToolExecutor.execute

    def _execute_then_corrupt_evidence(self, task_id_, request):
        result = original_execute(self, task_id_, request)
        if request.tool == "read_file" and result.output_hash:
            blob_path = Path(directory) / result.output_hash[:2] / result.output_hash
            blob_path.chmod(0o644)
            blob_path.write_bytes(b"corrupted bytes that do not match the recorded hash")
            blob_path.chmod(0o444)
        return result

    monkeypatch.setattr(ToolExecutor, "execute", _execute_then_corrupt_evidence)
    adapter = FakeWorkerAdapter([_read_call()])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert not outcome.ok
    assert outcome.reason == "evidence_verification_failed"
    assert len(adapter.calls) == 1


def test_evidence_content_accepted_regardless_of_preexisting_blob_classification(
    db_conn, turn_context,
):
    """`evidence_content()` does not gate on `source_kind`: what
    establishes trust is that `expected_hash` itself came from the one
    real, already-authorized `ToolExecutor.execute()` call, not the
    label on whichever blob dedup happened to land on (see
    `test_read_file_reuses_preexisting_blob_under_a_different_
    classification`)."""
    from code_slayer.workers.execution import evidence_content

    _root, _task_id, _lease, directory = turn_context
    content = b"hello\n"
    content_hash = hashlib.sha256(content).hexdigest()
    ContentStore(db_conn, directory).put(
        content, media_type="application/octet-stream", source_kind="command_output",
        exportable=False,
    )
    assert evidence_content(db_conn, directory, content_hash) == content


def test_continuation_receives_the_real_tool_result(db_conn, turn_context):
    _root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    adapter = FakeWorkerAdapter([_read_call(), _text("ok")])
    execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert len(adapter.calls) == 2
    prior = adapter.calls[1].prior_tool_result
    assert prior is not None
    assert prior.tool == "read_file"
    assert "hello" in prior.output_summary  # the real file content, not a placeholder


def test_final_text_response_succeeds(db_conn, turn_context):
    _root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    adapter = FakeWorkerAdapter([_read_call(), _text("Final summary.")])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert outcome.ok
    assert outcome.final_text == "Final summary."


# --- 5. LOCKED worker cannot execute -----------------------------------

def test_locked_worker_cannot_execute_read_file(db_conn, turn_context):
    _root, task_id, lease, directory = turn_context
    # No trust granted -- stays LOCKED.
    adapter = FakeWorkerAdapter([_read_call()])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert not outcome.ok
    assert outcome.reason == "trust_denied:LOCKED"
    assert outcome.tool_result is None
    assert operations(db_conn, task_id) == []


# --- 6/7/8/10. exact-scope trust never generalizes ---------------------

def test_guarded_read_file_does_not_authorize_run_command(db_conn, turn_context):
    _root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)  # read_file only
    adapter = FakeWorkerAdapter([
        WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="run_command", params={}),
        ),
    ])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Run a command.", lease=lease, blobs_dir=directory,
    )
    assert not outcome.ok
    assert outcome.reason == "capability_not_offered"
    assert operations(db_conn, task_id) == []
    # And the exact-scope trust manager itself confirms run_command was
    # never touched by the read_file grant.
    assert WorkerTrustManager(db_conn).current_trust(
        WORKER_ID, ROLE, "run_command",
    ) == TrustLevel.LOCKED


def test_guarded_read_file_does_not_authorize_write_file(db_conn, turn_context):
    _root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    adapter = FakeWorkerAdapter([
        WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="write_file", params={"path": "x", "content": "y"}),
        ),
    ])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Write a file.", lease=lease, blobs_dir=directory,
    )
    assert not outcome.ok
    assert outcome.reason == "capability_not_offered"
    assert operations(db_conn, task_id) == []
    assert WorkerTrustManager(db_conn).current_trust(
        WORKER_ID, ROLE, "write_file",
    ) == TrustLevel.LOCKED


def test_valid_structured_but_untrusted_capability_never_reaches_executor(db_conn, turn_context):
    """Test item 8: a well-formed tool call for a capability this turn
    never even offers (checkpoint_create) is denied with zero executor
    invocation -- structurally the same guarantee as run_command/
    write_file above, exercised against a third capability."""
    _root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    adapter = FakeWorkerAdapter([
        WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="checkpoint_create", params={}),
        ),
    ])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Create a checkpoint.", lease=lease, blobs_dir=directory,
    )
    assert not outcome.ok
    assert outcome.reason == "capability_not_offered"
    assert operations(db_conn, task_id) == []


# --- 9. malformed textual leakage: zero execution, automatic downgrade -----

def test_malformed_textual_leakage_never_executes_and_downgrades_trust(db_conn, turn_context):
    _root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    leaked = "<function=read_file>\n<parameter=path>\nREADME.md\n</parameter>\n</function>"
    adapter = FakeWorkerAdapter([WorkerResponse(kind=WorkerResponseKind.TEXT, text=leaked)])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert not outcome.ok
    assert outcome.reason == "protocol_violation:textual_tool_protocol_leakage"
    assert operations(db_conn, task_id) == []
    assert outcome.downgraded is True
    assert WorkerTrustManager(db_conn).current_trust(
        WORKER_ID, ROLE, "read_file",
    ) == TrustLevel.LOCKED


def test_non_leakage_malformed_response_does_not_downgrade(db_conn, turn_context):
    """Only the specific textual_tool_protocol_leakage reason triggers an
    automatic downgrade -- an adapter-side malformed shape says nothing
    about the worker's own behavior."""
    _root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    adapter = FakeWorkerAdapter([WorkerResponse(kind=WorkerResponseKind.MALFORMED, error="broken")])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert not outcome.ok
    assert outcome.downgraded is False
    assert WorkerTrustManager(db_conn).current_trust(
        WORKER_ID, ROLE, "read_file",
    ) == TrustLevel.GUARDED


# --- 11. policy denial => no filesystem effect / no operation --------------

def test_policy_denial_outside_scope_never_executes(db_conn, git_repo_with_commit, tmp_path):
    root = git_repo_with_commit
    info = identity.resolve(root)
    directory = tmp_path / "evidence"
    task = TaskRepo(db_conn).create(
        description="scoped", repo_root=str(root), repo_id=info.repo_id,
        worktree_id=info.worktree_id, config={"tool_policy": {"scope": ["src"]}},
    )
    service = InspectionService(db_conn, blobs_dir=directory)
    service.start(task.task_id)
    service.capture(task.task_id)
    machine = TaskStateMachine(db_conn)
    for to_state, expected in (
        (TaskState.PLANNING, TaskState.BASELINED),
        (TaskState.PLANNED, TaskState.PLANNING),
        (TaskState.IMPLEMENTING, TaskState.PLANNED),
    ):
        machine.transition(
            task.task_id, expected_state=expected, to_state=to_state, reason="progressing",
        )
    lease = acquire_lease(db_conn, task, worker_id=WORKER_ID, worker_session_id="turn-session")
    WorkersRepo(db_conn).register(worker_id=WORKER_ID, kind="fake", network_class="local")
    _grant_guarded(db_conn)
    adapter = FakeWorkerAdapter([_read_call("README.md")])  # outside declared scope ["src"]
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task.task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert not outcome.ok
    assert outcome.reason == "execution_denied:outside_task_scope"
    assert (root / "README.md").read_bytes() == README_BYTES  # untouched
    assert operations(db_conn, task.task_id) == []


# --- 12. stale/invalid lease prevents execution -----------------------------

def test_stale_lease_prevents_execution(db_conn, turn_context):
    _root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    # Invalidate the handle by releasing it -- the exact same fencing
    # check ToolExecutor.execute() runs for every request, mutating or
    # not, must now refuse it.
    released = LeaseManager(db_conn).release(lease)
    assert released.decision == Decision.ALLOW, released.reason
    adapter = FakeWorkerAdapter([_read_call()])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert not outcome.ok
    assert outcome.reason == "execution_denied:stale_fencing_token"
    assert operations(db_conn, task_id) == []


# --- 13. second tool request after continuation is never executed ----------

def test_second_tool_request_in_continuation_is_not_executed(db_conn, turn_context):
    _root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    adapter = FakeWorkerAdapter([_read_call(), _read_call()])  # asks again in the continuation
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert not outcome.ok
    assert outcome.reason == "bounded_turn_limit_reached"
    assert outcome.executed  # the FIRST call did execute
    assert outcome.tool_result.status == "SUCCEEDED"
    ops = operations(db_conn, task_id)
    assert len(ops) == 1  # never a second operation


# --- 14. exact worker/role/capability scope enforced ------------------------

def test_trust_for_a_different_role_does_not_authorize_this_turn(db_conn, turn_context):
    _root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn, role="reviewer")  # NOT "coder"
    adapter = FakeWorkerAdapter([_read_call()])
    outcome = execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    assert not outcome.ok
    assert outcome.reason == "trust_denied:LOCKED"
    assert operations(db_conn, task_id) == []


# --- 15. existing ToolExecutor evidence/audit remains intact ---------------

def test_existing_tool_executor_audit_trail_is_unmodified(db_conn, turn_context):
    _root, task_id, lease, directory = turn_context
    _grant_guarded(db_conn)
    adapter = FakeWorkerAdapter([_read_call(), _text("ok")])
    execute_guarded_turn(
        db_conn, adapter, task_id=task_id, worker_id=WORKER_ID, role=ROLE,
        original_prompt="Inspect README.md.", lease=lease, blobs_dir=directory,
    )
    types = event_types(db_conn, task_id)
    # ToolExecutor's own four events, in order, exactly as it always
    # emits them -- unmodified by this phase's new event types, which
    # only ever appear alongside, never in place of, these.
    assert "TOOL_REQUESTED" in types
    assert "POLICY_EVALUATED" in types
    assert "OPERATION_STARTED" in types
    assert "OPERATION_FINISHED" in types
    tool_idx = types.index("TOOL_REQUESTED")
    assert types[tool_idx:tool_idx + 4] == [
        "TOOL_REQUESTED", "POLICY_EVALUATED", "OPERATION_STARTED", "OPERATION_FINISHED",
    ]
    assert "WORKER_TOOL_CALL_EVALUATED" in types
    assert "WORKER_TURN_FINISHED" in types
    assert verify_chain(db_conn, task_id=task_id).ok
