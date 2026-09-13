"""The production authority for durable task state changes."""

import sqlite3
from collections.abc import Callable, Iterable

from code_slayer.core.states import TaskState
from code_slayer.core.transitions import TransitionRequest, validate_transition
from code_slayer.store.db import transaction
from code_slayer.store.models import Task
from code_slayer.store.task_repo import TaskRepo

TransitionGuard = Callable[[Task, TransitionRequest], None]


class TaskStateMachine:
    """Serialize load/validation/state/audit with BEGIN IMMEDIATE.

    expected_state is mandatory. Replaying a successful request raises
    StaleTaskState; a self-edge raises InvalidTransition. Neither appends
    an event. This is state-based concurrency, not request-ID deduplication.

    Additional guards are pure validators over frozen inputs, called after
    graph validation and before writes. They raise StateMachineError to veto
    a transition; they must not mutate state or perform external actions.
    """

    def __init__(
        self, conn: sqlite3.Connection, *, guards: Iterable[TransitionGuard] = ()
    ) -> None:
        self._conn = conn
        self._repo = TaskRepo(conn)
        self._guards = tuple(guards)

    def transition(
        self,
        task_id: str,
        *,
        expected_state: TaskState,
        to_state: TaskState,
        reason: str,
        completion_decision: bool = False,
        failure_decision: bool = False,
        reconciled_target: TaskState | None = None,
        actor_type: str = "system",
        actor_id: str | None = None,
    ) -> Task:
        request = TransitionRequest(
            expected_state, to_state, reason,
            completion_decision, failure_decision, reconciled_target,
        )
        with transaction(self._conn):
            updated = self.transition_in_transaction(
                task_id, request=request, actor_type=actor_type, actor_id=actor_id,
            )
        return updated

    def transition_in_transaction(
        self, task_id: str, *, request: TransitionRequest,
        actor_type: str = "system", actor_id: str | None = None,
    ) -> Task:
        """Compose a validated transition with related durable evidence.

        Caller owns a BEGIN IMMEDIATE transaction via store.db.transaction;
        it must roll back the whole transaction on any failure. This method
        applies exactly the same authority and guards as transition(), and
        never starts, commits, or nests a transaction itself.
        """
        if not self._conn.in_transaction:
            raise RuntimeError("state-machine composition requires an open write transaction")
        current = self._repo.get(task_id)
        effect = validate_transition(current, request)
        for guard in self._guards:
            guard(current, request)
        return self._repo._record_transition_in_transaction(
            task_id, to_state=request.to_state.value, to_phase=effect.phase_after,
            reason=request.reason, actor_type=actor_type, actor_id=actor_id,
            resume_origin=effect.resume_origin,
        )
