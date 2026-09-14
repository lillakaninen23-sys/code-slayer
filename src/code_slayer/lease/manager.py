"""Durable worktree ownership: acquire/renew/release/takeover, and the
fencing check every mutation path must pass (Phase 6).

## Model

`worker_leases.worktree_id` is the primary key: one worktree, one current
lease, matching Foundation invariant INV-2 (at most one non-terminal task
per worktree) — `tasks.worktree_id` was already documented, since Phase 1,
as "the unit of mutation ownership (future lease manager)".

An **ownership epoch** is `(worker_id, worker_session_id, generation)`.
`generation` is the durable fencing token: it starts at 1 on first
acquisition and strictly increases by exactly 1 on every later takeover,
never resets, and is never reused. A caller proves current authority by
presenting a `LeaseHandle` whose `(worktree_id, worker_id,
worker_session_id, generation)` still matches the persisted row exactly —
not by presenting a PID, hostname, timestamp, or UUID alone (none of
those are sufficient fencing; see `docs/LEASES_AND_RECOVERY.md`).

## Expiry vs. fencing — two different questions

*Expiry* (wall-clock: `heartbeat_at` vs. a TTL) decides whether **takeover
may occur at all**. *Fencing* (an exact integer/identity match) decides
whether **a specific caller's claimed authority is still valid**. A clock
that jumps or runs slow only ever affects when takeover becomes possible;
it can never make a stale generation compare equal to the current one.
Fencing validation therefore never depends on the clock at all.

The current holder may always `renew()` as long as its own
`(worker_id, worker_session_id, generation)` still matches the persisted
row — renewing *is* proof of liveness, regardless of how much time has
elapsed since the last heartbeat, exactly until a takeover has actually
replaced that row.

No background daemon lives here: acquire/renew/release/expire are pure,
explicit primitives a caller invokes; nothing here schedules itself.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.policy.engine import Decision
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.lease_repo import LeaseRepo, LeaseStatus
from code_slayer.store.models import WorkerLease

DEFAULT_LEASE_TTL_SECONDS = 300.0


class LeaseError(RuntimeError):
    """A lease request or a persisted lease record cannot be trusted."""


@dataclass(frozen=True)
class LeaseHandle:
    """A caller's claim to one ownership epoch. Never trust this object's
    fields alone for a decision — it must be revalidated against the
    durable row (`LeaseManager.is_current`) at the moment it matters."""

    worktree_id: str
    task_id: str
    worker_id: str
    worker_session_id: str
    generation: int
    acquired_at: str


@dataclass(frozen=True)
class LeaseResult:
    decision: Decision
    reason: str
    handle: LeaseHandle | None = None


def _deny(reason: str) -> LeaseResult:
    return LeaseResult(Decision.DENY, reason)


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _epoch(obj) -> tuple:
    """The exact ownership epoch a lease row or a handle claims."""
    return (obj.worktree_id, obj.worker_id, obj.worker_session_id, obj.generation)


class LeaseManager:
    def __init__(
        self, conn: sqlite3.Connection, *,
        ttl_seconds: float = DEFAULT_LEASE_TTL_SECONDS, now_fn=utcnow_iso,
    ) -> None:
        self._conn = conn
        self._leases = LeaseRepo(conn)
        self._audit = AuditWriter(conn)
        self._ttl_seconds = ttl_seconds
        self._now_fn = now_fn

    # -- internal helpers ----------------------------------------------

    def _event(self, task_id, event_type, payload) -> None:
        self._audit.append(
            task_id=task_id, event_type=event_type, actor_type="system",
            actor_id="lease-manager", payload=payload,
        )

    @staticmethod
    def _sane(lease: WorkerLease) -> bool:
        """Fail closed on a corrupted/unexpected persisted row rather than
        guessing what it was supposed to mean."""
        try:
            if type(lease.generation) is not int or lease.generation < 1:
                return False
            _parse_iso(lease.acquired_at)
            _parse_iso(lease.heartbeat_at)
        except (ValueError, TypeError):
            return False
        return (
            lease.status in LeaseStatus.ALL
            and isinstance(lease.worker_id, str) and lease.worker_id
            and isinstance(lease.worker_session_id, str) and lease.worker_session_id
        )

    def _is_expired(self, lease: WorkerLease, now: datetime) -> bool:
        return (now - _parse_iso(lease.heartbeat_at)).total_seconds() > self._ttl_seconds

    def _check_current(
        self, current: WorkerLease | None, handle: LeaseHandle,
    ) -> LeaseResult | None:
        """`None` means the handle is currently valid; otherwise the
        `LeaseResult` explaining why it is not."""
        if current is None:
            return _deny("no_lease")
        if not self._sane(current):
            return _deny("malformed_persisted_lease")
        if _epoch(current) != _epoch(handle):
            self._event(handle.task_id, EventType.FENCE_STALE_REJECTED, {
                "worktree_id": handle.worktree_id, "claimed_generation": handle.generation,
                "current_generation": current.generation,
            })
            return _deny("stale_fencing_token")
        if current.status != LeaseStatus.ACTIVE:
            return _deny("lease_not_active")
        return None

    # -- public API ------------------------------------------------------

    def acquire(
        self, *, worktree_id: str, task_id: str, worker_id: str, worker_session_id: str,
    ) -> LeaseResult:
        """Acquire a fresh epoch, or take over an expired/released one.

        Denies (never guesses) if the worktree is currently held, ACTIVE,
        and not expired by another session — or by the caller's own
        identity, which must use `renew()` instead."""
        if not all(isinstance(v, str) and v for v in (
            worktree_id, task_id, worker_id, worker_session_id,
        )):
            return _deny("malformed_lease_request")
        now_iso = self._now_fn()
        now = _parse_iso(now_iso)
        with transaction(self._conn):
            current = self._leases.get(worktree_id)
            if current is not None and not self._sane(current):
                self._event(task_id, EventType.FENCE_STALE_REJECTED, {
                    "worktree_id": worktree_id, "reason": "malformed_persisted_lease",
                })
                return _deny("malformed_persisted_lease")
            held = (
                current is not None and current.status == LeaseStatus.ACTIVE
                and not self._is_expired(current, now)
            )
            if held:
                if (current.worker_id, current.worker_session_id) == (worker_id, worker_session_id):
                    return _deny("already_held_by_caller_use_renew")
                return _deny("lease_held_and_not_expired")
            # Eligible for takeover: no prior row, RELEASED, EXPIRED, the
            # unused-by-Phase-6 QUIESCING, or ACTIVE but past its TTL.
            if current is None:
                new_generation = 1
            else:
                new_generation = current.generation + 1
                if current.status == LeaseStatus.ACTIVE:
                    self._event(current.task_id, EventType.LEASE_EXPIRED, {
                        "worktree_id": worktree_id, "generation": current.generation,
                        "reason": "ttl_exceeded_at_takeover",
                    })
            self._leases.upsert_in_transaction(
                worktree_id=worktree_id, task_id=task_id, worker_id=worker_id,
                worker_session_id=worker_session_id, generation=new_generation,
                acquired_at=now_iso, heartbeat_at=now_iso, status=LeaseStatus.ACTIVE,
                worker_pid=os.getpid(), worker_pid_started_at=now_iso, checkpoint_id=None,
            )
            self._event(task_id, EventType.LEASE_ACQUIRED, {
                "worktree_id": worktree_id, "worker_id": worker_id,
                "worker_session_id": worker_session_id, "generation": new_generation,
            })
        handle = LeaseHandle(
            worktree_id, task_id, worker_id, worker_session_id, new_generation, now_iso,
        )
        return LeaseResult(Decision.ALLOW, "acquired", handle)

    def renew(self, handle: LeaseHandle) -> LeaseResult:
        if not isinstance(handle, LeaseHandle):
            return _deny("malformed_lease_handle")
        now_iso = self._now_fn()
        with transaction(self._conn):
            current = self._leases.get(handle.worktree_id)
            rejected = self._check_current(current, handle)
            if rejected is not None:
                return rejected
            self._leases.update_status_in_transaction(
                handle.worktree_id, status=LeaseStatus.ACTIVE, heartbeat_at=now_iso,
            )
            self._event(handle.task_id, EventType.LEASE_RENEWED, {
                "worktree_id": handle.worktree_id, "generation": handle.generation,
            })
        return LeaseResult(Decision.ALLOW, "renewed", handle)

    def release(self, handle: LeaseHandle) -> LeaseResult:
        """Only the current holder may release; releasing never touches
        task state, checkpoints, or unresolved operations."""
        if not isinstance(handle, LeaseHandle):
            return _deny("malformed_lease_handle")
        with transaction(self._conn):
            current = self._leases.get(handle.worktree_id)
            rejected = self._check_current(current, handle)
            if rejected is not None:
                return rejected
            self._leases.update_status_in_transaction(
                handle.worktree_id, status=LeaseStatus.RELEASED,
            )
            self._event(handle.task_id, EventType.LEASE_RELEASED, {
                "worktree_id": handle.worktree_id, "generation": handle.generation,
            })
        return LeaseResult(Decision.ALLOW, "released")

    def expire_if_stale(self, worktree_id: str) -> LeaseResult:
        """Durably mark an ACTIVE-but-past-TTL lease `EXPIRED`, without
        acquiring it — makes staleness visible (e.g. to generic recovery)
        even before any new session takes over."""
        if not isinstance(worktree_id, str) or not worktree_id:
            return _deny("malformed_lease_request")
        now = _parse_iso(self._now_fn())
        with transaction(self._conn):
            current = self._leases.get(worktree_id)
            if current is None:
                return _deny("no_lease")
            if not self._sane(current):
                return _deny("malformed_persisted_lease")
            if current.status != LeaseStatus.ACTIVE:
                return _deny("not_active")
            if not self._is_expired(current, now):
                return _deny("not_expired")
            self._leases.update_status_in_transaction(worktree_id, status=LeaseStatus.EXPIRED)
            self._event(current.task_id, EventType.LEASE_EXPIRED, {
                "worktree_id": worktree_id, "generation": current.generation,
                "reason": "explicit_expire_if_stale",
            })
        return LeaseResult(Decision.ALLOW, "expired")

    def is_current(self, handle: LeaseHandle) -> bool:
        """Read-only fencing check: does `handle` still name the exact
        current ownership epoch? Safe to call from inside a caller's own
        already-open write transaction (a plain read, no transaction of
        its own) — this is the check every mutation path (Phase 4/5) must
        pass before proceeding. Emits no audit event of its own; the
        caller records the denial with its own operation context."""
        if not isinstance(handle, LeaseHandle):
            return False
        current = self._leases.get(handle.worktree_id)
        if current is None or not self._sane(current):
            return False
        return current.status == LeaseStatus.ACTIVE and _epoch(current) == _epoch(handle)
