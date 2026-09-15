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

*Expiry* (wall-clock: `heartbeat_at` vs. a TTL) decides whether a lease
becomes eligible to be *questioned*. *Fencing* (an exact identity/
generation match) decides whether a specific caller's claimed authority
is still valid. A clock that jumps or runs slow only ever affects when
questioning can begin; it can never make a stale generation compare equal
to the current one, and it can never by itself grant anyone a new epoch —
see quiescence below, which is what actually gates that.

## Quiescence: a new epoch is never granted while the old one might still
## be alive

An `ACTIVE` lease whose TTL has elapsed does **not** become directly
replaceable. `acquire()` first moves it to `QUIESCING` (same epoch,
unchanged), then evidence-checks whether the process that held it
(`worker_pid`/`worker_pid_started_at`, cross-referenced against `/proc`
via `lease.liveness` — never a bare pid, which PID reuse would make
unsafe) is actually gone:

```text
ACTIVE (generation N)
   │  TTL elapsed
   ▼
QUIESCING (generation N, same owner identity)
   │  liveness evidence: is the recorded process actually gone?
   ├─ ALIVE   -> deny takeover; stays QUIESCING (or reclaimed by renew())
   ├─ UNKNOWN -> deny takeover (fail closed); stays QUIESCING
   └─ GONE, no live recorded child either
        │
        ▼
      EXPIRED (generation N)
        │  a *subsequent* acquire attempt
        ▼
      ACTIVE (generation N+1) — a genuinely new epoch
```

A new generation is **only ever minted at the EXPIRED/RELEASED ->
ACTIVE step** — entering or leaving `QUIESCING` never burns a generation,
matching the durable fencing token's "strictly increasing, never wasted"
contract. Each arrow above is its own separately committed transaction
(never a single multi-step transaction spanning the liveness check, which
touches `/proc`, an external resource, outside any open transaction) so a
crash between any two steps leaves the row at the last *durably completed*
step, never a partial or invented one — `acquire()` internally advances
through as many consecutive steps as it can safely prove in one call, but
never skips the evidence gate.

The true, still-alive owner can always **reclaim** from `QUIESCING` by
calling `renew()` (or `release()`) with its still-matching epoch — the
authenticated call itself is stronger proof of liveness than any external
inference, and reclaiming is exactly how a merely-slow-to-heartbeat owner
avoids losing its lease to a takeover it was never actually unable to
contest. `is_current()` — the fencing gate every Phase 4/5 mutation path
checks — is unaffected by this: it requires `ACTIVE` specifically, so a
lease under quiescence review immediately stops authorizing new mutations
via that path even before quiescence resolves, which is exactly the
intended fail-safe behavior for the session whose liveness is in doubt.

No background daemon lives here: acquire/renew/release/`expire_if_stale`
are pure, explicit primitives a caller invokes; nothing here schedules
itself or polls.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.lease.liveness import Liveness, check_process_liveness
from code_slayer.lease.liveness import process_start_time as _real_process_start_time
from code_slayer.policy.engine import Decision
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.lease_repo import LeaseRepo, LeaseStatus
from code_slayer.store.models import WorkerLease

DEFAULT_LEASE_TTL_SECONDS = 300.0
_MAX_ACQUIRE_STEPS = 4  # fresh|EXPIRED->ACTIVE, or ACTIVE->QUIESCING->EXPIRED->ACTIVE + 1 spare


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
        liveness_fn=check_process_liveness, child_liveness_fn=check_process_liveness,
        process_start_time_fn=_real_process_start_time,
    ) -> None:
        self._conn = conn
        self._leases = LeaseRepo(conn)
        self._audit = AuditWriter(conn)
        self._ttl_seconds = ttl_seconds
        self._now_fn = now_fn
        # Injectable for tests: exercising real /proc-based liveness needs
        # real, separate processes to be meaningful (see
        # tests/integration/test_lease_liveness.py); the QUIESCING state
        # machine itself is tested against a deterministic fake here, the
        # same pattern already used for `now_fn`.
        self._liveness_fn = liveness_fn
        self._child_liveness_fn = child_liveness_fn
        self._process_start_time_fn = process_start_time_fn

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
        """`None` means the handle is currently valid for renew/release;
        otherwise the `LeaseResult` explaining why it is not. `ACTIVE` or
        `QUIESCING` both count here (unlike `is_current()`'s fencing gate,
        strictly `ACTIVE`-only): the true owner may always reclaim from
        quiescence review by proving itself via an authenticated call."""
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
        if current.status not in (LeaseStatus.ACTIVE, LeaseStatus.QUIESCING):
            return _deny("lease_not_active")
        return None

    def _has_live_child(self, worktree_id: str, generation: int) -> bool:
        """Conservative additional check before completing quiescence: an
        unresolved (STARTED/UNKNOWN) operation under this exact epoch
        whose recorded child subprocess still appears to exist. A worker
        process being gone does not by itself prove a subprocess it
        spawned is also gone (Foundation Plan §12's orphan-process
        concern) — though every capability's own bounded timeout already
        keeps this window small in practice (see
        `docs/LEASES_AND_RECOVERY.md`).

        `ALIVE` *and* `UNKNOWN` both block quiescence completion here —
        only a definite `GONE` permits it. This is deliberately more
        conservative than a plain existence check: we cannot prove a
        recorded pid is gone (rather than reused by an unrelated later
        process) without its `/proc`-derived start time, and when that
        start time could not be recorded (no `/proc` at spawn time, or
        the child had already exited), guessing "gone" would risk letting
        a takeover proceed alongside a subprocess that is, in fact, still
        running. Never guess; fail closed."""
        rows = self._conn.execute(
            "SELECT child_pid, child_pid_started_at FROM tool_operations "
            "WHERE worktree_id = ? AND lease_generation = ? "
            "AND status IN ('STARTED', 'UNKNOWN') AND child_pid IS NOT NULL",
            (worktree_id, generation),
        ).fetchall()
        return any(
            self._child_liveness_fn(row["child_pid"], row["child_pid_started_at"])
            != Liveness.GONE
            for row in rows
        )

    # -- public API ------------------------------------------------------

    def acquire(
        self, *, worktree_id: str, task_id: str, worker_id: str, worker_session_id: str,
    ) -> LeaseResult:
        """Acquire a fresh epoch, or take over a released/proven-gone one.

        An `ACTIVE` lease that merely looks TTL-expired is never directly
        replaced: this durably drives it through `QUIESCING` first and
        only grants a new epoch once the old one is confirmed `EXPIRED`
        (see this module's docstring for the full state machine). Denies
        (never guesses) whenever evidence is insufficient, or when the
        worktree is held, `ACTIVE`, and not expired by another session —
        or by the caller's own identity, which must use `renew()` instead.
        """
        if not all(isinstance(v, str) and v for v in (
            worktree_id, task_id, worker_id, worker_session_id,
        )):
            return _deny("malformed_lease_request")
        last_result: LeaseResult = _deny("quiescence_did_not_converge")
        for _ in range(_MAX_ACQUIRE_STEPS):
            current = self._leases.get(worktree_id)
            if current is not None and not self._sane(current):
                return self._reject_malformed(task_id, worktree_id)
            if current is None or current.status in (LeaseStatus.RELEASED, LeaseStatus.EXPIRED):
                return self._try_grant_new_epoch(
                    worktree_id, task_id, worker_id, worker_session_id, current,
                )
            if current.status == LeaseStatus.ACTIVE:
                now = _parse_iso(self._now_fn())
                if not self._is_expired(current, now):
                    if (current.worker_id, current.worker_session_id) == (
                        worker_id, worker_session_id,
                    ):
                        return _deny("already_held_by_caller_use_renew")
                    return _deny("lease_held_and_not_expired")
                advanced = self._try_begin_quiescence(worktree_id, current)
                if advanced is None:
                    continue  # someone else already changed it; re-observe
                last_result = advanced
                continue
            if current.status == LeaseStatus.QUIESCING:
                resolved = self._try_resolve_quiescence(worktree_id, current)
                if resolved is None:
                    continue  # advanced to EXPIRED (or state changed); re-observe
                return resolved  # a terminal denial: still alive / unknown / child alive
            return _deny("unexpected_lease_status")
        return last_result

    def _reject_malformed(self, task_id: str, worktree_id: str) -> LeaseResult:
        self._event(task_id, EventType.FENCE_STALE_REJECTED, {
            "worktree_id": worktree_id, "reason": "malformed_persisted_lease",
        })
        return _deny("malformed_persisted_lease")

    def _try_grant_new_epoch(
        self, worktree_id: str, task_id: str, worker_id: str, worker_session_id: str,
        expected: WorkerLease | None,
    ) -> LeaseResult:
        now_iso = self._now_fn()
        with transaction(self._conn):
            current = self._leases.get(worktree_id)
            if expected is None:
                if current is not None:
                    return _deny("lease_state_changed")
                new_generation = 1
            else:
                if (
                    current is None
                    or _epoch(current) != _epoch(expected)
                    or current.status != expected.status
                ):
                    return _deny("lease_state_changed")
                new_generation = current.generation + 1
            self._leases.upsert_in_transaction(
                worktree_id=worktree_id, task_id=task_id, worker_id=worker_id,
                worker_session_id=worker_session_id, generation=new_generation,
                acquired_at=now_iso, heartbeat_at=now_iso, status=LeaseStatus.ACTIVE,
                worker_pid=os.getpid(),
                worker_pid_started_at=self._process_start_time_fn(os.getpid()),
                checkpoint_id=None,
            )
            self._event(task_id, EventType.LEASE_ACQUIRED, {
                "worktree_id": worktree_id, "worker_id": worker_id,
                "worker_session_id": worker_session_id, "generation": new_generation,
            })
        handle = LeaseHandle(
            worktree_id, task_id, worker_id, worker_session_id, new_generation, now_iso,
        )
        return LeaseResult(Decision.ALLOW, "acquired", handle)

    def _try_begin_quiescence(
        self, worktree_id: str, expected: WorkerLease,
    ) -> LeaseResult | None:
        """Durably transition `ACTIVE` (TTL-expired) -> `QUIESCING`, same
        epoch, in its own committed transaction. Returns `None` on success
        (caller re-observes) or a denial if it lost a race."""
        with transaction(self._conn):
            current = self._leases.get(worktree_id)
            if (
                current is None or _epoch(current) != _epoch(expected)
                or current.status != LeaseStatus.ACTIVE
            ):
                return _deny("lease_state_changed")
            self._leases.update_status_in_transaction(worktree_id, status=LeaseStatus.QUIESCING)
            self._event(current.task_id, EventType.LEASE_QUIESCING, {
                "worktree_id": worktree_id, "generation": current.generation,
                "reason": "ttl_exceeded_pending_liveness_check",
            })
        return None

    def _try_resolve_quiescence(
        self, worktree_id: str, expected: WorkerLease,
    ) -> LeaseResult | None:
        """Evidence-check the quiescing owner's liveness (outside any
        transaction — this touches `/proc`, an external resource) and, if
        proven gone (and no live recorded child), durably complete
        `QUIESCING` -> `EXPIRED` in its own transaction. Returns `None` on
        successful advancement (caller re-observes and may then grant a
        new epoch) or a terminal denial otherwise."""
        liveness = self._liveness_fn(expected.worker_pid, expected.worker_pid_started_at)
        if liveness == Liveness.ALIVE:
            return _deny("quiescing_owner_still_alive")
        if liveness == Liveness.UNKNOWN:
            return _deny("quiescing_liveness_unknown")
        if self._has_live_child(worktree_id, expected.generation):
            return _deny("quiescing_child_process_alive")
        with transaction(self._conn):
            current = self._leases.get(worktree_id)
            if (
                current is None or _epoch(current) != _epoch(expected)
                or current.status != LeaseStatus.QUIESCING
            ):
                return _deny("lease_state_changed")
            self._leases.update_status_in_transaction(worktree_id, status=LeaseStatus.EXPIRED)
            self._event(current.task_id, EventType.LEASE_EXPIRED, {
                "worktree_id": worktree_id, "generation": current.generation,
                "reason": "quiescence_confirmed_gone",
            })
        return None

    def renew(self, handle: LeaseHandle) -> LeaseResult:
        """Refresh the heartbeat — and, if the lease had been placed under
        quiescence review, reclaim it back to `ACTIVE`: the authenticated
        call itself is proof of liveness stronger than any external
        inference."""
        if not isinstance(handle, LeaseHandle):
            return _deny("malformed_lease_handle")
        now_iso = self._now_fn()
        with transaction(self._conn):
            current = self._leases.get(handle.worktree_id)
            rejected = self._check_current(current, handle)
            if rejected is not None:
                return rejected
            was_quiescing = current.status == LeaseStatus.QUIESCING
            self._leases.update_status_in_transaction(
                handle.worktree_id, status=LeaseStatus.ACTIVE, heartbeat_at=now_iso,
            )
            self._event(handle.task_id, EventType.LEASE_RENEWED, {
                "worktree_id": handle.worktree_id, "generation": handle.generation,
                "reclaimed_from_quiescing": was_quiescing,
            })
        return LeaseResult(Decision.ALLOW, "renewed", handle)

    def release(self, handle: LeaseHandle) -> LeaseResult:
        """Only the current holder (`ACTIVE` or `QUIESCING`) may release;
        releasing never touches task state, checkpoints, or unresolved
        operations."""
        with transaction(self._conn):
            return self.release_in_transaction(handle)

    def release_in_transaction(self, handle: LeaseHandle) -> LeaseResult:
        """Compose the same fenced release with task terminalization and audit."""
        if not self._conn.in_transaction:
            raise RuntimeError("lease release requires an open write transaction")
        if not isinstance(handle, LeaseHandle):
            return _deny("malformed_lease_handle")
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
        """Advance a stale lease by exactly one quiescence step, without
        acquiring it: `ACTIVE`-past-TTL -> `QUIESCING`, or (if already
        `QUIESCING` and proven gone) -> `EXPIRED`. Makes staleness durably
        visible — e.g. to generic recovery or an operator — without
        needing a new owner ready to acquire immediately."""
        if not isinstance(worktree_id, str) or not worktree_id:
            return _deny("malformed_lease_request")
        current = self._leases.get(worktree_id)
        if current is None:
            return _deny("no_lease")
        if not self._sane(current):
            return _deny("malformed_persisted_lease")
        if current.status == LeaseStatus.ACTIVE:
            now = _parse_iso(self._now_fn())
            if not self._is_expired(current, now):
                return _deny("not_expired")
            result = self._try_begin_quiescence(worktree_id, current)
            return result if result is not None else LeaseResult(Decision.ALLOW, "quiescing")
        if current.status == LeaseStatus.QUIESCING:
            result = self._try_resolve_quiescence(worktree_id, current)
            return result if result is not None else LeaseResult(Decision.ALLOW, "expired")
        return _deny("not_active")

    def is_current(self, handle: LeaseHandle) -> bool:
        """Read-only fencing check: does `handle` still name the exact
        current ownership epoch? Safe to call from inside a caller's own
        already-open write transaction (a plain read, no transaction of
        its own) — this is the check every mutation path (Phase 4/5) must
        pass before proceeding. Deliberately `ACTIVE`-only: a lease under
        quiescence review must stop authorizing new mutations immediately,
        even before quiescence resolves either way (unlike `_check_current`,
        used by `renew`/`release`, which also accepts `QUIESCING` so the
        true owner can still reclaim it). Emits no audit event of its own;
        the caller records the denial with its own operation context."""
        if not isinstance(handle, LeaseHandle):
            return False
        current = self._leases.get(handle.worktree_id)
        if current is None or not self._sane(current):
            return False
        return current.status == LeaseStatus.ACTIVE and _epoch(current) == _epoch(handle)
