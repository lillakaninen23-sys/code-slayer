"""Durable worker/model trust: LOCKED / GUARDED / AUTO (Phase 7.2 —
`docs/ROADMAP.md#local-worker-runtime`, `docs/CODE_SLAYER_VISION.md`
§42).

## Absence of evidence is LOCKED — never guessed upward

Current trust is *derived*, not stored as a mutable status: it is
whatever the *latest* `worker_trust_events` row for an exact
`(worker_id, role, capability)` scope says, or `TrustLevel.LOCKED` when
there is no such row at all. There is deliberately no "default" column
value anywhere that could silently read as `GUARDED` or `AUTO` — an
unknown worker, an unrecorded role, an unrecorded capability all
converge on the same safe answer: `LOCKED`.

## Scoping: deliberately minimal for Phase 7.2

Trust is scoped by `(worker_id, role, capability)` only — not a
separately normalized model/provider/runtime/version identity, even
though `docs/CODE_SLAYER_VISION.md` §42 eventually wants that finer
grain. `worker_id` is trusted, for now, to identify one concrete
configured worker; splitting that identity further later needs a new
column/table, never a redesign of the transition semantics here. Scope
matching is always *exact* — a capability-scoped grant never implies
anything about a different capability, and a role-scoped grant never
implies anything about a different role (no implicit inheritance in
this slice; see `WorkerTrustManager.current_trust`).

## What Phase 7.2 does and does not grant

Every upward transition this phase exposes requires a non-blank
`evidence_ref` — but Phase 7.2 only checks that *a* reference was
supplied, never that it points at a passing conformance run (the
conformance store does not exist yet; that verification is Phase 7.3's
job). The only upward transition reachable through this module's public
API at all is `LOCKED -> GUARDED`. `AUTO` exists in `TrustLevel` for
forward compatibility and *is* a valid downgrade target/source
internally, but no public method here can ever promote anything to it —
broad autonomous operation is a later phase's decision, not this one's.

Downward transitions require only a `reason`, never evidence: losing
trust is deliberately easier than gaining it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.worker_trust_repo import TrustLevel, WorkerTrustRepo
from code_slayer.store.workers_repo import WorkersRepo

# The complete Phase 7.2 transition table. LOCKED->AUTO and GUARDED->AUTO
# are deliberately absent: broad AUTO autonomy is not reachable through
# any transition this phase permits, direct or otherwise.
_ALLOWED_TRANSITIONS = frozenset({
    (TrustLevel.LOCKED, TrustLevel.GUARDED),
    (TrustLevel.GUARDED, TrustLevel.LOCKED),
    (TrustLevel.AUTO, TrustLevel.LOCKED),
    (TrustLevel.AUTO, TrustLevel.GUARDED),
})


def _is_allowed_transition(from_level: TrustLevel, to_level: TrustLevel) -> bool:
    return (from_level, to_level) in _ALLOWED_TRANSITIONS


@dataclass(frozen=True)
class TrustResult:
    ok: bool
    reason: str
    level: TrustLevel | None = None


def _deny(reason: str) -> TrustResult:
    return TrustResult(False, reason)


class WorkerTrustManager:
    def __init__(self, conn: sqlite3.Connection, *, now_fn=utcnow_iso) -> None:
        self._conn = conn
        self._workers = WorkersRepo(conn)
        self._trust = WorkerTrustRepo(conn)
        self._audit = AuditWriter(conn)
        self._now_fn = now_fn

    # -- derivation -------------------------------------------------------

    def current_trust(
        self, worker_id: str, role: str, capability: str | None = None,
    ) -> TrustLevel:
        """`LOCKED` whenever no history exists for this exact scope —
        the one rule every other method in this class is built on."""
        event = self._trust.latest_for_scope(worker_id, role, capability)
        return TrustLevel(event.to_level) if event is not None else TrustLevel.LOCKED

    def history(self, worker_id: str, role: str, capability: str | None = None):
        return self._trust.history_for_scope(worker_id, role, capability)

    def latest_event(self, worker_id: str, role: str, capability: str | None = None):
        """The single most recent trust event for this exact scope, or
        `None` if this scope has no history at all. Used by
        `workers.promotion` to determine evidence freshness: since
        `current_trust()` is derived from exactly this same row, a
        non-`None` result here — at the moment a `LOCKED` scope is being
        considered for promotion — is necessarily the event that most
        recently brought it *to* `LOCKED` (a downgrade, or an initial
        state with nothing to be stale relative to when `None`)."""
        return self._trust.latest_for_scope(worker_id, role, capability)

    # -- the one public upward transition -----------------------------------

    def promote_to_guarded(
        self, *, worker_id: str, role: str, capability: str | None = None,
        reason: str, evidence_ref: str,
    ) -> TrustResult:
        """`LOCKED -> GUARDED`, requiring a non-blank `evidence_ref`.
        Phase 7.2 does not verify `evidence_ref` points at a passing
        conformance run — see the module docstring."""
        if not self._sane_scope(worker_id, role, capability, reason):
            return _deny("malformed_trust_request")
        if not isinstance(evidence_ref, str) or not evidence_ref.strip():
            return _deny("missing_evidence_reference")
        if self._workers.get(worker_id) is None:
            return _deny("unknown_worker")
        return self._transition(
            worker_id=worker_id, role=role, capability=capability,
            allowed_from=frozenset({TrustLevel.LOCKED}), to_level=TrustLevel.GUARDED,
            reason=reason, evidence_ref=evidence_ref,
        )

    # -- downgrades: reason required, no evidence required -------------------

    def downgrade_to_locked(
        self, *, worker_id: str, role: str, capability: str | None = None,
        reason: str, evidence_ref: str | None = None,
    ) -> TrustResult:
        """`GUARDED -> LOCKED` or `AUTO -> LOCKED`. Losing trust never
        requires positive evidence — only a reason, matching the module
        docstring's "losing trust is easier than gaining it" posture."""
        if not self._sane_scope(worker_id, role, capability, reason):
            return _deny("malformed_trust_request")
        if self._workers.get(worker_id) is None:
            return _deny("unknown_worker")
        return self._transition(
            worker_id=worker_id, role=role, capability=capability,
            allowed_from=frozenset({TrustLevel.GUARDED, TrustLevel.AUTO}),
            to_level=TrustLevel.LOCKED, reason=reason, evidence_ref=evidence_ref,
        )

    def downgrade_to_guarded(
        self, *, worker_id: str, role: str, capability: str | None = None,
        reason: str, evidence_ref: str | None = None,
    ) -> TrustResult:
        """`AUTO -> GUARDED`. Unreachable in ordinary Phase 7.2 operation
        — nothing in this phase's public API can ever grant `AUTO` in
        the first place — but the downgrade path is implemented now so a
        later phase that *can* grant `AUTO` inherits a correct, already-
        tested way to step it back down."""
        if not self._sane_scope(worker_id, role, capability, reason):
            return _deny("malformed_trust_request")
        if self._workers.get(worker_id) is None:
            return _deny("unknown_worker")
        return self._transition(
            worker_id=worker_id, role=role, capability=capability,
            allowed_from=frozenset({TrustLevel.AUTO}), to_level=TrustLevel.GUARDED,
            reason=reason, evidence_ref=evidence_ref,
        )

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _sane_scope(
        worker_id: object, role: object, capability: object, reason: object,
    ) -> bool:
        if not isinstance(worker_id, str) or not worker_id:
            return False
        if not isinstance(role, str) or not role:
            return False
        if capability is not None and (not isinstance(capability, str) or not capability):
            return False
        return isinstance(reason, str) and bool(reason.strip())

    def _transition(
        self, *, worker_id: str, role: str, capability: str | None,
        allowed_from: frozenset[TrustLevel], to_level: TrustLevel,
        reason: str, evidence_ref: str | None,
    ) -> TrustResult:
        """Open this connection's one write transaction and perform the
        transition inside it. Two callers racing for the same scope can
        never both durably record the same upward transition; SQLite's
        own write lock plus `_transition_in_transaction()`'s fresh
        re-check serializes them into exactly one winner, mirroring
        `lease.manager.LeaseManager`'s pattern."""
        occurred_at = self._now_fn()
        with transaction(self._conn):
            return self._transition_in_transaction(
                worker_id=worker_id, role=role, capability=capability,
                allowed_from=allowed_from, to_level=to_level,
                reason=reason, evidence_ref=evidence_ref, occurred_at=occurred_at,
            )

    def _transition_in_transaction(
        self, *, worker_id: str, role: str, capability: str | None,
        allowed_from: frozenset[TrustLevel], to_level: TrustLevel,
        reason: str, evidence_ref: str | None, occurred_at: str,
    ) -> TrustResult:
        """The write half of `_transition()`, usable by a caller that
        already holds this connection's one open write transaction —
        mirrors `store.task_repo.TaskRepo._record_transition_in_transaction`
        / `core.state_machine.TaskStateMachine.transition_in_transaction`'s
        established "composition point" pattern.

        Phase 7.7c: `workers.promotion.promote_from_conformance()` calls
        this directly so that re-reading the conformance run, re-reading
        the latest trust event for freshness, and this trust write are
        all one serialized control-plane decision — no freshness-
        sensitive read the promotion decision depends on may be taken
        before the write lock is acquired and then trusted afterward.
        `current_trust()` here is what closes that gap: it is read fresh,
        under the write lock this method requires already be open, right
        before writing, so a concurrent downgrade that committed while a
        caller was still validating other evidence is never missed.

        Raises if no transaction is open on this connection — this method
        never opens or commits one itself.
        """
        if not self._conn.in_transaction:
            raise RuntimeError("trust transition requires an open write transaction")
        if self._workers.get(worker_id) is None:
            return _deny("unknown_worker")
        current = self.current_trust(worker_id, role, capability)
        if current not in allowed_from:
            return _deny(f"trust_transition_not_eligible_from_{current.value.lower()}")
        if not _is_allowed_transition(current, to_level):
            # Defense in depth: every caller above only ever reaches
            # here with an (allowed_from, to_level) pair already
            # present in _ALLOWED_TRANSITIONS, but this makes that an
            # enforced invariant, not merely trusted caller discipline.
            return _deny("transition_not_permitted_in_phase_7_2")
        event = self._trust.append_in_transaction(
            worker_id=worker_id, role=role, capability=capability,
            from_level=current.value, to_level=to_level.value,
            reason=reason, evidence_ref=evidence_ref, occurred_at=occurred_at,
        )
        self._audit.append(
            task_id=None, event_type=EventType.WORKER_TRUST_CHANGED,
            actor_type="system", actor_id=worker_id,
            payload={
                "worker_id": worker_id, "role": role, "capability": capability,
                "from_level": current.value, "to_level": to_level.value,
                "reason": reason, "evidence_ref": evidence_ref,
                "trust_event_id": event.id,
            },
        )
        return TrustResult(True, "trust_transition_recorded", level=to_level)
