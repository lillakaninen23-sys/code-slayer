"""Conformance-gated trust promotion — the normal, safe public route to
`LOCKED -> GUARDED` (Phase 7.3).

## The boundary this module exists to draw

`workers.trust.WorkerTrustManager.promote_to_guarded()` is the Phase 7.2
trust *primitive*: it only checks that a non-blank `evidence_ref` string
was supplied, never that the string means anything. That was a
deliberate, documented Phase 7.2 boundary — real verification needed a
conformance store that did not exist yet.

It still does not, and still should not, verify evidence — Phase 7.2's
tests and semantics are unchanged by this module. What changes is that
application code now has a better, safer route: **`promote_from_
conformance()`, not `promote_to_guarded()` with a hand-picked string, is
the intended normal path for conformance-based promotion.** This module
does the real verification work Phase 7.2 always deferred, then calls
the Phase 7.2 primitive with a concrete, checked `run_id` as
`evidence_ref` — the primitive is never bypassed, only fronted by a
gate that actually earns the right to call it.

## What `promote_from_conformance` verifies before ever touching trust

1. the run exists
2. the run belongs to the exact `worker_id` being promoted
3. the run belongs to the exact `role` being promoted
4. **the requested capability scope is one the suite actually, concretely
   tested — fail closed, with no default-allow path**:
   - `capability=None` (role-level) is refused outright. Phase 7.2's
     exact-scope semantics already mean `capability=None` never
     implicitly grants `read_file`/`run_command`/`write_file`/etc — this
     module additionally refuses to let conformance evidence promote the
     role-level scope *at all*, because nothing about "the fixed suite
     passed" is evidence for a scope broader than the one concrete
     capability the suite exercises. Explicit, tested capability scope
     is required every time.
   - an unrecognized capability name is refused (`unknown_capability`) —
     never treated the same as "known and non-mutating."
   - a known *mutating* capability is refused
     (`mutation_capability_not_conformance_tested`) — nothing in the
     fixed Phase 7.3 suite ever tests mutation (`docs/CODE_SLAYER_VISION.
     md` §37 isolation doesn't exist yet).
   - a known, non-mutating capability the suite did not actually
     exercise is refused (`capability_not_covered_by_suite`) — being
     "non-mutating" is necessary but not sufficient; only
     `conformance.PROMOTABLE_CAPABILITIES` (currently just `read_file`)
     may be promoted from a `phase7.3-v1` run.
5. the run is finalized (not `RUNNING`)
6. the run's status is `PASSED`
7. every required case (`workers.conformance.REQUIRED_CASES`) has a
   recorded result in *this* run
8. every required case's recorded result is `passed`
9. the run has not been invalidated — Phase 7.3 has no separate
   invalidation flag or mechanism (a finalized run cannot be silently
   rewritten — see `worker_conformance_runs_no_mutate_finalized` — so a
   run's own durable `status` *is* its complete validity record; a
   future phase that needs to invalidate historical evidence without
   lying about what happened would add a new, separately durable event
   referencing this `run_id`, never a mutation of it)
10. the run's `suite_version` matches this code's current `conformance.
    SUITE_VERSION` — an older or newer suite's claims are never silently
    reused
11. **the run is not stale relative to this exact scope's trust
    history**: if `(worker_id, role, capability)` has *any* prior trust
    event at all, the run must have *begun* (`started_at`) strictly
    after that event's `occurred_at` — an equal or earlier timestamp
    fails closed as stale, never treated as "close enough." This is what
    stops an old `PASSED` run — the exact one already used, or a
    different one that merely finished before a later downgrade — from
    re-promoting a worker that was downgraded back to `LOCKED` after it:
    a fresh conformance run is required post-downgrade, always. Ordering
    is decided by direct string comparison of the two canonical
    `store.db.utcnow_iso()`-format timestamps, which is exact for that
    fixed-width ISO-8601 UTC format — never a parsed/derived comparison
    that could disagree with what was actually durably recorded.

Only once every check above holds does this call the Phase 7.2
primitive at all.

## Phase 7.7c: one serialized control-plane decision

Every check above — steps 1-11 — now runs *inside* the same `BEGIN
IMMEDIATE` write transaction that, if every check passes, performs the
actual `LOCKED -> GUARDED` write (`workers.trust.WorkerTrustManager.
_transition_in_transaction()`). Before this phase, step 11's freshness
check (and every read before it) ran as plain, unserialized reads, and
only the final write itself (inside `promote_to_guarded()`'s own,
*separately opened* transaction) was atomic. An independent audit
reproduced the resulting gap: a promotion caller could read a `PASSED`
run, verify it was fresh relative to the trust history *at that moment*,
then pause (in wall-clock terms — nothing here ever needed a real
`sleep`, just an unlucky scheduler) while a concurrent downgrade
committed, then resume and call `promote_to_guarded()` — which only
re-checks *current trust level* (now `LOCKED` again, thanks to the
downgrade, which is exactly what `promote_to_guarded()`'s own allowed-
from-`LOCKED` check wants to see), never re-checking freshness against
the *now-current* latest trust event. A stale run would then durably
re-promote a worker straight back to `GUARDED` the instant after it was
downgraded.

Closing this needs no new lock: `store.db.transaction()`'s existing
`BEGIN IMMEDIATE` write-lock discipline already makes the control-plane
database itself the single serialization point every other concurrency
fix in this codebase (leases, tasks, job-worktree cleanup claims) relies
on. `promote_from_conformance()` now opens that transaction itself, before
its very first read of the conformance run, and does not release it
until either a terminal denial is returned or the trust write (and its
audit event) has been appended — so a concurrent downgrade for the same
`(worker_id, role, capability)` scope can only ever land *entirely
before* this transaction starts (in which case the freshness re-read
inside it sees the downgrade and correctly denies as stale) or *entirely
after* it commits or is denied (in which case it simply downgrades
whatever this transaction left behind) — never in between. See `docs/
CODE_SLAYER_VISION.md` §42 and `tests/unit/test_worker_conformance.py`'s
concurrency section for the exact interleaving this closes.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from code_slayer.store.conformance_repo import ConformanceRepo, ConformanceRunStatus
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.tools.registry import CAPABILITIES
from code_slayer.workers.conformance import PROMOTABLE_CAPABILITIES, REQUIRED_CASES, SUITE_VERSION
from code_slayer.workers.trust import TrustLevel, TrustResult, WorkerTrustManager


def _deny(reason: str) -> TrustResult:
    return TrustResult(False, reason)


def _capability_scope_gate(capability: str | None) -> TrustResult | None:
    """`None` on success; a terminal denial otherwise. Fails closed for
    every case except a capability the fixed suite explicitly, concretely
    covers — there is no default-allow branch here, deliberately:
    an unrecognized name must never be treated the same as a known,
    harmless one."""
    if capability is None:
        return _deny("role_level_promotion_not_supported_by_conformance")
    if capability not in CAPABILITIES:
        return _deny("unknown_capability")
    if CAPABILITIES[capability].mutation:
        return _deny("mutation_capability_not_conformance_tested")
    if capability not in PROMOTABLE_CAPABILITIES:
        return _deny("capability_not_covered_by_suite")
    return None


def promote_from_conformance(
    conn: sqlite3.Connection, *, worker_id: str, role: str, capability: str | None = None,
    run_id: str, reason: str = "conformance_suite_passed",
    now_fn: Callable[[], str] = utcnow_iso,
) -> TrustResult:
    """Verify `run_id` is genuine, complete, passing evidence for exactly
    `(worker_id, role, capability)`, then perform the `LOCKED -> GUARDED`
    write — all as one serialized control-plane decision. See the module
    docstring ("Phase 7.7c: one serialized control-plane decision") for
    why every check, not only the final write, must happen inside the
    same transaction, and the ordered check list above it for what is
    checked. Any failure denies before trust is ever touched.

    `now_fn` (default `store.db.utcnow_iso`) is the same injectable-clock
    seam `WorkerTrustManager` itself already exposes — tests that need to
    control exactly when this promotion's own trust event is recorded
    (to construct a precise, deterministic ordering against a later
    conformance run or trust event) pass a fake clock here, exactly as
    they already do to `WorkerTrustManager(conn, now_fn=...)`.
    """
    if not isinstance(worker_id, str) or not worker_id:
        return _deny("malformed_promotion_request")
    if not isinstance(role, str) or not role:
        return _deny("malformed_promotion_request")
    if capability is not None and (not isinstance(capability, str) or not capability):
        return _deny("malformed_promotion_request")
    if not isinstance(run_id, str) or not run_id:
        return _deny("malformed_promotion_request")

    gate = _capability_scope_gate(capability)
    if gate is not None:
        return gate

    manager = WorkerTrustManager(conn, now_fn=now_fn)
    conformance = ConformanceRepo(conn)

    with transaction(conn):
        # Every read below is taken fresh, under this transaction's own
        # write lock — nothing read before `transaction(conn)` was
        # entered is trusted for the decision made inside it (see this
        # module's docstring). `worker_conformance_runs`/`_results` rows
        # are immutable once finalized (`worker_conformance_runs_no_
        # mutate_finalized`), so re-reading them here is defense in
        # depth, not merely ceremony; `worker_trust_events` is exactly
        # the mutable table a concurrent actor could have changed, which
        # is why its own latest-event read must happen here too.
        run = conformance.get_run(run_id)
        if run is None:
            return _deny("unknown_conformance_run")
        if run.worker_id != worker_id:
            return _deny("run_belongs_to_different_worker")
        if run.role != role:
            return _deny("run_belongs_to_different_role")
        if run.status == ConformanceRunStatus.RUNNING:
            return _deny("run_not_finalized")
        if run.status != ConformanceRunStatus.PASSED:
            return _deny("run_not_passed")
        if run.suite_version != SUITE_VERSION:
            return _deny("run_suite_version_outdated")

        results = conformance.list_results(run_id)
        passed_cases = {r.case_name for r in results if r.passed}
        if not REQUIRED_CASES.issubset(passed_cases):
            return _deny("required_conformance_cases_missing_or_failed")

        latest_event = manager.latest_event(worker_id, role, capability)
        if latest_event is not None and run.started_at <= latest_event.occurred_at:
            # Strict `>` required: an equal or earlier run.started_at
            # means this run cannot prove anything about the worker's
            # behavior *after* the most recent trust change for this
            # exact scope — including, critically, after a downgrade
            # back to LOCKED that committed while this transaction was
            # waiting to acquire the write lock. Fails closed on ties,
            # never treats "same instant" as fresh enough.
            return _deny("run_stale_relative_to_latest_trust_event")

        return manager._transition_in_transaction(
            worker_id=worker_id, role=role, capability=capability,
            allowed_from=frozenset({TrustLevel.LOCKED}), to_level=TrustLevel.GUARDED,
            reason=reason, evidence_ref=run_id, occurred_at=now_fn(),
        )
