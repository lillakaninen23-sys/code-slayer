"""Durable engineering-plan records (Phase 8.2, schema v8's
`engineering_plans`/`engineering_plan_human_resolutions`).

A thin, transactional persistence primitive only — mirroring
`store.runner_repo.RunnerRepo`'s own "repository does not decide
legality" pattern. `planning.service.EngineeringPlanningService` decides
what state transitions are legal and when to call these; this module
only durably records them.
"""

from __future__ import annotations

import sqlite3

from code_slayer.store.models import EngineeringPlanHumanResolution, EngineeringPlanRow


class PlanningRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_in_transaction(
        self, *, plan_id: str, created_at: str, schema_version: str, repo_id: str,
        worktree_id: str, run_id: str | None, request_content_hash: str,
        predecessor_plan_id: str | None, revision: int, state: str,
    ) -> EngineeringPlanRow:
        if not self._conn.in_transaction:
            raise RuntimeError("engineering plan creation requires an open write transaction")
        self._conn.execute(
            "INSERT INTO engineering_plans "
            "(plan_id, created_at, updated_at, schema_version, repo_id, worktree_id, run_id, "
            " request_content_hash, predecessor_plan_id, revision, state) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                plan_id, created_at, created_at, schema_version, repo_id, worktree_id, run_id,
                request_content_hash, predecessor_plan_id, revision, state,
            ),
        )
        return self.get(plan_id)

    def update_in_transaction(
        self, plan_id: str, *, updated_at: str, state: str | None = None,
        reason: str | None | object = ...,
        head_sha: str | None | object = ...,
        working_tree_dirty: bool | None = None,
        working_tree_fingerprint: str | None | object = ...,
        intelligence_snapshot_id: str | None | object = ...,
        planner_input_content_hash: str | None | object = ...,
        planner_output_content_hash: str | None | object = ...,
        validation_content_hash: str | None | object = ...,
        plan_content_hash: str | None | object = ...,
        questions_json: str | None | object = ...,
    ) -> EngineeringPlanRow:
        """Update only the evolving orchestration columns of an existing
        plan row. Every optional field defaults to the sentinel `...`
        (leave unchanged) so a caller updates exactly the fields it
        actually decided, never accidentally nulling out something it
        didn't mean to touch — mirrors `RunnerRepo.update_in_transaction`."""
        if not self._conn.in_transaction:
            raise RuntimeError("engineering plan update requires an open write transaction")
        current = self.get(plan_id)
        fields = {
            "reason": reason,
            "head_sha": head_sha,
            "working_tree_fingerprint": working_tree_fingerprint,
            "intelligence_snapshot_id": intelligence_snapshot_id,
            "planner_input_content_hash": planner_input_content_hash,
            "planner_output_content_hash": planner_output_content_hash,
            "validation_content_hash": validation_content_hash,
            "plan_content_hash": plan_content_hash,
            "questions_json": questions_json,
        }
        values = {
            name: (getattr(current, name) if value is ... else value)
            for name, value in fields.items()
        }
        self._conn.execute(
            "UPDATE engineering_plans SET updated_at = ?, state = ?, reason = ?, head_sha = ?, "
            "working_tree_dirty = ?, working_tree_fingerprint = ?, intelligence_snapshot_id = ?, "
            "planner_input_content_hash = ?, planner_output_content_hash = ?, "
            "validation_content_hash = ?, plan_content_hash = ?, questions_json = ? "
            "WHERE plan_id = ?",
            (
                updated_at, state if state is not None else current.state,
                values["reason"], values["head_sha"],
                int(working_tree_dirty) if working_tree_dirty is not None
                else int(current.working_tree_dirty),
                values["working_tree_fingerprint"], values["intelligence_snapshot_id"],
                values["planner_input_content_hash"], values["planner_output_content_hash"],
                values["validation_content_hash"], values["plan_content_hash"],
                values["questions_json"], plan_id,
            ),
        )
        return self.get(plan_id)

    def get(self, plan_id: str) -> EngineeringPlanRow:
        row = self._conn.execute(
            "SELECT * FROM engineering_plans WHERE plan_id = ?", (plan_id,),
        ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        return _row_to_plan(row)

    def get_or_none(self, plan_id: str) -> EngineeringPlanRow | None:
        try:
            return self.get(plan_id)
        except KeyError:
            return None

    def list_for_scope(
        self, repo_id: str, worktree_id: str, *, limit: int, offset: int,
    ) -> list[EngineeringPlanRow]:
        rows = self._conn.execute(
            "SELECT * FROM engineering_plans WHERE repo_id = ? AND worktree_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (repo_id, worktree_id, limit, offset),
        ).fetchall()
        return [_row_to_plan(row) for row in rows]

    def record_human_resolution_in_transaction(
        self, *, plan_id: str, ambiguity_id: str, source: str, resolution_kind: str,
        answer_content_hash: str, created_at: str,
    ) -> EngineeringPlanHumanResolution:
        if not self._conn.in_transaction:
            raise RuntimeError("human resolution recording requires an open write transaction")
        cursor = self._conn.execute(
            "INSERT INTO engineering_plan_human_resolutions "
            "(plan_id, ambiguity_id, source, resolution_kind, answer_content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (plan_id, ambiguity_id, source, resolution_kind, answer_content_hash, created_at),
        )
        assert cursor.lastrowid is not None
        row = self._conn.execute(
            "SELECT * FROM engineering_plan_human_resolutions WHERE id = ?", (cursor.lastrowid,),
        ).fetchone()
        return _row_to_resolution(row)

    def latest_human_resolutions(self, plan_id: str) -> list[EngineeringPlanHumanResolution]:
        """The most recent resolution row for each distinct
        `ambiguity_id` under `plan_id` — mirrors `RunnerRepo.
        latest_human_resolutions()`: a human revising an earlier answer
        adds a new row rather than mutating the old one."""
        rows = self._conn.execute(
            "SELECT r1.* FROM engineering_plan_human_resolutions r1 "
            "WHERE r1.plan_id = ? AND r1.id = ("
            "  SELECT MAX(r2.id) FROM engineering_plan_human_resolutions r2 "
            "  WHERE r2.plan_id = r1.plan_id AND r2.ambiguity_id = r1.ambiguity_id"
            ") ORDER BY r1.ambiguity_id",
            (plan_id,),
        ).fetchall()
        return [_row_to_resolution(row) for row in rows]


def _row_to_plan(row: sqlite3.Row) -> EngineeringPlanRow:
    return EngineeringPlanRow(
        plan_id=row["plan_id"], created_at=row["created_at"], updated_at=row["updated_at"],
        schema_version=row["schema_version"], repo_id=row["repo_id"],
        worktree_id=row["worktree_id"], run_id=row["run_id"],
        request_content_hash=row["request_content_hash"],
        predecessor_plan_id=row["predecessor_plan_id"], revision=row["revision"],
        state=row["state"], reason=row["reason"], head_sha=row["head_sha"],
        working_tree_dirty=bool(row["working_tree_dirty"]),
        working_tree_fingerprint=row["working_tree_fingerprint"],
        intelligence_snapshot_id=row["intelligence_snapshot_id"],
        planner_input_content_hash=row["planner_input_content_hash"],
        planner_output_content_hash=row["planner_output_content_hash"],
        validation_content_hash=row["validation_content_hash"],
        plan_content_hash=row["plan_content_hash"], questions_json=row["questions_json"],
    )


def _row_to_resolution(row: sqlite3.Row) -> EngineeringPlanHumanResolution:
    return EngineeringPlanHumanResolution(
        id=row["id"], plan_id=row["plan_id"], ambiguity_id=row["ambiguity_id"],
        source=row["source"], resolution_kind=row["resolution_kind"],
        answer_content_hash=row["answer_content_hash"], created_at=row["created_at"],
    )
