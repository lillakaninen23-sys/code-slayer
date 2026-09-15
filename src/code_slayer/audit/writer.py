"""AuditWriter: the sole write path into `audit_events`.

Foundation Plan §07/§08, INV-3/INV-4. Standalone appends own a BEGIN
IMMEDIATE transaction; appends within a caller's write transaction compose
with it. Sequence allocation, previous hash selection and insertion are
serialized even for the NULL/system chain, independently of UNIQUE indexes.
"""

from __future__ import annotations

import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from code_slayer.audit.canonical import canonical_json, event_hash
from code_slayer.audit.events import EventType
from code_slayer.store.db import transaction


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class AuditEventRecord:
    id: int
    task_id: str | None
    seq: int
    event_type: str
    occurred_at: str
    actor_type: str
    actor_id: str | None
    payload: dict[str, Any]
    prev_event_hash: str | None
    event_hash: str


class AuditWriter:
    """The only class permitted to INSERT into `audit_events`."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def _next_seq_and_prev_hash(self, task_id: str | None) -> tuple[int, str | None]:
        row = self._conn.execute(
            "SELECT seq, event_hash FROM audit_events WHERE task_id IS ? "
            "ORDER BY seq DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if row is None:
            return 1, None
        return row["seq"] + 1, row["event_hash"]

    def append(
        self,
        *,
        task_id: str | None,
        event_type: EventType | str,
        actor_type: str,
        actor_id: str | None,
        payload: dict[str, Any],
        occurred_at: str | None = None,
    ) -> AuditEventRecord:
        """Append atomically, joining an existing caller-owned write transaction."""
        with nullcontext() if self._conn.in_transaction else transaction(self._conn):
            return self._append_in_transaction(
                task_id=task_id, event_type=event_type, actor_type=actor_type,
                actor_id=actor_id, payload=payload, occurred_at=occurred_at,
            )

    def _append_in_transaction(
        self, *, task_id, event_type, actor_type, actor_id, payload, occurred_at,
    ) -> AuditEventRecord:
        event_type_value = event_type.value if isinstance(event_type, EventType) else event_type
        occurred_at = occurred_at or utcnow_iso()
        seq, prev_hash = self._next_seq_and_prev_hash(task_id)
        digest = event_hash(
            task_id=task_id,
            seq=seq,
            event_type=event_type_value,
            occurred_at=occurred_at,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload,
            prev_event_hash=prev_hash,
        )
        cur = self._conn.execute(
            "INSERT INTO audit_events "
            "(task_id, seq, event_type, occurred_at, actor_type, actor_id, "
            " payload_json, prev_event_hash, event_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                seq,
                event_type_value,
                occurred_at,
                actor_type,
                actor_id,
                canonical_json(payload),
                prev_hash,
                digest,
            ),
        )
        assert cur.lastrowid is not None
        return AuditEventRecord(
            id=cur.lastrowid,
            task_id=task_id,
            seq=seq,
            event_type=event_type_value,
            occurred_at=occurred_at,
            actor_type=actor_type,
            actor_id=actor_id,
            payload=payload,
            prev_event_hash=prev_hash,
            event_hash=digest,
        )
