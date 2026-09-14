"""Durable checkpoint records (Phase 5, schema v1's pre-existing `checkpoints`
table — Foundation Plan §08, schema-only in Phase 1).

A row here is written **only** once a checkpoint's underlying Git mutation
is already known-`SUCCEEDED` (evidenced by its `tool_operations` row): this
table represents confirmed, durable checkpoints, never an intent or an
in-flight attempt. That distinction is exactly what `tool_operations`
(`STARTED`/`SUCCEEDED`/`FAILED`/`UNKNOWN`) already exists to carry, so this
repository does not re-invent it. `status` here is always `COMPLETE`;
`WIP` remains valid per schema but unused by Phase 5.

`completed_json`, `pending_json`, `worker_id`, and `next_action` are
agent-loop/worker bookkeeping fields with no Phase 5 producer — left at
their schema defaults (`'[]'`/`NULL`), not invented here. `verified_json`
carries the structured, machine-verifiable Git identity this phase does
produce: commit/tree/parent shas, the baseline HEAD this checkpoint was
built from, the exact owned-path/content-hash manifest included, and the
checkpoint's canonical request hash.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from code_slayer.store.models import Checkpoint


class CheckpointError(RuntimeError):
    """A complete, correctly bound checkpoint cannot be recorded or read."""


@dataclass(frozen=True)
class CheckpointVerified:
    """The structured contents of `checkpoints.verified_json`."""

    commit_sha: str
    tree_sha: str
    parent_commit_sha: str | None
    base_head_sha: str | None
    request_hash: str
    owned_paths: tuple[tuple[str, str], ...]  # (path, content_hash), sorted


class CheckpointRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def next_seq(self, task_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(seq) AS m FROM checkpoints WHERE task_id = ? AND status = 'COMPLETE'",
            (task_id,),
        ).fetchone()
        return 0 if row["m"] is None else row["m"] + 1

    def latest(self, task_id: str) -> Checkpoint | None:
        row = self._conn.execute(
            "SELECT * FROM checkpoints WHERE task_id = ? AND status = 'COMPLETE' "
            "ORDER BY seq DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return _row_to_checkpoint(row) if row is not None else None

    def get(self, checkpoint_id: str) -> Checkpoint:
        row = self._conn.execute(
            "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,),
        ).fetchone()
        if row is None:
            raise KeyError(checkpoint_id)
        return _row_to_checkpoint(row)

    def list_for_task(self, task_id: str) -> list[Checkpoint]:
        rows = self._conn.execute(
            "SELECT * FROM checkpoints WHERE task_id = ? ORDER BY seq", (task_id,),
        ).fetchall()
        return [_row_to_checkpoint(row) for row in rows]

    def record_in_transaction(
        self,
        *,
        checkpoint_id: str,
        task_id: str,
        seq: int,
        parent_checkpoint: str | None,
        created_at: str,
        phase: str,
        git_ref: str,
        git_branch: str | None,
        verified: CheckpointVerified,
        changed_paths: tuple[str, ...],
    ) -> Checkpoint:
        """Persist one durable, COMPLETE checkpoint row.

        Internal persistence primitive: the caller owns the transaction
        (matching `BaselineRepo.record_in_transaction` and
        `ToolOperationsRepo.*_in_transaction`), so this composes atomically
        with the `tool_operations` finish and the state transition.
        """
        if not self._conn.in_transaction:
            raise RuntimeError("checkpoint persistence requires an open write transaction")
        if self._conn.execute(
            "SELECT 1 FROM checkpoints WHERE task_id = ? AND seq = ?", (task_id, seq),
        ).fetchone() is not None:
            raise CheckpointError(f"checkpoint seq {seq} already recorded for task {task_id}")
        verified_json = json.dumps({
            "commit_sha": verified.commit_sha,
            "tree_sha": verified.tree_sha,
            "parent_commit_sha": verified.parent_commit_sha,
            "base_head_sha": verified.base_head_sha,
            "request_hash": verified.request_hash,
            "owned_paths": [list(pair) for pair in verified.owned_paths],
        }, sort_keys=True)
        self._conn.execute(
            "INSERT INTO checkpoints "
            "(checkpoint_id, task_id, seq, parent_checkpoint, created_at, phase, status, "
            " safe_to_resume, git_ref, git_branch, worker_id, verified_json, "
            " changed_files_json) "
            "VALUES (?, ?, ?, ?, ?, ?, 'COMPLETE', 1, ?, ?, NULL, ?, ?)",
            (
                checkpoint_id, task_id, seq, parent_checkpoint, created_at, phase,
                git_ref, git_branch, verified_json, json.dumps(sorted(changed_paths)),
            ),
        )
        return self.get(checkpoint_id)


def _row_to_checkpoint(row: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        checkpoint_id=row["checkpoint_id"],
        task_id=row["task_id"],
        seq=row["seq"],
        parent_checkpoint=row["parent_checkpoint"],
        created_at=row["created_at"],
        phase=row["phase"],
        status=row["status"],
        safe_to_resume=bool(row["safe_to_resume"]),
        git_ref=row["git_ref"],
        git_branch=row["git_branch"],
        worker_id=row["worker_id"],
        completed_json=row["completed_json"],
        pending_json=row["pending_json"],
        verified_json=row["verified_json"],
        changed_files_json=row["changed_files_json"],
        next_action=row["next_action"],
    )


def parse_verified(checkpoint: Checkpoint) -> CheckpointVerified:
    data = json.loads(checkpoint.verified_json)
    return CheckpointVerified(
        commit_sha=data["commit_sha"],
        tree_sha=data["tree_sha"],
        parent_commit_sha=data["parent_commit_sha"],
        base_head_sha=data["base_head_sha"],
        request_hash=data["request_hash"],
        owned_paths=tuple(tuple(pair) for pair in data["owned_paths"]),
    )
