"""Durable application-run records (Phase 7.7, schema v5's
`runner_runs`/`runner_human_resolutions`).

A thin, transactional persistence primitive only — mirroring
`store.checkpoint_repo.CheckpointRepo`/`store.lease_repo.LeaseRepo`'s own
"repository does not decide legality" pattern. `runner.
local_worker_runner.LocalWorkerRunner` decides what state transitions are
legal and when to call these; this module only durably records them.
"""

from __future__ import annotations

import sqlite3

from code_slayer.store.models import RunnerHumanResolution, RunnerRun


class RunnerRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_in_transaction(
        self, *, run_id: str, created_at: str, repo_id: str, primary_worktree_id: str,
        original_prompt_hash: str, worker_id: str, role: str, requires_mutation: bool,
        status: str,
    ) -> RunnerRun:
        if not self._conn.in_transaction:
            raise RuntimeError("runner run creation requires an open write transaction")
        self._conn.execute(
            "INSERT INTO runner_runs "
            "(run_id, created_at, updated_at, repo_id, primary_worktree_id, "
            " original_prompt_hash, worker_id, role, requires_mutation, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, created_at, created_at, repo_id, primary_worktree_id,
                original_prompt_hash, worker_id, role, int(requires_mutation), status,
            ),
        )
        return self.get(run_id)

    def update_in_transaction(
        self, run_id: str, *, updated_at: str, status: str | None = None,
        task_id: str | None | object = ...,
        execution_worktree_id: str | None | object = ...,
        job_worktree_path: str | None | object = ...,
        analysis_content_hash: str | None | object = ...,
        final_text_content_hash: str | None | object = ...,
        tool_operation_id: str | None | object = ...,
        questions_json: str | None | object = ...,
        reason: str | None | object = ...,
    ) -> RunnerRun:
        """Update only the evolving orchestration columns of an existing
        run row. Every optional field defaults to the sentinel `...`
        (leave unchanged) so a caller can update exactly the fields it
        actually decided, never accidentally null out something it
        didn't mean to touch."""
        if not self._conn.in_transaction:
            raise RuntimeError("runner run update requires an open write transaction")
        current = self.get(run_id)
        fields = {
            "task_id": task_id,
            "execution_worktree_id": execution_worktree_id,
            "job_worktree_path": job_worktree_path,
            "analysis_content_hash": analysis_content_hash,
            "final_text_content_hash": final_text_content_hash,
            "tool_operation_id": tool_operation_id,
            "questions_json": questions_json,
            "reason": reason,
        }
        values = {
            name: (getattr(current, name) if value is ... else value)
            for name, value in fields.items()
        }
        self._conn.execute(
            "UPDATE runner_runs SET updated_at = ?, status = ?, task_id = ?, "
            "execution_worktree_id = ?, job_worktree_path = ?, analysis_content_hash = ?, "
            "final_text_content_hash = ?, tool_operation_id = ?, questions_json = ?, reason = ? "
            "WHERE run_id = ?",
            (
                updated_at, status if status is not None else current.status,
                values["task_id"], values["execution_worktree_id"], values["job_worktree_path"],
                values["analysis_content_hash"], values["final_text_content_hash"],
                values["tool_operation_id"], values["questions_json"], values["reason"], run_id,
            ),
        )
        return self.get(run_id)

    def get(self, run_id: str) -> RunnerRun:
        row = self._conn.execute(
            "SELECT * FROM runner_runs WHERE run_id = ?", (run_id,),
        ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _row_to_run(row)

    def list_for_worker(self, worker_id: str, *, limit: int = 200) -> list[RunnerRun]:
        """Every run for `worker_id`, most recent first. Read-only; used
        by `workers.lifecycle`'s active-work check (H.3) and available
        generally for a worker's own run history."""
        rows = self._conn.execute(
            "SELECT * FROM runner_runs WHERE worker_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (worker_id, limit),
        ).fetchall()
        return [_row_to_run(row) for row in rows]

    def get_or_none(self, run_id: str) -> RunnerRun | None:
        try:
            return self.get(run_id)
        except KeyError:
            return None

    def record_human_resolution_in_transaction(
        self, *, run_id: str, ambiguity_id: str, source: str, resolution_kind: str,
        answer_content_hash: str, created_at: str,
    ) -> RunnerHumanResolution:
        if not self._conn.in_transaction:
            raise RuntimeError("human resolution recording requires an open write transaction")
        cursor = self._conn.execute(
            "INSERT INTO runner_human_resolutions "
            "(run_id, ambiguity_id, source, resolution_kind, answer_content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, ambiguity_id, source, resolution_kind, answer_content_hash, created_at),
        )
        assert cursor.lastrowid is not None
        row = self._conn.execute(
            "SELECT * FROM runner_human_resolutions WHERE id = ?", (cursor.lastrowid,),
        ).fetchone()
        return _row_to_resolution(row)

    def latest_human_resolutions(self, run_id: str) -> list[RunnerHumanResolution]:
        """The most recent resolution row for each distinct
        `ambiguity_id` under `run_id` — a human revising an earlier
        answer adds a new row rather than mutating the old one; this is
        what "current" means for that append-only history."""
        rows = self._conn.execute(
            "SELECT r1.* FROM runner_human_resolutions r1 "
            "WHERE r1.run_id = ? AND r1.id = ("
            "  SELECT MAX(r2.id) FROM runner_human_resolutions r2 "
            "  WHERE r2.run_id = r1.run_id AND r2.ambiguity_id = r1.ambiguity_id"
            ") ORDER BY r1.ambiguity_id",
            (run_id,),
        ).fetchall()
        return [_row_to_resolution(row) for row in rows]


def _row_to_run(row: sqlite3.Row) -> RunnerRun:
    return RunnerRun(
        run_id=row["run_id"], created_at=row["created_at"], updated_at=row["updated_at"],
        repo_id=row["repo_id"], primary_worktree_id=row["primary_worktree_id"],
        original_prompt_hash=row["original_prompt_hash"], worker_id=row["worker_id"],
        role=row["role"], requires_mutation=bool(row["requires_mutation"]), status=row["status"],
        task_id=row["task_id"], execution_worktree_id=row["execution_worktree_id"],
        job_worktree_path=row["job_worktree_path"],
        analysis_content_hash=row["analysis_content_hash"],
        final_text_content_hash=row["final_text_content_hash"],
        tool_operation_id=row["tool_operation_id"], questions_json=row["questions_json"],
        reason=row["reason"],
    )


def _row_to_resolution(row: sqlite3.Row) -> RunnerHumanResolution:
    return RunnerHumanResolution(
        id=row["id"], run_id=row["run_id"], ambiguity_id=row["ambiguity_id"],
        source=row["source"], resolution_kind=row["resolution_kind"],
        answer_content_hash=row["answer_content_hash"], created_at=row["created_at"],
    )
