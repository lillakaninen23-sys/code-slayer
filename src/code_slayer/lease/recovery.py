"""Generic unresolved-operation recovery (Phase 6, §13).

Discovers `STARTED`/`UNKNOWN` `tool_operations` rows, binds each to the
ownership epoch (`lease_generation`) it started under, and reports how
that epoch relates to the worktree's *current* lease row — purely
informational: this classification never changes what happens here, and
process death alone never proves anything about whether an external side
effect occurred (§13). In particular, this module never treats
`QUIESCING`/`EXPIRED` as license to invent an outcome for an operation —
`epoch_state` only tells a caller *which* epoch an unresolved operation
belongs to; whether that epoch's owner is provably gone is exactly what
`LeaseManager`'s own QUIESCING cascade (`lease.manager`) already decides,
under its own transactions, not this read-only reporting.

This module does not itself know how to resolve any specific operation.
It dispatches to a tool-specific reconciler only where one already
exists: today, exactly `checkpoint_create`
(`repo.checkpoint.CheckpointManager.reconcile`), whose own evidence-based
semantics (a checkpoint's dedicated ref existing or not) are reused
unchanged, never rewritten. Every other tool — every Phase 4 file/command
capability — has no reconciler yet and is reported unresolved, untouched:
this module never invents one, and never infers an outcome from a
recorded epoch being superseded, quiescing, expired, or a process simply
being gone.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from code_slayer.store.lease_repo import LeaseRepo, LeaseStatus

CHECKPOINT_TOOL_NAME = "checkpoint_create"

# One label per case `discover_unresolved` can distinguish, purely by
# comparing the operation's recorded epoch against the worktree's current
# lease row (never by asking whether any process is actually alive —
# that liveness question belongs to `LeaseManager` alone):
#
# - "no_lease": the worktree currently has no lease row at all (e.g. it
#   was never leased, or fully released with no subsequent acquire).
# - "current_active": the operation's epoch *is* the current lease, and
#   that lease is `ACTIVE` — an ordinary in-progress or crashed-but-not-
#   yet-superseded operation under live authority.
# - "current_quiescing": the operation's epoch *is* the current lease,
#   and that lease is `QUIESCING` — its owner's liveness is, right now,
#   unresolved (this *is* the "unknown process liveness" case: whether
#   it is actually still alive is exactly what `QUIESCING` means).
# - "current_expired": the operation's epoch *is* the current lease, and
#   that lease is durably `EXPIRED` — its owner has been proven gone, but
#   no successor has acquired yet.
# - "current_released": the operation's epoch *is* the current lease,
#   and that lease was explicitly `RELEASED` (the rare case of a release
#   racing an operation that had not yet finished/journaled its result).
# - "stale": the worktree has a current lease, but under a *different*
#   epoch than the one this operation started under — a strictly later
#   generation has already become fully `ACTIVE`, so this operation's
#   epoch is conclusively superseded.
# - "unknown": the operation's own `lease_generation` was never recorded
#   (journaled before Phase 6 populated it) — not comparable either way.
EpochState = str
_NO_LEASE: EpochState = "no_lease"
_CURRENT_BY_STATUS: dict[str, EpochState] = {
    LeaseStatus.ACTIVE: "current_active",
    LeaseStatus.QUIESCING: "current_quiescing",
    LeaseStatus.EXPIRED: "current_expired",
    LeaseStatus.RELEASED: "current_released",
}
_STALE: EpochState = "stale"
_UNKNOWN: EpochState = "unknown"


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
    current_lease_status: str | None

    @property
    def stale_epoch(self) -> bool:
        """`True` only when both generations are known and disagree — an
        unknown (`None`) generation is never treated as "stale"; it is
        simply not comparable (e.g. an operation journaled before Phase 6
        ever populated `lease_generation`). Kept for backward
        compatibility; `epoch_state` is the fuller classification."""
        return (
            self.lease_generation is not None
            and self.current_lease_generation is not None
            and self.lease_generation != self.current_lease_generation
        )

    @property
    def epoch_state(self) -> EpochState:
        """Classify this operation's recorded epoch against the
        worktree's current lease row. See the module-level comment above
        for exactly what each label means and does not mean."""
        if self.lease_generation is None:
            return _UNKNOWN
        if self.current_lease_generation is None or self.current_lease_status is None:
            return _NO_LEASE
        if self.lease_generation != self.current_lease_generation:
            return _STALE
        return _CURRENT_BY_STATUS.get(self.current_lease_status, _UNKNOWN)

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
            current_lease_status=current.status if current is not None else None,
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
