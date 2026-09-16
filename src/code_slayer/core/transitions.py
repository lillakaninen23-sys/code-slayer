"""Pure graph and guard validation; no persistence or external actions."""

import json
from dataclasses import dataclass
from types import MappingProxyType

from code_slayer.core.states import (
    ACTIVE_STATES,
    SUSPENDED_STATES,
    TaskState,
    is_active,
    is_terminal,
)
from code_slayer.store.models import Task


class StateMachineError(RuntimeError):
    """Base class for rejected state-machine requests."""


class InvalidTransition(StateMachineError):
    """The edge or its decision inputs are invalid."""


class StaleTaskState(StateMachineError):
    """The task no longer has the caller's expected state."""


class InvalidResumeTarget(InvalidTransition):
    """A suspended task must return to its explicit, durable origin."""


class InvalidTaskPhase(StateMachineError):
    """Stored phase semantics cannot be interpreted safely."""


class TerminalTaskError(InvalidTransition):
    """Terminal tasks cannot transition, including to themselves."""


# Suspended-state return edges depend on the durable origin and are
# validated separately. All other edges are enumerated here.
_FORWARD = {
    TaskState.CREATED: {TaskState.INSPECTING},
    TaskState.INSPECTING: {TaskState.BASELINED},
    TaskState.BASELINED: {TaskState.PLANNING},
    TaskState.PLANNING: {TaskState.PLANNED},
    TaskState.PLANNED: {TaskState.IMPLEMENTING},
    TaskState.IMPLEMENTING: {TaskState.VERIFYING},
    # READY_FOR_CHECKPOINT is a direct edge (not only via REVIEWING):
    # Deterministic Finalization's finalizer (`finalization.service.
    # Finalizer`) takes it whenever no reviewer was actually consulted
    # (review_required = false in this phase/version -- no reviewer model
    # exists yet) so audit/state history never pretends a review happened
    # when none did; REVIEWING remains reachable for when a real review
    # verdict genuinely is supplied.
    TaskState.VERIFYING: {
        TaskState.REVIEWING, TaskState.REPAIRING, TaskState.READY_FOR_CHECKPOINT,
    },
    TaskState.REPAIRING: {TaskState.VERIFYING},
    TaskState.REVIEWING: {TaskState.READY_FOR_CHECKPOINT, TaskState.REPAIRING},
    TaskState.READY_FOR_CHECKPOINT: {TaskState.CHECKPOINTED},
    TaskState.CHECKPOINTED: {TaskState.IMPLEMENTING, TaskState.COMPLETED},
}
TRANSITIONS = MappingProxyType({
    state: frozenset(
        _FORWARD.get(state, set())
        | (SUSPENDED_STATES if is_active(state) else set())
        | ({TaskState.FAILED} if not is_terminal(state) else set())
    )
    for state in TaskState
})


@dataclass(frozen=True)
class TransitionRequest:
    expected_state: TaskState
    to_state: TaskState
    reason: str
    completion_decision: bool = False
    failure_decision: bool = False
    reconciled_target: TaskState | None = None


@dataclass(frozen=True)
class TransitionEffect:
    phase_after: str | None
    resume_origin: TaskState | None = None


def _active_phase(state: TaskState) -> str | None:
    # Foundation task creation has no entered phase yet.
    return None if state == TaskState.CREATED else state.value


def validate_transition(task: Task, request: TransitionRequest) -> TransitionEffect:
    """Validate under the caller's transaction before either conceptual write.

    current_phase is the canonical active state (NULL for CREATED). In a
    suspended state it instead retains the exact active origin, including
    CREATED. No audit-history reconstruction or config_json mutation is needed.
    """
    try:
        state = TaskState(task.state)
    except ValueError as exc:
        raise InvalidTransition(f"unknown persisted state: {task.state!r}") from exc
    if not isinstance(request.expected_state, TaskState):
        raise InvalidTransition("expected_state must be a TaskState")
    if state != request.expected_state:
        raise StaleTaskState(f"expected {request.expected_state}, found {state}")
    if is_terminal(state):
        raise TerminalTaskError(f"{state} is terminal")
    if not isinstance(request.to_state, TaskState):
        raise InvalidTransition("to_state must be a TaskState")
    target = request.to_state
    if not isinstance(request.reason, str) or not request.reason.strip():
        raise InvalidTransition("a non-empty reason is required")
    for decision in (request.completion_decision, request.failure_decision):
        if type(decision) is not bool:
            raise InvalidTransition("decision inputs must be booleans")
    if request.completion_decision != (target == TaskState.COMPLETED):
        raise InvalidTransition("completion_decision is required only for COMPLETED")
    if request.failure_decision != (target == TaskState.FAILED):
        raise InvalidTransition("failure_decision is required only for FAILED")
    resolving = state == TaskState.BLOCKED and is_active(target)
    if resolving:
        if (
            not isinstance(request.reconciled_target, TaskState)
            or request.reconciled_target != target
        ):
            raise InvalidResumeTarget("BLOCKED resolution must name the reconciled target")
    elif request.reconciled_target is not None:
        raise InvalidTransition("reconciled_target is only valid when resolving BLOCKED")

    origin = None
    if state in SUSPENDED_STATES:
        try:
            origin = TaskState(task.current_phase)
        except (ValueError, TypeError) as exc:
            raise InvalidResumeTarget("missing or unknown durable resume origin") from exc
        if origin not in ACTIVE_STATES:
            raise InvalidResumeTarget(f"{origin} is not an active resume origin")
        if target != TaskState.FAILED:
            if target != origin:
                raise InvalidResumeTarget(f"must return to durable origin {origin}, not {target}")
            return TransitionEffect(_active_phase(origin), origin)
    elif task.current_phase != _active_phase(state):
        raise InvalidTaskPhase(f"{state} has incompatible phase {task.current_phase!r}")

    # Bounded read-only runner tasks have no repository changes to checkpoint.
    # This is a conditional completion edge, never a shortcut for ordinary jobs.
    read_only_completion = (
        state == TaskState.IMPLEMENTING and target == TaskState.COMPLETED
        and json.loads(task.config_json).get("execution_kind") == "bounded_read_only_turn"
    )
    if target not in TRANSITIONS[state] and not read_only_completion:
        raise InvalidTransition(f"illegal transition: {state} -> {target}")
    if target in SUSPENDED_STATES:
        return TransitionEffect(state.value, state)
    if is_terminal(target):
        return TransitionEffect(task.current_phase, origin)
    return TransitionEffect(_active_phase(target))
