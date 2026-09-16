"""End-to-end `Finalizer`/`checkpointed_completion_guard` behavior against
real temporary Git repos, a real SQLite state database, and real (fast,
deterministic) subprocess execution of the fixed verification-command set.

Covers: no groundable command -> `BLOCKED`/`INVALID_ENVIRONMENT` with a
correct `VERIFYING` resume target; a deterministically failing command ->
`REPAIRING`; all commands passing -> directly to `READY_FOR_CHECKPOINT`
(review_required = false in this phase/version -- REVIEWING is never a
pretend pass-through); the `CHECKPOINTED -> COMPLETED` guard allowing
completion only when a real checkpoint, an unsuperseded `VERIFIED` record,
AND a matching Git tree id between them all exist (closing the
verify-A/checkpoint-B TOCTOU gap), and refusing it with a distinct reason
for each missing piece of evidence, while leaving `bounded_read_only_turn`
untouched; lease fencing; wrong-state fail-closed behavior; repair-attempt
bound enforcement; and (the isolated-verification-tree hardening) that
unowned tracked modifications, untracked files, and git-ignored files in
the live repository cannot influence verification commands, which execute
against a materialized checkout of `verified_tree_sha` instead of
`repo_root`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_slayer.audit.writer import AuditWriter
from code_slayer.core import InvalidResumeTarget, InvalidTransition, TaskState, TaskStateMachine
from code_slayer.finalization.service import (
    DEFAULT_MAX_REPAIR_ATTEMPTS,
    Finalizer,
    checkpointed_completion_guard,
)
from code_slayer.finalization.types import FinalizerVerdict
from code_slayer.finalization.verification import FinalizationError
from code_slayer.lease.manager import LeaseHandle
from code_slayer.policy.engine import Decision
from code_slayer.repo import checkpoint_git as cg
from code_slayer.repo import identity
from code_slayer.repo.baseline import InspectionService
from code_slayer.repo.checkpoint import CheckpointManager
from code_slayer.store.checkpoint_repo import CheckpointRepo, parse_verified
from code_slayer.store.db import transaction
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.tool_operations_repo import OperationStatus, ToolOperationsRepo
from code_slayer.tools import file_tools as files
from code_slayer.tools.executor import ToolExecutor
from code_slayer.tools.models import ToolRequest
from tests.repo_helpers import acquire_lease, commit


def _advance(machine, task_id, *pairs):
    for expected, to_state in pairs:
        machine.transition(task_id, expected_state=expected, to_state=to_state, reason="progress")


def _new_task(db_conn, root, blobs_dir, *, pyproject_toml=None, extra_baseline_files=None):
    # Every baseline file (config or otherwise) must be committed BEFORE
    # `service.capture()` -- committing anything afterward would move live
    # HEAD past the already-captured baseline, which both the Finalizer's
    # own `baseline_valid` check and `ToolExecutor`'s mutation-time
    # `verify_identity()` correctly treat as external drift and refuse.
    baseline_files = dict(extra_baseline_files or {})
    if pyproject_toml is not None:
        baseline_files["pyproject.toml"] = pyproject_toml
    if baseline_files:
        for rel_path, content in baseline_files.items():
            full = root / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, str):
                full.write_text(content)
            else:
                full.write_bytes(content)
        commit(root, "add verification config/baseline files")
    info = identity.resolve(root)
    task = TaskRepo(db_conn).create(
        description="finalization flow", repo_root=str(root), repo_id=info.repo_id,
        worktree_id=info.worktree_id, config={"tool_policy": {"scope": ["."]}},
    )
    service = InspectionService(db_conn, blobs_dir=blobs_dir)
    service.start(task.task_id)
    service.capture(task.task_id)
    return task


def _build(
    db_conn, root, blobs_dir, *, pyproject_toml="[tool.ruff]\n", extra_baseline_files=None,
    stop_at_verifying,
):
    task = _new_task(
        db_conn, root, blobs_dir, pyproject_toml=pyproject_toml,
        extra_baseline_files=extra_baseline_files,
    )
    machine = TaskStateMachine(db_conn)
    pairs = [
        (TaskState.BASELINED, TaskState.PLANNING),
        (TaskState.PLANNING, TaskState.PLANNED),
        (TaskState.PLANNED, TaskState.IMPLEMENTING),
    ]
    if stop_at_verifying:
        pairs.append((TaskState.IMPLEMENTING, TaskState.VERIFYING))
    _advance(machine, task.task_id, *pairs)
    lease = acquire_lease(db_conn, task)
    return root, task.task_id, blobs_dir, lease


@pytest.fixture
def context(db_conn, git_repo_with_commit, tmp_path):
    """A task already at `VERIFYING`, no owned content, with a baseline-
    committed `pyproject.toml` (`[tool.ruff]`) already in place --
    representing a project that already had lint configured before this
    task started."""
    return _build(db_conn, git_repo_with_commit, tmp_path / "evidence", stop_at_verifying=True)


@pytest.fixture
def implementing_context(db_conn, git_repo_with_commit, tmp_path):
    """Same as `context`, but stopped at `IMPLEMENTING` so a test can own
    real content (via `ToolExecutor`) before advancing to `VERIFYING`
    itself -- needed for the content-fingerprint-binding and isolation
    tests, which must verify *real*, checkpoint-checkable owned content."""
    return _build(db_conn, git_repo_with_commit, tmp_path / "evidence", stop_at_verifying=False)


@pytest.fixture
def bare_context(db_conn, git_repo_with_commit, tmp_path):
    """Like `context`, but with NO verification config committed at all --
    used only by tests that specifically require zero groundable
    commands."""
    return _build(
        db_conn, git_repo_with_commit, tmp_path / "evidence",
        pyproject_toml=None, stop_at_verifying=True,
    )


def make_finalizer(db_conn, blobs_dir) -> Finalizer:
    return Finalizer(
        db_conn, blobs_dir=blobs_dir, tmp_dir=Path(blobs_dir).parent / "finalizer-tmp",
    )


def own_file(executor, task_id, path, content):
    result = executor.execute(task_id, ToolRequest(tool="create_file", path=path, content=content))
    assert result.status == OperationStatus.SUCCEEDED
    return result


def rewrite_file(executor, task_id, path, content, expected_hash):
    result = executor.execute(
        task_id,
        ToolRequest(tool="write_file", path=path, content=content, expected_hash=expected_hash),
    )
    assert result.status == OperationStatus.SUCCEEDED
    return result


def events(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM audit_events WHERE task_id = ? ORDER BY seq", (task_id,),
    )]


def finalization_payloads(conn, task_id):
    return [
        json.loads(e["payload_json"]) for e in events(conn, task_id)
        if e["event_type"] == "FINALIZATION_DECIDED"
    ]


def state_transition_targets(conn, task_id):
    return [
        json.loads(e["payload_json"])["to_state"] for e in events(conn, task_id)
        if e["event_type"] == "STATE_TRANSITION"
    ]


def transition_to_verifying(db_conn, task_id):
    TaskStateMachine(db_conn).transition(
        task_id, expected_state=TaskState.IMPLEMENTING, to_state=TaskState.VERIFYING, reason="p",
    )


# --- no groundable verification command -> INVALID_ENVIRONMENT/BLOCKED -----

def test_no_verification_commands_blocks_with_invalid_environment_reason(bare_context, db_conn):
    root, task_id, blobs_dir, lease = bare_context
    finalizer = make_finalizer(db_conn, blobs_dir)

    decision = finalizer.decide_after_verification(task_id, lease)

    assert decision.verdict == FinalizerVerdict.INVALID_ENVIRONMENT
    assert decision.reason_code == "no_verification_commands_grounded"
    assert decision.target_state == TaskState.BLOCKED

    task = TaskRepo(db_conn).get(task_id)
    assert task.state == "BLOCKED"
    # Correct resume_target (approved amendment #1): entering BLOCKED
    # durably records its origin via the existing suspend/resume
    # mechanics -- no parallel state machine, no new TaskState.
    assert task.current_phase == "VERIFYING"

    payloads = finalization_payloads(db_conn, task_id)
    assert len(payloads) == 1
    assert payloads[0]["verdict"] == "INVALID_ENVIRONMENT"
    assert payloads[0]["reason_code"] == "no_verification_commands_grounded"
    assert payloads[0]["verification"] == []


def test_blocked_task_resumes_only_back_to_verifying(bare_context, db_conn):
    root, task_id, blobs_dir, lease = bare_context
    make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)
    machine = TaskStateMachine(db_conn)

    with pytest.raises(InvalidResumeTarget):
        machine.transition(
            task_id, expected_state=TaskState.BLOCKED, to_state=TaskState.REPAIRING,
            reason="wrong resume target", reconciled_target=TaskState.REPAIRING,
        )

    machine.transition(
        task_id, expected_state=TaskState.BLOCKED, to_state=TaskState.VERIFYING,
        reason="environment fixed", reconciled_target=TaskState.VERIFYING,
    )
    assert TaskRepo(db_conn).get(task_id).state == "VERIFYING"


# --- a deterministically failing command -> REPAIR_REQUIRED ----------------

def test_failing_verification_command_requires_repair(implementing_context, db_conn):
    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "bad.py", b"import os\n")  # unused import -- ruff fails
    transition_to_verifying(db_conn, task_id)

    decision = make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)

    assert decision.verdict == FinalizerVerdict.REPAIR_REQUIRED
    assert decision.reason_code == "verification_command_failed:ruff check ."
    assert TaskRepo(db_conn).get(task_id).state == "REPAIRING"
    payload = finalization_payloads(db_conn, task_id)[0]
    assert payload["verification"][0]["command"] == "ruff check ."
    assert payload["verification"][0]["status"] == "FAILED"


def test_repair_attempts_exhausted_blocks_instead_of_looping_forever(implementing_context, db_conn):
    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "bad.py", b"import os\n")
    transition_to_verifying(db_conn, task_id)
    machine = TaskStateMachine(db_conn)
    finalizer = make_finalizer(db_conn, blobs_dir)

    for _ in range(DEFAULT_MAX_REPAIR_ATTEMPTS):
        decision = finalizer.decide_after_verification(task_id, lease, max_repair_attempts=1)
        assert decision.verdict in (FinalizerVerdict.REPAIR_REQUIRED, FinalizerVerdict.BLOCKED)
        if decision.verdict == FinalizerVerdict.BLOCKED:
            break
        machine.transition(
            task_id, expected_state=TaskState.REPAIRING, to_state=TaskState.VERIFYING,
            reason="repair attempt made (no-op in this test)",
        )
    else:
        pytest.fail("never reached the repair bound")

    assert decision.reason_code == "repair_attempts_exhausted"
    assert TaskRepo(db_conn).get(task_id).state == "BLOCKED"


# --- review_required = false: REVIEWING is never a pretend pass-through ----

def test_no_reviewer_skips_reviewing_state_entirely(implementing_context, db_conn):
    """Item #3.A: no reviewer exists in this patch -- the transition
    history must never include a REVIEWING hop for a plain verification
    pass."""
    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "clean.py", b"VALUE = 1\n")
    transition_to_verifying(db_conn, task_id)

    decision = make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)

    assert decision.verdict == FinalizerVerdict.VERIFIED
    assert "REVIEWING" not in state_transition_targets(db_conn, task_id)


def test_verification_pass_reaches_ready_for_checkpoint_directly(implementing_context, db_conn):
    """Item #3.B: the finalizer's own transition record goes straight
    VERIFYING -> READY_FOR_CHECKPOINT -- exactly one STATE_TRANSITION for
    this decision, never a two-hop pretend review."""
    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "clean.py", b"VALUE = 1\n")
    transition_to_verifying(db_conn, task_id)

    make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)

    assert TaskRepo(db_conn).get(task_id).state == "READY_FOR_CHECKPOINT"
    transitions = [
        json.loads(e["payload_json"]) for e in events(db_conn, task_id)
        if e["event_type"] == "STATE_TRANSITION"
    ]
    last = transitions[-1]
    assert last["from_state"] == "VERIFYING"
    assert last["to_state"] == "READY_FOR_CHECKPOINT"


def test_no_review_events_recorded_when_no_reviewer_exists(implementing_context, db_conn):
    """Item #3.C."""
    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "clean.py", b"VALUE = 1\n")
    transition_to_verifying(db_conn, task_id)

    decision = make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)
    assert decision.verdict == FinalizerVerdict.VERIFIED  # otherwise this test proves nothing

    recorded_types = {e["event_type"] for e in events(db_conn, task_id)}
    assert "REVIEW_STARTED" not in recorded_types
    assert "REVIEW_FINDING" not in recorded_types


# --- CHECKPOINTED -> COMPLETED guard: checkpoint + verification evidence ---

def test_completion_guard_blocks_without_prior_verification_evidence(context, db_conn, tmp_path):
    """A task manually driven straight to CHECKPOINTED without ever going
    through the Finalizer (mirroring `test_checkpoint_manager.py`'s own
    `ready_for_checkpoint` helper) must never be allowed to become
    COMPLETED -- there is no durable evidence verification ever ran."""
    root, task_id, blobs_dir, lease = context
    machine = TaskStateMachine(db_conn)
    _advance(machine, task_id,
              (TaskState.VERIFYING, TaskState.REVIEWING),
              (TaskState.REVIEWING, TaskState.READY_FOR_CHECKPOINT))
    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_path / "tmp", lease=lease)
    result = manager.create(task_id)
    assert result.decision == Decision.ALLOW
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"

    guarded = TaskStateMachine(db_conn, guards=(checkpointed_completion_guard(db_conn),))
    with pytest.raises(InvalidTransition, match="no_verification_evidence"):
        guarded.transition(
            task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.COMPLETED,
            reason="attempted without evidence", completion_decision=True,
        )
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"


def test_completion_guard_never_vetoes_bounded_read_only_completion(db_conn, git_repo_with_commit):
    """The guard only ever applies to a task actually in CHECKPOINTED --
    the `bounded_read_only_turn` shortcut (`IMPLEMENTING -> COMPLETED`)
    must remain completely unaffected."""
    root = git_repo_with_commit
    info = identity.resolve(root)
    task = TaskRepo(db_conn).create(
        description="read-only turn", repo_root=str(root), repo_id=info.repo_id,
        worktree_id=info.worktree_id,
        config={"tool_policy": {"scope": ["."]}, "execution_kind": "bounded_read_only_turn"},
    )
    machine = TaskStateMachine(db_conn, guards=(checkpointed_completion_guard(db_conn),))
    machine.transition(
        task.task_id, expected_state=TaskState.CREATED, to_state=TaskState.INSPECTING,
        reason="p",
    )
    # Directly force IMPLEMENTING via the low-level repo (bypassing the
    # ordinary graph) purely to reach the exact shortcut edge under test --
    # mirrors how `core.transitions`'s own read_only_completion special
    # case is tested elsewhere.
    with transaction(db_conn):
        TaskRepo(db_conn)._record_transition_in_transaction(
            task.task_id, to_state="IMPLEMENTING", to_phase="IMPLEMENTING", reason="setup",
        )
    machine.transition(
        task.task_id, expected_state=TaskState.IMPLEMENTING, to_state=TaskState.COMPLETED,
        reason="bounded read-only turn done", completion_decision=True,
    )
    assert TaskRepo(db_conn).get(task.task_id).state == "COMPLETED"


# --- TOCTOU closure: VERIFIED is bound to exactly the checkpointed content -

def test_verify_a_checkpoint_a_completed_allowed(implementing_context, db_conn, tmp_path):
    """Item #2.A / mandatory test F: verifying content A and checkpointing
    that exact same content allows COMPLETED."""
    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "x.txt", b"content A")
    transition_to_verifying(db_conn, task_id)
    decision = make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)
    assert decision.verdict == FinalizerVerdict.VERIFIED

    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_path / "tmp", lease=lease)
    result = manager.create(task_id)
    assert result.decision == Decision.ALLOW
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"

    guarded = TaskStateMachine(db_conn, guards=(checkpointed_completion_guard(db_conn),))
    guarded.transition(
        task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.COMPLETED,
        reason="finalized", completion_decision=True,
    )
    assert TaskRepo(db_conn).get(task_id).state == "COMPLETED"


def test_adversarial_verify_a_then_content_changes_to_b_then_real_checkpoint_b_denies_completion(
    implementing_context, db_conn, tmp_path,
):
    """MANDATORY adversarial test (explicit requirement): same base_head,
    same owned_paths (path name), only the file's *content* differs
    between what was verified and what a REAL checkpoint later commits.

    1. baseline H0 (via `implementing_context`)
    2. owned path `src/foo.py`
    3. write CONTENT_A
    4. verification succeeds, VERIFIED evidence is durably created
    5. the SAME path's content changes to CONTENT_B
    6. a REAL checkpoint is created for CONTENT_B
    7. attempt CHECKPOINTED -> COMPLETED
    8. the transition MUST be denied with a content mismatch

    Step 5 is simulated as durable evidence recorded without ever going
    through `TaskStateMachine` (no `STATE_TRANSITION` event at all) --
    the realistic residual gap this mechanism exists to guard against: a
    bug or out-of-band process that updates recorded ownership evidence
    without the ordinary state-transition gate ever firing. Under the
    current state graph, content can only legitimately change while
    `IMPLEMENTING`/`REPAIRING`, and re-entering either always trips the
    separate temporal check (`prior_verification_confirmed`) on its own
    -- this test isolates the CONTENT-identity check specifically, proving
    it independently closes the gap even when the temporal check alone
    would not have fired.
    """
    content_a = b"VALUE = 'A'\n"
    content_b = b"VALUE = 'B'\n"
    root, task_id, blobs_dir, lease = implementing_context
    (root / "src").mkdir()
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "src/foo.py", content_a)
    transition_to_verifying(db_conn, task_id)
    decision = make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)
    assert decision.verdict == FinalizerVerdict.VERIFIED
    assert TaskRepo(db_conn).get(task_id).state == "READY_FOR_CHECKPOINT"
    verified_a_tree_sha = finalization_payloads(db_conn, task_id)[0]["verified_tree_sha"]

    # Step 5: content changes to CONTENT_B, same path, same base_head --
    # recorded as durable ownership evidence exactly as a real write_file
    # would, but with NO state transition at all (task stays at
    # READY_FOR_CHECKPOINT throughout).
    (root / "src/foo.py").write_bytes(content_b)
    new_hash = files.digest(content_b)
    with transaction(db_conn):
        op = ToolOperationsRepo(db_conn).start_in_transaction(
            task_id=task_id, worktree_id=lease.worktree_id, worker_id="test-simulated-race",
            worker_session_id="s", tool_name="write_file", risk_class="WRITE_OWNED",
            request_hash="simulated", target_resource="src/foo.py",
            before_evidence=files.digest(content_a),
        )
        ToolOperationsRepo(db_conn).finish_in_transaction(
            op.operation_id, status=OperationStatus.SUCCEEDED, after_evidence=new_hash,
        )
        db_conn.execute(
            "UPDATE task_owned_paths SET last_operation_id = ? WHERE task_id = ? AND path = ?",
            (op.operation_id, task_id, "src/foo.py"),
        )

    # Step 6: a REAL checkpoint for CONTENT_B -- CheckpointManager
    # independently re-validates on-disk content against this (now
    # internally consistent) evidence and legitimately accepts it; this
    # is a genuine checkpoint, not a simulated/forced state.
    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_path / "tmp", lease=lease)
    result = manager.create(task_id)
    assert result.decision == Decision.ALLOW
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"
    checkpoint_b = CheckpointRepo(db_conn).get(result.checkpoint_id)
    verified_b = parse_verified(checkpoint_b)
    # Same base_head, same owned path, only the content differs -- and
    # the resulting Git tree ids already provably differ as a result.
    assert verified_b.owned_paths == (("src/foo.py", new_hash),)
    assert verified_b.tree_sha != verified_a_tree_sha

    # Steps 7-8: CHECKPOINTED -> COMPLETED MUST be denied, specifically
    # with a content mismatch (the temporal check alone never fired here
    # -- no STATE_TRANSITION to IMPLEMENTING ever occurred).
    guarded = TaskStateMachine(db_conn, guards=(checkpointed_completion_guard(db_conn),))
    with pytest.raises(InvalidTransition, match="verification_content_mismatch"):
        guarded.transition(
            task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.COMPLETED,
            reason="attempted despite content B checkpoint", completion_decision=True,
        )
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"


def test_verify_a_then_repo_changes_then_checkpoint_b_denies_completion(
    implementing_context, db_conn, tmp_path,
):
    """Item #2.B / mandatory test G: verify content A, checkpoint it
    (checkpoint #0), do more work producing content B and re-verify it --
    but before a *real* checkpoint #1 is ever created, simulate a race/bug
    that reaches CHECKPOINTED anyway (`CheckpointRepo.latest()` still
    answers with checkpoint #0 / content A, while the most recent
    unsuperseded VERIFIED record is genuinely about content B). The two
    independently-recorded tree ids must not match, and COMPLETED must be
    refused."""
    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "x.txt", b"content A")
    transition_to_verifying(db_conn, task_id)
    machine = TaskStateMachine(db_conn)
    finalizer = make_finalizer(db_conn, blobs_dir)
    finalizer.decide_after_verification(task_id, lease)
    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_path / "tmp", lease=lease)
    checkpoint_a = manager.create(task_id)
    assert checkpoint_a.decision == Decision.ALLOW
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"

    machine.transition(
        task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.IMPLEMENTING,
        reason="more work",
    )
    rewrite_file(executor, task_id, "x.txt", b"content B", files.digest(b"content A"))
    machine.transition(
        task_id, expected_state=TaskState.IMPLEMENTING, to_state=TaskState.VERIFYING, reason="p",
    )
    decision_b = finalizer.decide_after_verification(task_id, lease)
    assert decision_b.verdict == FinalizerVerdict.VERIFIED
    assert TaskRepo(db_conn).get(task_id).state == "READY_FOR_CHECKPOINT"

    # Simulated race/bug: reach CHECKPOINTED without ever creating a real
    # checkpoint for content B -- bypasses TaskStateMachine (and therefore
    # every guard) entirely, exactly like this file's own bounded-read-
    # only-turn setup does to reach its own edge under test.
    with transaction(db_conn):
        TaskRepo(db_conn)._record_transition_in_transaction(
            task_id, to_state="CHECKPOINTED", to_phase="CHECKPOINTED", reason="simulated race",
        )

    guarded = TaskStateMachine(db_conn, guards=(checkpointed_completion_guard(db_conn),))
    with pytest.raises(InvalidTransition, match="verification_content_mismatch"):
        guarded.transition(
            task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.COMPLETED,
            reason="attempted despite mismatch", completion_decision=True,
        )
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"


def test_old_verified_record_may_not_be_reused_after_remutation(
    implementing_context, db_conn, tmp_path,
):
    """Item #2.C: once a task re-enters IMPLEMENTING, the VERIFIED record
    from before that re-mutation is never treated as valid evidence again,
    even if a (now-stale) checkpoint still nominally exists."""
    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "x.txt", b"content A")
    transition_to_verifying(db_conn, task_id)
    machine = TaskStateMachine(db_conn)
    finalizer = make_finalizer(db_conn, blobs_dir)
    finalizer.decide_after_verification(task_id, lease)
    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_path / "tmp", lease=lease)
    manager.create(task_id)
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"

    machine.transition(
        task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.IMPLEMENTING,
        reason="more work",
    )
    # No re-verification happens this time -- simulate reaching
    # CHECKPOINTED again anyway (a race/bug bypassing the ordinary graph).
    with transaction(db_conn):
        TaskRepo(db_conn)._record_transition_in_transaction(
            task_id, to_state="CHECKPOINTED", to_phase="CHECKPOINTED", reason="simulated race",
        )

    guarded = TaskStateMachine(db_conn, guards=(checkpointed_completion_guard(db_conn),))
    with pytest.raises(InvalidTransition, match="no_verification_evidence"):
        guarded.transition(
            task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.COMPLETED,
            reason="attempted with stale evidence", completion_decision=True,
        )


def test_verified_record_missing_fingerprint_field_denies_completion(
    implementing_context, db_conn, tmp_path,
):
    """Item #2.D: a VERIFIED record that (a malformed/legacy producer)
    carries no `verified_tree_sha` at all must never be treated as a
    match by omission -- absence of proof is not proof of a match."""
    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "x.txt", b"content A")
    transition_to_verifying(db_conn, task_id)
    machine = TaskStateMachine(db_conn)
    with transaction(db_conn):
        AuditWriter(db_conn).append(
            task_id=task_id, event_type="FINALIZATION_DECIDED", actor_type="system",
            actor_id="test-forged-record", payload={
                "verdict": "VERIFIED", "reason_code": "all_verification_commands_passed",
                "target_state": "READY_FOR_CHECKPOINT", "repair_attempts": 0, "verification": [],
            },
        )
    machine.transition(
        task_id, expected_state=TaskState.VERIFYING, to_state=TaskState.READY_FOR_CHECKPOINT,
        reason="p",
    )
    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_path / "tmp", lease=lease)
    result = manager.create(task_id)
    assert result.decision == Decision.ALLOW
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"

    guarded = TaskStateMachine(db_conn, guards=(checkpointed_completion_guard(db_conn),))
    with pytest.raises(InvalidTransition, match="verification_content_mismatch"):
        guarded.transition(
            task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.COMPLETED,
            reason="attempted without a fingerprint", completion_decision=True,
        )


def test_provenance_shows_which_verification_bound_to_which_checkpoint(
    implementing_context, db_conn, tmp_path,
):
    """Item #2.E: the audit trail must show, in full and independently
    re-derivable form, which verified content a checkpoint's completion
    was actually approved against."""
    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "x.txt", b"content A")
    transition_to_verifying(db_conn, task_id)
    make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)
    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_path / "tmp", lease=lease)
    result = manager.create(task_id)

    payload = finalization_payloads(db_conn, task_id)[0]
    expected_hash = files.digest(b"content A")
    assert payload["verified_owned_paths"] == [["x.txt", expected_hash]]
    assert payload["verified_tree_sha"]

    checkpoint = CheckpointRepo(db_conn).get(result.checkpoint_id)
    verified = parse_verified(checkpoint)
    assert verified.owned_paths == (("x.txt", expected_hash),)
    # The checkpoint's own already-durable tree id is IDENTICAL to the
    # one the finalizer recorded at verification time -- no recomputation
    # needed on either side to prove it; both are the exact same evidence.
    assert verified.tree_sha == payload["verified_tree_sha"]


# --- lease fencing / wrong state --------------------------------------------

def test_stale_lease_denies_without_transitioning(context, db_conn):
    root, task_id, blobs_dir, lease = context
    stale = LeaseHandle(lease.worktree_id, lease.task_id, "someone-else", "s", 0, "x")

    with pytest.raises(FinalizationError, match="stale_fencing_token"):
        make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, stale)

    assert TaskRepo(db_conn).get(task_id).state == "VERIFYING"


def test_wrong_task_state_raises_without_side_effects(context, db_conn):
    root, task_id, blobs_dir, lease = context
    TaskStateMachine(db_conn).transition(
        task_id, expected_state=TaskState.VERIFYING, to_state=TaskState.REVIEWING, reason="p",
    )

    with pytest.raises(FinalizationError, match="wrong_task_state"):
        make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)

    assert TaskRepo(db_conn).get(task_id).state == "REVIEWING"
    assert finalization_payloads(db_conn, task_id) == []


# --- formatter commands must never mutate the code they verify -------------

def test_ruff_format_check_never_rewrites_files(db_conn, git_repo_with_commit, tmp_path):
    """A file that genuinely needs reformatting must be reported as a
    FAILED verification command (`ruff format --check .` exits non-zero)
    -- never silently reformatted by the verification pass itself. Needs
    its own `[tool.ruff.format]` baseline config, distinct from the other
    tests' plain `[tool.ruff]` default."""
    root, task_id, blobs_dir, lease = _build(
        db_conn, git_repo_with_commit, tmp_path / "evidence",
        pyproject_toml="[tool.ruff]\n[tool.ruff.format]\n", stop_at_verifying=False,
    )
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    badly_formatted = b"def f( ):\n    return    1\n"
    own_file(executor, task_id, "messy.py", badly_formatted)
    transition_to_verifying(db_conn, task_id)

    decision = make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)

    assert (root / "messy.py").read_bytes() == badly_formatted  # byte-for-byte untouched
    assert decision.verdict == FinalizerVerdict.REPAIR_REQUIRED
    assert decision.reason_code == "verification_command_failed:ruff format ."
    payload = finalization_payloads(db_conn, task_id)[0]
    entry = next(v for v in payload["verification"] if v["command"] == "ruff format .")
    assert entry["status"] == "FAILED"


# --- Invariant A: verification runs against an isolated tree, never the ---
# --- live worktree -----------------------------------------------------

def test_unowned_tracked_live_modification_does_not_influence_verification(
    db_conn, git_repo_with_commit, tmp_path,
):
    """Mandatory test A: an owned test file depends on a baseline-tracked,
    NOT-owned helper module. Modifying that helper live (working-tree
    only, never committed, never owned) would make the live repo's test
    pass -- but isolated verification must see only the helper's baseline
    (committed) content, so the test must still fail there."""
    root, task_id, blobs_dir, lease = _build(
        db_conn, git_repo_with_commit, tmp_path / "evidence",
        pyproject_toml="[tool.pytest]\n",
        extra_baseline_files={"helper.py": "VALUE = False\n"},
        stop_at_verifying=False,
    )
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(
        executor, task_id, "test_thing.py",
        b"from helper import VALUE\n\n\ndef test_x():\n    assert VALUE\n",
    )
    transition_to_verifying(db_conn, task_id)

    # X: an unowned, tracked file modified live (working-tree only) in a
    # way that WOULD make the test pass if verification saw it.
    (root / "helper.py").write_text("VALUE = True\n")

    decision = make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)

    # Proof the live repo really would have been influenced by X: running
    # pytest directly against the live root passes.
    import subprocess

    live_result = subprocess.run(
        ["pytest"], cwd=root, capture_output=True, timeout=30,
    )
    assert live_result.returncode == 0, "test setup invalid: live repo did not pass with X present"

    # Isolated verification must NOT have seen X -- the test must still
    # fail (helper.py == baseline's VALUE = False in the materialized
    # tree), so the decision is REPAIR_REQUIRED, never VERIFIED.
    assert decision.verdict == FinalizerVerdict.REPAIR_REQUIRED
    payload = finalization_payloads(db_conn, task_id)[0]
    assert payload["verification"][0]["status"] == "FAILED"
    # The live file itself is untouched -- verification never mutates
    # anything it reads from, isolated tree or otherwise.
    assert (root / "helper.py").read_text() == "VALUE = True\n"


def test_untracked_conftest_contamination_does_not_influence_verification(
    db_conn, git_repo_with_commit, tmp_path,
):
    """Mandatory test B: an untracked `conftest.py` that would skip every
    test (making a genuinely failing test suite report success) must not
    be visible to isolated verification."""
    root, task_id, blobs_dir, lease = _build(
        db_conn, git_repo_with_commit, tmp_path / "evidence",
        pyproject_toml="[tool.pytest]\n", stop_at_verifying=False,
    )
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "test_thing.py", b"def test_x():\n    assert False\n")
    transition_to_verifying(db_conn, task_id)

    # Contamination: untracked, never owned, never committed.
    (root / "conftest.py").write_text(
        "import pytest\n\n"
        "def pytest_collection_modifyitems(config, items):\n"
        "    for item in items:\n"
        "        item.add_marker(pytest.mark.skip(reason='contamination'))\n",
    )

    decision = make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)

    import subprocess

    live_result = subprocess.run(["pytest"], cwd=root, capture_output=True, timeout=30)
    assert live_result.returncode == 0, "test setup invalid: live conftest did not skip the failure"

    assert decision.verdict == FinalizerVerdict.REPAIR_REQUIRED
    payload = finalization_payloads(db_conn, task_id)[0]
    assert payload["verification"][0]["status"] == "FAILED"


def test_gitignored_contamination_does_not_influence_verification(
    db_conn, git_repo_with_commit, tmp_path,
):
    """Mandatory test C: same as test B, but the contaminating file is
    additionally git-ignored -- isolated verification must still not see
    it (by construction: `checkout-index` only ever materializes what is
    in the index, regardless of `.gitignore`)."""
    root, task_id, blobs_dir, lease = _build(
        db_conn, git_repo_with_commit, tmp_path / "evidence",
        pyproject_toml="[tool.pytest]\n",
        extra_baseline_files={".gitignore": "conftest.py\n"},
        stop_at_verifying=False,
    )
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "test_thing.py", b"def test_x():\n    assert False\n")
    transition_to_verifying(db_conn, task_id)

    (root / "conftest.py").write_text(
        "import pytest\n\n"
        "def pytest_collection_modifyitems(config, items):\n"
        "    for item in items:\n"
        "        item.add_marker(pytest.mark.skip(reason='contamination'))\n",
    )

    decision = make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)

    assert decision.verdict == FinalizerVerdict.REPAIR_REQUIRED
    payload = finalization_payloads(db_conn, task_id)[0]
    assert payload["verification"][0]["status"] == "FAILED"


def test_verification_commands_execute_outside_live_repo_root(
    implementing_context, db_conn, monkeypatch,
):
    """Mandatory test D: verification commands run with `cwd` set to the
    materialized isolated tree, never `repo_root`."""
    import code_slayer.finalization.verification as verification_module

    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "clean.py", b"VALUE = 1\n")
    transition_to_verifying(db_conn, task_id)

    seen_cwds = []
    real_capture = verification_module._capture

    def spying_capture(argv, *, cwd, **kwargs):
        seen_cwds.append(Path(cwd))
        return real_capture(argv, cwd=cwd, **kwargs)

    monkeypatch.setattr(verification_module, "_capture", spying_capture)

    make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)

    assert seen_cwds, "no verification command actually ran"
    for cwd in seen_cwds:
        assert cwd != root
        assert not cwd.is_relative_to(root)
        assert "finalizer-verify-tree-" in cwd.name


def test_materialized_tree_content_matches_verified_tree_sha(
    implementing_context, db_conn, monkeypatch,
):
    """Mandatory test E: the materialized tree's actual on-disk content
    (captured before cleanup) matches exactly what `verified_tree_sha`
    represents -- baseline's `README.md` plus the owned path's exact
    bytes, nothing else."""
    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "owned.py", b"VALUE = 1\n")
    transition_to_verifying(db_conn, task_id)
    (root / "untracked_scratch.txt").write_text("not part of any tree")

    captured_dir = {}
    real_materialize = Finalizer._materialize_verification_tree

    def spying_materialize(self, base_tree_sha, edits, repo_root):
        isolated_dir, tree_sha, real_cleanup = real_materialize(
            self, base_tree_sha, edits, repo_root,
        )
        # Snapshot the directory's content immediately after
        # materialization, BEFORE any verification command runs -- a
        # command (ruff/pytest) legitimately writes its own cache
        # artifacts (`.ruff_cache/`, `__pycache__/`, ...) into the
        # isolated directory as a side effect of running, which is
        # unrelated to what was actually checked out and would otherwise
        # contaminate this snapshot.
        captured_dir["tree_sha"] = tree_sha
        captured_dir["files"] = {
            p.relative_to(isolated_dir).as_posix(): p.read_bytes()
            for p in isolated_dir.rglob("*") if p.is_file()
        }
        return isolated_dir, tree_sha, real_cleanup

    monkeypatch.setattr(Finalizer, "_materialize_verification_tree", spying_materialize)

    decision = make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)
    assert decision.verdict == FinalizerVerdict.VERIFIED

    present = captured_dir["files"]
    assert set(present) == {"README.md", "pyproject.toml", "owned.py"}
    assert present["README.md"] == (root / "README.md").read_bytes()
    assert present["owned.py"] == b"VALUE = 1\n"
    assert "untracked_scratch.txt" not in present

    payload = finalization_payloads(db_conn, task_id)[0]
    assert payload["verified_tree_sha"] == captured_dir["tree_sha"]


def test_materialization_failure_blocks_with_durable_evidence_no_new_state(
    implementing_context, db_conn, monkeypatch,
):
    """Mandatory test H: if materializing the isolated tree fails, no
    verification success can occur -- the task is durably marked
    INVALID_ENVIRONMENT/BLOCKED (existing semantics, no new TaskState),
    with durable evidence explaining why."""
    root, task_id, blobs_dir, lease = implementing_context
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    own_file(executor, task_id, "clean.py", b"VALUE = 1\n")
    transition_to_verifying(db_conn, task_id)

    def boom(*a, **kw):
        raise cg.CheckpointGitError("simulated checkout-index failure")

    monkeypatch.setattr(cg, "checkout_tree_to_directory", boom)

    decision = make_finalizer(db_conn, blobs_dir).decide_after_verification(task_id, lease)

    assert decision.verdict == FinalizerVerdict.INVALID_ENVIRONMENT
    assert decision.reason_code == "verification_tree_materialization_failed"
    assert decision.target_state == TaskState.BLOCKED
    task = TaskRepo(db_conn).get(task_id)
    assert task.state == "BLOCKED"
    assert task.current_phase == "VERIFYING"  # existing resume-origin semantics, no new TaskState

    payloads = finalization_payloads(db_conn, task_id)
    assert len(payloads) == 1
    assert payloads[0]["verdict"] == "INVALID_ENVIRONMENT"
    assert payloads[0]["reason_code"] == "verification_tree_materialization_failed"
    assert payloads[0]["verification"] == []
