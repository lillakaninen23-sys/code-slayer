"""Durable worktree lease/fencing (Phase 6): who currently has authority to
mutate a worktree, and a monotonic token stale holders can never satisfy
again once superseded."""

from code_slayer.lease.liveness import Liveness
from code_slayer.lease.manager import (
    DEFAULT_LEASE_TTL_SECONDS,
    LeaseError,
    LeaseHandle,
    LeaseManager,
    LeaseResult,
)
from code_slayer.lease.recovery import (
    UnresolvedOperation,
    discover_unresolved,
    reconcile_supported,
)

__all__ = [
    "DEFAULT_LEASE_TTL_SECONDS",
    "LeaseError",
    "LeaseHandle",
    "LeaseManager",
    "LeaseResult",
    "Liveness",
    "UnresolvedOperation",
    "discover_unresolved",
    "reconcile_supported",
]
