"""Canonical durable task states and their shared classifications."""

from enum import StrEnum


class TaskState(StrEnum):
    CREATED = "CREATED"
    INSPECTING = "INSPECTING"
    BASELINED = "BASELINED"
    PLANNING = "PLANNING"
    PLANNED = "PLANNED"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    REPAIRING = "REPAIRING"
    REVIEWING = "REVIEWING"
    READY_FOR_CHECKPOINT = "READY_FOR_CHECKPOINT"
    CHECKPOINTED = "CHECKPOINTED"
    INTERRUPTED_RESUMABLE = "INTERRUPTED_RESUMABLE"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


TERMINAL_STATES = frozenset({TaskState.FAILED, TaskState.COMPLETED})
SUSPENDED_STATES = frozenset({TaskState.INTERRUPTED_RESUMABLE, TaskState.BLOCKED})
ACTIVE_STATES = frozenset(TaskState) - TERMINAL_STATES - SUSPENDED_STATES


def is_terminal(state: TaskState) -> bool:
    return state in TERMINAL_STATES


def is_active(state: TaskState) -> bool:
    """An ordinary lifecycle state, including CREATED and CHECKPOINTED.

    Suspended tasks still occupy their worktree's non-terminal slot, but
    are not active phases in the transition graph.
    """
    return state in ACTIVE_STATES


def is_interruptible(state: TaskState) -> bool:
    return is_active(state)
