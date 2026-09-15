"""Graph, decision, origin, and atomicity contracts for the durable core."""

from __future__ import annotations

import json
import sqlite3

import pytest

from code_slayer.audit.verify import verify_chain
from code_slayer.audit.writer import AuditWriter
from code_slayer.core import (
    InvalidResumeTarget,
    InvalidTaskPhase,
    InvalidTransition,
    StaleTaskState,
    StateMachineError,
    TaskState,
    TaskStateMachine,
    TerminalTaskError,
    is_active,
    is_interruptible,
    is_terminal,
)
from code_slayer.core.transitions import TRANSITIONS
from code_slayer.store.task_repo import TaskRepo

# Independent specification oracle, not generated from the production table.
HAPPY_PATH = (
    "CREATED", "INSPECTING", "BASELINED", "PLANNING", "PLANNED",
    "IMPLEMENTING", "VERIFYING", "REVIEWING", "READY_FOR_CHECKPOINT",
    "CHECKPOINTED", "COMPLETED",
)
ACTIVE = (*HAPPY_PATH[:-1], "REPAIRING")
SUSPENDED = ("INTERRUPTED_RESUMABLE", "BLOCKED")
TERMINAL = ("FAILED", "COMPLETED")
FORWARD_EDGES = set(zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False)) | {
    ("CHECKPOINTED", "IMPLEMENTING"),
    ("VERIFYING", "REPAIRING"),
    ("REPAIRING", "VERIFYING"),
    ("REVIEWING", "REPAIRING"),
}
STATIC_EDGES = (
    FORWARD_EDGES
    | {(source, target) for source in ACTIVE for target in (*SUSPENDED, "FAILED")}
    | {(source, "FAILED") for source in SUSPENDED}
)


def create_task(conn, state="CREATED", *, phase=None):
    """Use foundation primitives only to arrange otherwise unreachable fixtures."""
    repo = TaskRepo(conn)
    task = repo.create(
        description="state-machine test", repo_root="/r", repo_id="r",
        worktree_id="w", config={"preserve": {"setting": True}},
    )
    if state != "CREATED" or phase is not None:
        task = repo.record_transition(
            task.task_id, to_state=state,
            to_phase=phase if phase is not None else state, reason="fixture setup",
        )
    return task


def events(conn, task_id):
    return [dict(row) for row in conn.execute(
        "SELECT * FROM audit_events WHERE task_id = ? ORDER BY seq", (task_id,)
    )]


def apply(machine, task, target, **kwargs):
    return machine.transition(
        task.task_id, expected_state=TaskState(task.state),
        to_state=TaskState(target), reason="explicit test decision", **kwargs,
    )


def decision_inputs(source, target):
    return {
        "completion_decision": target == "COMPLETED",
        "failure_decision": target == "FAILED",
        "reconciled_target": TaskState(target)
        if source == "BLOCKED" and target in ACTIVE else None,
    }


def test_vocabulary_classification_and_exact_static_graph():
    assert {s.value for s in TaskState} == set(ACTIVE + SUSPENDED + TERMINAL)
    for state in TaskState:
        assert is_active(state) == (state.value in ACTIVE)
        assert is_interruptible(state) == (state.value in ACTIVE)
        assert is_terminal(state) == (state.value in TERMINAL)
    actual = {(source.value, target.value) for source, targets in TRANSITIONS.items()
              for target in targets}
    assert actual == STATIC_EDGES
    with pytest.raises(TypeError):
        TRANSITIONS[TaskState.CREATED] = frozenset()


@pytest.mark.parametrize("source", [s.value for s in TaskState])
@pytest.mark.parametrize("target", [s.value for s in TaskState])
def test_every_state_pair(db_conn, source, target):
    task = create_task(db_conn, source, phase="INSPECTING" if source in SUSPENDED else None)
    before_events = events(db_conn, task.task_id)
    machine = TaskStateMachine(db_conn)
    legal = (source, target) in STATIC_EDGES or (
        source in SUSPENDED and target == "INSPECTING"
    )
    if not legal:
        error = TerminalTaskError if source in TERMINAL else InvalidTransition
        with pytest.raises(error):
            apply(machine, task, target, **decision_inputs(source, target))
        assert TaskRepo(db_conn).get(task.task_id) == task
        assert events(db_conn, task.task_id) == before_events
        return

    updated = apply(machine, task, target, **decision_inputs(source, target))
    after_events = events(db_conn, task.task_id)
    assert updated.state == target
    assert updated.config_json == task.config_json
    assert len(after_events) == len(before_events) + 1
    event = after_events[-1]
    payload = json.loads(event["payload_json"])
    assert event["event_type"] == "STATE_TRANSITION"
    assert payload["from_state"] == source
    assert payload["to_state"] == target
    assert payload["phase_before"] == task.current_phase
    assert payload["phase_after"] == updated.current_phase
    assert payload["reason"] == "explicit test decision"
    assert event["occurred_at"] == updated.updated_at
    if source in SUSPENDED or target in SUSPENDED:
        assert payload["resume_origin"] == ("INSPECTING" if source in SUSPENDED else source)
    else:
        assert "resume_origin" not in payload
    assert verify_chain(db_conn, task_id=task.task_id).ok


@pytest.mark.parametrize("suspension", SUSPENDED)
@pytest.mark.parametrize("origin", ACTIVE)
def test_every_origin_round_trips_exact_phase(db_conn, origin, suspension):
    task = create_task(db_conn, origin)
    machine = TaskStateMachine(db_conn)
    paused = apply(machine, task, suspension)
    assert paused.current_phase == origin
    resumed = apply(machine, paused, origin, **decision_inputs(suspension, origin))
    assert resumed.state == origin
    assert resumed.current_phase == task.current_phase
    assert resumed.config_json == task.config_json
    for row in events(db_conn, task.task_id)[-2:]:
        assert json.loads(row["payload_json"])["resume_origin"] == origin


@pytest.mark.parametrize("suspension", SUSPENDED)
@pytest.mark.parametrize("origin", [None, "unknown", "COMPLETED", "FAILED", *SUSPENDED])
def test_invalid_durable_origin_rejects_resume_and_failure(db_conn, suspension, origin):
    task = create_task(db_conn, suspension)
    # Deliberately malformed storage must fail closed, even if history exists.
    db_conn.execute("UPDATE tasks SET current_phase = ? WHERE task_id = ?",
                    (origin, task.task_id))
    task = TaskRepo(db_conn).get(task.task_id)
    before = events(db_conn, task.task_id)
    for target in ("INSPECTING", "FAILED"):
        with pytest.raises(InvalidResumeTarget):
            apply(TaskStateMachine(db_conn), task, target,
                  **decision_inputs(suspension, target))
        assert TaskRepo(db_conn).get(task.task_id) == task
        assert events(db_conn, task.task_id) == before


@pytest.mark.parametrize("origin", ACTIVE)
@pytest.mark.parametrize("suspension", SUSPENDED)
def test_different_active_origin_is_rejected(db_conn, origin, suspension):
    task = create_task(db_conn, origin)
    machine = TaskStateMachine(db_conn)
    paused = apply(machine, task, suspension)
    wrong = "VERIFYING" if origin != "VERIFYING" else "REVIEWING"
    before = events(db_conn, task.task_id)
    with pytest.raises(InvalidResumeTarget):
        apply(machine, paused, wrong, **decision_inputs(suspension, wrong))
    assert TaskRepo(db_conn).get(task.task_id) == paused
    assert events(db_conn, task.task_id) == before


def test_completion_and_happy_path(db_conn):
    task = create_task(db_conn)
    machine = TaskStateMachine(db_conn)
    for target in HAPPY_PATH[1:]:
        task = apply(machine, task, target, completion_decision=target == "COMPLETED")
    assert task.current_phase == "CHECKPOINTED"
    assert len(events(db_conn, task.task_id)) == len(HAPPY_PATH)
    with pytest.raises(TerminalTaskError):
        apply(machine, task, "IMPLEMENTING")


@pytest.mark.parametrize("source", ["VERIFYING", "REVIEWING"])
def test_repair_loop(db_conn, source):
    task = create_task(db_conn, source)
    machine = TaskStateMachine(db_conn)
    for target in ("REPAIRING", "VERIFYING", "REVIEWING"):
        task = apply(machine, task, target)
        assert task.current_phase == target
    assert verify_chain(db_conn, task_id=task.task_id).ok


def test_checkpoint_implementation_loop(db_conn):
    task = create_task(db_conn, "CHECKPOINTED")
    machine = TaskStateMachine(db_conn)
    for target in ("IMPLEMENTING", "VERIFYING", "REVIEWING", "READY_FOR_CHECKPOINT",
                   "CHECKPOINTED", "IMPLEMENTING"):
        task = apply(machine, task, target)
    assert task.state == "IMPLEMENTING"


@pytest.mark.parametrize(("source", "target", "kwargs", "error"), [
    ("CHECKPOINTED", "COMPLETED", {}, InvalidTransition),
    ("PLANNED", "FAILED", {}, InvalidTransition),
    ("PLANNED", "IMPLEMENTING", {"completion_decision": True}, InvalidTransition),
    ("PLANNED", "IMPLEMENTING", {"failure_decision": True}, InvalidTransition),
    ("CHECKPOINTED", "COMPLETED", {"completion_decision": "yes"}, InvalidTransition),
    ("PLANNED", "FAILED", {"failure_decision": 1}, InvalidTransition),
    ("BLOCKED", "PLANNED", {}, InvalidResumeTarget),
    ("BLOCKED", "PLANNED", {"reconciled_target": TaskState.PLANNING}, InvalidResumeTarget),
    ("BLOCKED", "PLANNED", {"reconciled_target": "PLANNED"}, InvalidResumeTarget),
    ("PLANNED", "IMPLEMENTING", {"reconciled_target": TaskState.PLANNED}, InvalidTransition),
])
def test_invalid_decision_inputs_leave_no_trace(db_conn, source, target, kwargs, error):
    task = create_task(db_conn, source, phase="PLANNED" if source == "BLOCKED" else None)
    before = events(db_conn, task.task_id)
    with pytest.raises(error):
        apply(TaskStateMachine(db_conn), task, target, **kwargs)
    assert TaskRepo(db_conn).get(task.task_id) == task
    assert events(db_conn, task.task_id) == before


@pytest.mark.parametrize("reason", [None, "", "  ", 42])
def test_invalid_reason(db_conn, reason):
    task = create_task(db_conn)
    with pytest.raises(InvalidTransition):
        TaskStateMachine(db_conn).transition(
            task.task_id, expected_state=TaskState.CREATED,
            to_state=TaskState.INSPECTING, reason=reason,
        )
    assert TaskRepo(db_conn).get(task.task_id) == task
    assert len(events(db_conn, task.task_id)) == 1


@pytest.mark.parametrize(("expected", "target"), [
    ("CREATED", TaskState.INSPECTING),
    (TaskState.CREATED, "INSPECTING"),
    (TaskState.CREATED, "invented"),
])
def test_state_inputs_require_enum(db_conn, expected, target):
    task = create_task(db_conn)
    with pytest.raises(InvalidTransition):
        TaskStateMachine(db_conn).transition(
            task.task_id, expected_state=expected, to_state=target, reason="test",
        )
    assert TaskRepo(db_conn).get(task.task_id) == task
    assert len(events(db_conn, task.task_id)) == 1


def test_duplicate_request_and_self_edge_are_explicit_errors(db_conn):
    original = create_task(db_conn)
    machine = TaskStateMachine(db_conn)
    updated = apply(machine, original, "INSPECTING")
    before = events(db_conn, original.task_id)
    with pytest.raises(StaleTaskState):
        apply(machine, original, "INSPECTING")
    with pytest.raises(InvalidTransition):
        apply(machine, updated, "INSPECTING")
    assert TaskRepo(db_conn).get(original.task_id) == updated
    assert events(db_conn, original.task_id) == before


@pytest.mark.parametrize("phase", [None, "implementation", "PLANNED"])
def test_incompatible_stored_phase_is_not_reinterpreted(db_conn, phase):
    task = create_task(db_conn, "IMPLEMENTING")
    db_conn.execute("UPDATE tasks SET current_phase = ? WHERE task_id = ?", (phase, task.task_id))
    before = events(db_conn, task.task_id)
    with pytest.raises(InvalidTaskPhase):
        apply(TaskStateMachine(db_conn), task, "INTERRUPTED_RESUMABLE")
    assert TaskRepo(db_conn).get(task.task_id).current_phase == phase
    assert events(db_conn, task.task_id) == before


def test_unknown_persisted_state_is_domain_error(db_conn):
    task = create_task(db_conn, "invented")
    before = events(db_conn, task.task_id)
    with pytest.raises(InvalidTransition):
        TaskStateMachine(db_conn).transition(
            task.task_id, expected_state=TaskState.CREATED,
            to_state=TaskState.INSPECTING, reason="test",
        )
    assert TaskRepo(db_conn).get(task.task_id) == task
    assert events(db_conn, task.task_id) == before


@pytest.mark.parametrize("fail_after_append", [False, True])
def test_failure_between_writes_or_before_commit_rolls_back_both(
    db_conn, monkeypatch, fail_after_append,
):
    task = create_task(db_conn, "IMPLEMENTING")
    before = events(db_conn, task.task_id)
    original_append = AuditWriter.append

    def fail(self, **kwargs):
        # Prove that the task UPDATE really happened before injecting failure.
        assert db_conn.in_transaction
        assert TaskRepo(db_conn).get(task.task_id).state == "VERIFYING"
        assert len(events(db_conn, task.task_id)) == len(before)
        if fail_after_append:
            original_append(self, **kwargs)
            assert len(events(db_conn, task.task_id)) == len(before) + 1
        raise RuntimeError("injected failure")

    with monkeypatch.context() as patch:
        patch.setattr(AuditWriter, "append", fail)
        with pytest.raises(RuntimeError, match="injected failure"):
            apply(TaskStateMachine(db_conn), task, "VERIFYING")
    assert not db_conn.in_transaction
    assert TaskRepo(db_conn).get(task.task_id) == task
    assert events(db_conn, task.task_id) == before
    assert verify_chain(db_conn, task_id=task.task_id).ok
    # A retry after rollback must work and append exactly one real event.
    apply(TaskStateMachine(db_conn), task, "VERIFYING")
    assert len(events(db_conn, task.task_id)) == len(before) + 1
    assert verify_chain(db_conn, task_id=task.task_id).ok


@pytest.mark.parametrize("table", ["tasks", "audit_events"])
def test_database_write_failure_cannot_leave_inverse_partial_transition(db_conn, table):
    task = create_task(db_conn, "IMPLEMENTING")
    before = events(db_conn, task.task_id)
    operation = "UPDATE" if table == "tasks" else "INSERT"
    db_conn.execute(
        f"CREATE TRIGGER reject_write BEFORE {operation} ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'injected database failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected database failure"):
        apply(TaskStateMachine(db_conn), task, "VERIFYING")
    assert TaskRepo(db_conn).get(task.task_id) == task
    assert events(db_conn, task.task_id) == before


def test_additional_guard_runs_before_writes_and_can_veto(db_conn):
    task = create_task(db_conn)
    before = events(db_conn, task.task_id)
    seen = []

    def guard(current, request):
        assert db_conn.in_transaction
        assert current == task
        assert events(db_conn, task.task_id) == before
        seen.append(request.to_state)
        raise StateMachineError("additional guard rejected")

    with pytest.raises(StateMachineError, match="additional guard rejected"):
        apply(TaskStateMachine(db_conn, guards=[guard]), task, "INSPECTING")
    assert seen == [TaskState.INSPECTING]
    assert TaskRepo(db_conn).get(task.task_id) == task
    assert events(db_conn, task.task_id) == before


def test_transition_preserves_actor_attribution(db_conn):
    task = create_task(db_conn)
    apply(TaskStateMachine(db_conn), task, "INSPECTING", actor_type="user", actor_id="owner")
    event = events(db_conn, task.task_id)[-1]
    assert (event["actor_type"], event["actor_id"]) == ("user", "owner")
    assert verify_chain(db_conn, task_id=task.task_id).ok


def test_transaction_scoped_primitive_rejects_autocommit(db_conn):
    task = create_task(db_conn)
    with pytest.raises(RuntimeError, match="open write transaction"):
        TaskRepo(db_conn)._record_transition_in_transaction(
            task.task_id, to_state="INSPECTING", to_phase="INSPECTING", reason="test",
        )
    assert TaskRepo(db_conn).get(task.task_id) == task
    assert len(events(db_conn, task.task_id)) == 1


@pytest.mark.parametrize(
    "operation", [None, "SUCCEEDED", "FAILED", "STARTED", "UNKNOWN", "mutation"],
)
def test_bounded_read_only_completion_checks_journal(db_conn, operation):
    from code_slayer.store.tool_operations_repo import ToolOperationsRepo

    task = TaskRepo(db_conn).create(
        description="bounded read-only turn", repo_root="/r", repo_id="r", worktree_id="w",
        config={"execution_kind": "bounded_read_only_turn"},
    )
    TaskRepo(db_conn).record_transition(
        task.task_id, to_state="IMPLEMENTING", to_phase="IMPLEMENTING", reason="fixture",
    )
    task = TaskRepo(db_conn).get(task.task_id)
    if operation:
        repo = ToolOperationsRepo(db_conn)
        op = repo.start(
            task_id=task.task_id, worktree_id="w", worker_id="worker", worker_session_id="session",
            tool_name="write_file" if operation == "mutation" else "read_file",
            risk_class="MUTATING" if operation == "mutation" else "READ_ONLY",
            request_hash="hash", target_resource="README.md",
        )
        repo.finish(op.operation_id, status="SUCCEEDED" if operation == "mutation" else operation)
    machine = TaskStateMachine(db_conn)
    with pytest.raises(InvalidTransition):
        apply(machine, task, "COMPLETED")  # still needs explicit completion decision
    if operation in ("STARTED", "UNKNOWN", "mutation"):
        with pytest.raises(InvalidTransition, match="resolved read-only"):
            apply(machine, task, "COMPLETED", completion_decision=True)
        assert TaskRepo(db_conn).get(task.task_id).state == "IMPLEMENTING"
    else:
        assert apply(machine, task, "COMPLETED", completion_decision=True).state == "COMPLETED"
    assert verify_chain(db_conn, task_id=task.task_id).ok
