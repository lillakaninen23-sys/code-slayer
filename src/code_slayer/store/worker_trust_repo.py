"""Durable, append-only worker/model trust history (Phase 7.2,
`worker_trust_events` — migrations/0002_worker_trust.sql).

This module is a thin, transactional persistence primitive only — like
`store.lease_repo`, it does not decide whether a transition is legal.
`workers.trust.WorkerTrustManager` does, under the same
`store.db.transaction()` discipline every other repository in this
codebase already follows.

Current trust is never stored as a mutable status column: it is derived
by reading the *latest* event for an exact `(worker_id, role,
capability)` scope. Absence of any matching event means `LOCKED` — that
derivation lives in `workers.trust`, not here, but this module is what
makes "latest for an exact scope" a real, efficient query
(`ix_worker_trust_events_scope`).
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from code_slayer.store.models import WorkerTrustEvent


class TrustLevel(StrEnum):
    LOCKED = "LOCKED"
    GUARDED = "GUARDED"
    AUTO = "AUTO"


class WorkerTrustRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append_in_transaction(
        self,
        *,
        worker_id: str,
        role: str,
        capability: str | None,
        from_level: str,
        to_level: str,
        reason: str,
        evidence_ref: str | None,
        occurred_at: str,
    ) -> WorkerTrustEvent:
        """Append one durable trust event. Internal persistence primitive:
        the caller (`WorkerTrustManager`) has already decided, under an
        open write transaction, that this exact transition is legal —
        this method does not itself re-check transition legality, only
        that the two level values it is asked to persist are at least
        real trust levels."""
        if not self._conn.in_transaction:
            raise RuntimeError("trust event append requires an open write transaction")
        if from_level not in _ALL_LEVELS:
            raise ValueError(f"unknown trust level: {from_level!r}")
        if to_level not in _ALL_LEVELS:
            raise ValueError(f"unknown trust level: {to_level!r}")
        cur = self._conn.execute(
            "INSERT INTO worker_trust_events "
            "(worker_id, role, capability, from_level, to_level, reason, "
            " evidence_ref, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (worker_id, role, capability, from_level, to_level, reason, evidence_ref, occurred_at),
        )
        assert cur.lastrowid is not None
        return self.get(cur.lastrowid)

    def get(self, event_id: int) -> WorkerTrustEvent:
        row = self._conn.execute(
            "SELECT * FROM worker_trust_events WHERE id = ?", (event_id,),
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return _row_to_event(row)

    def latest_for_scope(
        self, worker_id: str, role: str, capability: str | None,
    ) -> WorkerTrustEvent | None:
        """The single most recent event for this *exact* scope — never a
        broader role- or worker-wide match when `capability` is given, and
        never a narrower one when it is not. `capability IS ?` (not `=`)
        so a `NULL`-scoped history is matched by `NULL`, not silently
        excluded (SQL `NULL = NULL` is never true)."""
        row = self._conn.execute(
            "SELECT * FROM worker_trust_events "
            "WHERE worker_id = ? AND role = ? AND capability IS ? "
            "ORDER BY id DESC LIMIT 1",
            (worker_id, role, capability),
        ).fetchone()
        return _row_to_event(row) if row is not None else None

    def history_for_scope(
        self, worker_id: str, role: str, capability: str | None,
    ) -> list[WorkerTrustEvent]:
        """Every event for this exact scope, oldest first — proves the
        append-only history remains fully queryable after later
        transitions, never truncated or overwritten."""
        rows = self._conn.execute(
            "SELECT * FROM worker_trust_events "
            "WHERE worker_id = ? AND role = ? AND capability IS ? "
            "ORDER BY id ASC",
            (worker_id, role, capability),
        ).fetchall()
        return [_row_to_event(row) for row in rows]


_ALL_LEVELS = frozenset({TrustLevel.LOCKED, TrustLevel.GUARDED, TrustLevel.AUTO})


def _row_to_event(row: sqlite3.Row) -> WorkerTrustEvent:
    return WorkerTrustEvent(
        id=row["id"],
        worker_id=row["worker_id"],
        role=row["role"],
        capability=row["capability"],
        from_level=row["from_level"],
        to_level=row["to_level"],
        reason=row["reason"],
        evidence_ref=row["evidence_ref"],
        occurred_at=row["occurred_at"],
    )
