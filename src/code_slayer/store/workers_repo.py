"""Minimal repository over `workers` (schema v1, schema-only since
Phase 1 — see `adr/0006-worker-model-local-first.md`).

Phase 7.2 adds only the smallest access layer `worker_trust_events`
needs to reference a real row: registering a worker (idempotently) and
reading one back. This is deliberately not a full registry — no
availability probing, no capability-list management, no update path for
an already-registered worker's own fields. Those remain out of scope
until something in this codebase actually needs them.
"""

from __future__ import annotations

import sqlite3

from code_slayer.store.db import transaction
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
        no-op — the existing row (whatever its current fields are) is
        returned unchanged. This repo has no update path for a worker's
        own registered fields; that is out of Phase 7.2's minimal scope.
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


def _row_to_worker(row: sqlite3.Row) -> Worker:
    return Worker(
        worker_id=row["worker_id"],
        kind=row["kind"],
        network_class=row["network_class"],
        capabilities_json=row["capabilities_json"],
        availability_state=row["availability_state"],
        available_after=row["available_after"],
        last_probe_at=row["last_probe_at"],
        last_error=row["last_error"],
    )
