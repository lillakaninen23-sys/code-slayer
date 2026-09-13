"""Task persistence primitives (Foundation Plan §03/§06).

This module gives repository primitives only: create, read, and one
low-level `record_transition` that persists a new (state, phase) plus its
`STATE_TRANSITION` audit event atomically. It does not itself decide which
transitions are legal — the state-machine engine (Phase 2) will call this
primitive rather than reaching into the schema directly. Calling
`record_transition` here never validates FSM legality.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.store.db import transaction
from code_slayer.store.models import Task


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class TaskAlreadyActiveError(RuntimeError):
    """A non-terminal task already exists for this worktree (INV-2)."""


class TaskRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._audit = AuditWriter(conn)

    def create(
        self,
        *,
        description: str,
        repo_root: str,
        repo_id: str,
        worktree_id: str,
        initial_state: str = "CREATED",
        task_id: str | None = None,
        actor_type: str = "user",
        actor_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> Task:
        """Create a task row and its `TASK_CREATED` audit event atomically.

        Raises `TaskAlreadyActiveError` if the worktree already has a
        non-terminal task (enforced by the database's own partial unique
        index, not merely application discipline).
        """
        task_id = task_id or str(uuid.uuid4())
        now = utcnow_iso()
        config_json = json.dumps(config or {})
        with transaction(self._conn):
            try:
                self._conn.execute(
                    "INSERT INTO tasks (task_id, description, repo_root, repo_id, "
                    "worktree_id, created_at, updated_at, state, current_phase, "
                    "config_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id, description, repo_root, repo_id, worktree_id,
                        now, now, initial_state, None, config_json,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                message = str(exc)
                if "UNIQUE constraint failed" in message and "worktree_id" in message:
                    raise TaskAlreadyActiveError(
                        f"worktree {worktree_id!r} already has a non-terminal task"
                    ) from exc
                raise
            self._audit.append(
                task_id=task_id,
                event_type=EventType.TASK_CREATED,
                actor_type=actor_type,
                actor_id=actor_id,
                payload={
                    "description": description,
                    "repo_root": repo_root,
                    "repo_id": repo_id,
                    "worktree_id": worktree_id,
                },
                occurred_at=now,
            )
        return self.get(task_id)

    def get(self, task_id: str) -> Task:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return _row_to_task(row)

    def get_active_for_worktree(self, worktree_id: str) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE worktree_id = ? "
            "AND state NOT IN ('COMPLETED', 'FAILED')",
            (worktree_id,),
        ).fetchone()
        return _row_to_task(row) if row is not None else None

    def record_transition(
        self,
        task_id: str,
        *,
        to_state: str,
        to_phase: str | None,
        reason: str,
        actor_type: str = "system",
        actor_id: str | None = None,
    ) -> Task:
        """Persist a new (state, phase) and its `STATE_TRANSITION` audit
        event atomically. Does not validate that `to_state` is a legal
        transition from the current state — that is the future
        state-machine engine's job; this is the primitive it will call."""
        with transaction(self._conn):
            current = self.get(task_id)
            now = utcnow_iso()
            self._conn.execute(
                "UPDATE tasks SET state = ?, current_phase = ?, updated_at = ? "
                "WHERE task_id = ?",
                (to_state, to_phase, now, task_id),
            )
            self._audit.append(
                task_id=task_id,
                event_type=EventType.STATE_TRANSITION,
                actor_type=actor_type,
                actor_id=actor_id,
                payload={
                    "from_state": current.state,
                    "to_state": to_state,
                    "from_phase": current.current_phase,
                    "to_phase": to_phase,
                    "reason": reason,
                },
                occurred_at=now,
            )
        return self.get(task_id)


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        task_id=row["task_id"],
        description=row["description"],
        repo_root=row["repo_root"],
        repo_id=row["repo_id"],
        worktree_id=row["worktree_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        state=row["state"],
        current_phase=row["current_phase"],
        config_json=row["config_json"],
    )
