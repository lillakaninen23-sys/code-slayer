"""Generic unresolved-operation recovery (Phase 6, §13).

Discovers `STARTED`/`UNKNOWN` `tool_operations` rows, binds each to the
ownership epoch (`lease_generation`) it started under, and reports whether
that epoch is still the current one for its worktree — purely
informational: staleness never changes what happens here, and process
death alone never proves anything about whether an external side effect
occurred (§13).

This module does not itself know how to resolve any specific operation.
It dispatches to a tool-specific reconciler only where one already
exists: today, exactly `checkpoint_create`
(`repo.checkpoint.CheckpointManager.reconcile`), whose own evidence-based
semantics (a checkpoint's dedicated ref existing or not) are reused
unchanged, never rewritten. Every other tool — every Phase 4 file/command
capability — has no reconciler yet and is reported unresolved, untouched:
this module never invents one, and never infers an outcome from a
recorded epoch being superseded or a process simply being gone.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from code_slayer.store.lease_repo import LeaseRepo

CHECKPOINT_TOOL_NAME = "checkpoint_create"


@dataclass(frozen=True)
class UnresolvedOperation:
    operation_id: str
    task_id: str
    worktree_id: str
    tool_name: str
    status: str
    started_at: str
    lease_generation: int | None
    current_lease_generation: int | None

    @property
    def stale_epoch(self) -> bool:
        """`True` only when both generations are known and disagree — an
        unknown (`None`) generation is never treated as "stale"; it is
        simply not comparable (e.g. an operation journaled before Phase 6
        ever populated `lease_generation`)."""
        return (
            self.lease_generation is not None
            and self.current_lease_generation is not None
            and self.lease_generation != self.current_lease_generation
        )

    @property
    def has_reconciler(self) -> bool:
        return self.tool_name == CHECKPOINT_TOOL_NAME


def discover_unresolved(
    conn: sqlite3.Connection, *, task_id: str | None = None,
) -> list[UnresolvedOperation]:
    """List every `STARTED`/`UNKNOWN` operation (optionally scoped to one
    task), each annotated with its epoch and whether that epoch is stale.
    Read-only: discovery alone changes nothing."""
    leases = LeaseRepo(conn)
    if task_id is not None:
        rows = conn.execute(
            "SELECT * FROM tool_operations WHERE task_id = ? "
            "AND status IN ('STARTED', 'UNKNOWN') ORDER BY started_at",
            (task_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tool_operations "
            "WHERE status IN ('STARTED', 'UNKNOWN') ORDER BY started_at",
        ).fetchall()
    results = []
    for row in rows:
        current = leases.get(row["worktree_id"])
        results.append(UnresolvedOperation(
            operation_id=row["operation_id"], task_id=row["task_id"],
            worktree_id=row["worktree_id"], tool_name=row["tool_name"],
            status=row["status"], started_at=row["started_at"],
            lease_generation=row["lease_generation"],
            current_lease_generation=current.generation if current is not None else None,
        ))
    return results


def reconcile_supported(
    conn: sqlite3.Connection, *, blobs_dir: Path | str, tmp_dir: Path | str,
    task_id: str | None = None,
) -> list[tuple[UnresolvedOperation, object | None]]:
    """Discover unresolved operations and dispatch each to its
    tool-specific reconciler where one exists, leaving everything else
    exactly as found. Returns `(operation, reconcile_result_or_None)`
    pairs in discovery order — `None` both for "no reconciler exists" and
    for "the reconciler ran but found nothing to resolve"; callers that
    need to distinguish these should call the tool-specific reconciler
    directly (this function's job is dispatch, not disambiguation).
    """
    from code_slayer.repo.checkpoint import CheckpointManager

    outcomes: list[tuple[UnresolvedOperation, object | None]] = []
    for operation in discover_unresolved(conn, task_id=task_id):
        if not operation.has_reconciler:
            outcomes.append((operation, None))
            continue
        manager = CheckpointManager(conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir)
        outcomes.append((operation, manager.reconcile(operation.task_id)))
    return outcomes
