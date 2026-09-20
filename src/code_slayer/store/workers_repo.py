"""Minimal repository over `workers` (schema v1, schema-only since
Phase 1 — see `adr/0006-worker-model-local-first.md`; lifecycle columns
added schema v18, H.3).

Phase 7.2 adds only the smallest access layer `worker_trust_events`
needs to reference a real row: registering a worker (idempotently) and
reading one back. This is deliberately not a full registry — no
availability probing, no capability-list management, no update path for
an already-registered worker's own fields. Those remain out of scope
until something in this codebase actually needs them.

## Lifecycle (H.3)

`lifecycle_state` (`WorkerLifecycleState.ACTIVE`/`ARCHIVED`) is a
SEPARATE dimension from `availability_state` (runtime health/
reachability, above): it is the durable, administrative record of
whether a worker may receive new production/certification work at all.
This repository owns only the mechanical read/write of that column —
never the legality of a transition (whether active work exists, what
to check before archiving, what to audit). That policy lives in
`code_slayer.workers.lifecycle`, which is the one caller authorized to
invoke `archive_in_transaction()`/`reactivate_in_transaction()` for a
real administrative transition. `archive()`/`reactivate()` (below) are
thin single-operation convenience wrappers for callers with no other
work to join into the same transaction (e.g. tests) — production code
paths that also need an active-work check or an audit event in the
same commit MUST use the `*_in_transaction()` forms.

`register()`'s existing `ON CONFLICT(worker_id) DO NOTHING` already
means re-registering an already-ARCHIVED worker is a genuine no-op: no
column on the existing row, including `lifecycle_state`, is ever
touched. This is deliberate and is what makes a service restart or a
config reload structurally unable to reactivate an archived worker —
see the module's own test coverage for the regression this guards."""

from __future__ import annotations

import sqlite3

from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.models import Worker


class AvailabilityState:
    UNKNOWN = "UNKNOWN"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"

    ALL = frozenset({UNKNOWN, AVAILABLE, UNAVAILABLE})


class NetworkClass:
    LOCAL = "local"
    CLOUD = "cloud"

    ALL = frozenset({LOCAL, CLOUD})


class WorkerLifecycleState:
    """H.3: the durable administrative worker lifecycle (schema v18).
    See the module docstring's "Lifecycle" section. Code MUST reject
    every value outside `ALL` — never silently coerced or wildcarded."""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"

    ALL = frozenset({ACTIVE, ARCHIVED})


class WorkersRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, worker_id: str) -> Worker | None:
        row = self._conn.execute(
            "SELECT * FROM workers WHERE worker_id = ?", (worker_id,),
        ).fetchone()
        return _row_to_worker(row) if row is not None else None

    def register(self, *, worker_id: str, kind: str, network_class: str) -> Worker:
        """Idempotent: registering an already-registered `worker_id` is a
        no-op — the existing row (whatever its current fields are,
        including `lifecycle_state`) is returned unchanged. This repo
        has no update path for a worker's own registered fields; that
        is out of Phase 7.2's minimal scope.
        """
        if not isinstance(worker_id, str) or not worker_id:
            raise ValueError("worker_id must be a non-empty string")
        if not isinstance(kind, str) or not kind:
            raise ValueError("kind must be a non-empty string")
        if network_class not in NetworkClass.ALL:
            raise ValueError(f"unknown network_class: {network_class!r}")
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO workers (worker_id, kind, network_class, availability_state) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(worker_id) DO NOTHING",
                (worker_id, kind, network_class, AvailabilityState.UNKNOWN),
            )
        worker = self.get(worker_id)
        assert worker is not None
        return worker

    def is_active(self, worker_id: str) -> bool:
        worker = self.get(worker_id)
        return worker is not None and worker.lifecycle_state == WorkerLifecycleState.ACTIVE

    def _transition_in_transaction(
        self, worker_id: str, *, to_state: str,
    ) -> tuple[Worker | None, bool]:
        """Shared mechanics for `archive_in_transaction()`/`reactivate_
        in_transaction()`. Returns `(worker, changed)`: `worker` is
        `None` only if `worker_id` is not registered; `changed` is
        `False` for an idempotent no-op (already in `to_state`) so a
        caller can skip emitting a duplicate audit event, and `True`
        only when a real transition was just committed."""
        if not self._conn.in_transaction:
            raise RuntimeError(
                f"{'archive' if to_state == WorkerLifecycleState.ARCHIVED else 'reactivate'}"
                "_in_transaction requires an open write transaction",
            )
        current = self.get(worker_id)
        if current is None:
            return None, False
        if current.lifecycle_state == to_state:
            return current, False
        self._conn.execute(
            "UPDATE workers SET lifecycle_state = ?, lifecycle_changed_at = ? "
            "WHERE worker_id = ?",
            (to_state, utcnow_iso(), worker_id),
        )
        return self.get(worker_id), True

    def archive_in_transaction(self, worker_id: str) -> tuple[Worker | None, bool]:
        """`ACTIVE -> ARCHIVED`. Must run inside a transaction the
        caller already opened (see the module docstring) — this method
        never opens or commits one itself, so a caller can join a
        production active-work check and an audit-event append into
        the exact same atomic commit. Idempotent: already `ARCHIVED`
        returns `(worker, False)`, never a second lifecycle write."""
        return self._transition_in_transaction(
            worker_id, to_state=WorkerLifecycleState.ARCHIVED,
        )

    def reactivate_in_transaction(self, worker_id: str) -> tuple[Worker | None, bool]:
        """`ARCHIVED -> ACTIVE`. Same transactional contract as
        `archive_in_transaction()`. Idempotent: already `ACTIVE`
        returns `(worker, False)`."""
        return self._transition_in_transaction(
            worker_id, to_state=WorkerLifecycleState.ACTIVE,
        )

    def archive(self, worker_id: str) -> tuple[Worker | None, bool]:
        """Convenience single-operation form: opens its own transaction.
        Never call this from inside another open transaction (use
        `archive_in_transaction()` there instead) — production code
        that also needs an active-work check or an audit event in the
        same commit MUST use `archive_in_transaction()` via `code_
        slayer.workers.lifecycle`, never this method."""
        with transaction(self._conn):
            return self.archive_in_transaction(worker_id)

    def reactivate(self, worker_id: str) -> tuple[Worker | None, bool]:
        """Convenience single-operation form of `reactivate_in_
        transaction()` — see `archive()`'s own docstring."""
        with transaction(self._conn):
            return self.reactivate_in_transaction(worker_id)


def _row_to_worker(row: sqlite3.Row) -> Worker:
    # `lifecycle_state`/`lifecycle_changed_at` (schema v18) are read
    # defensively: some existing migration tests deliberately construct
    # a `WorkersRepo` against a database frozen at a pre-v18 schema
    # version (see `tests/unit/test_promotion_provenance_migration.py`)
    # to exercise a LATER migration against realistic pre-existing
    # data. `Worker`'s own dataclass defaults (`"ACTIVE"`/`None`) are
    # exactly what a not-yet-migrated row means: no lifecycle column
    # exists yet, so nothing has ever been archived.
    keys = row.keys()
    return Worker(
        worker_id=row["worker_id"],
        kind=row["kind"],
        network_class=row["network_class"],
        capabilities_json=row["capabilities_json"],
        availability_state=row["availability_state"],
        available_after=row["available_after"],
        last_probe_at=row["last_probe_at"],
        last_error=row["last_error"],
        lifecycle_state=row["lifecycle_state"] if "lifecycle_state" in keys else "ACTIVE",
        lifecycle_changed_at=(
            row["lifecycle_changed_at"] if "lifecycle_changed_at" in keys else None
        ),
    )
