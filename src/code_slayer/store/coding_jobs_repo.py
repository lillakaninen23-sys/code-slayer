"""Durable coding-job records (`store.migrations.0021_coding_jobs`).

A thin, transactional persistence primitive only -- mirrors `store.
planning_jobs_repo.PlanningJobsRepo`'s own "repository does not decide
legality" pattern. `code_slayer.coding.pipeline.run_coding_job()` decides
what state transition is legal; this module only durably records it.
"""

from __future__ import annotations

import sqlite3

from code_slayer.store.models import CodingJobRow


class CodingJobsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_in_transaction(
        self, *, job_id: str, plan_id: str, repo_id: str, primary_worktree_id: str,
        created_at: str, original_prompt_hash: str, base_revision: str,
        max_repair_attempts: int, state: str,
    ) -> CodingJobRow:
        if not self._conn.in_transaction:
            raise RuntimeError("coding job creation requires an open write transaction")
        self._conn.execute(
            "INSERT INTO coding_jobs "
            "(job_id, plan_id, repo_id, primary_worktree_id, created_at, updated_at, "
            " original_prompt_hash, base_revision, max_repair_attempts, state, "
            " repair_attempts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (job_id, plan_id, repo_id, primary_worktree_id, created_at, created_at,
             original_prompt_hash, base_revision, max_repair_attempts, state),
        )
        return self.get(job_id)

    def update_in_transaction(self, job_id: str, *, updated_at: str, **fields) -> CodingJobRow:
        """Update any subset of the evolving fields (never an identity
        field -- `coding_jobs_no_mutate_identity` refuses that at the
        schema level regardless of what this method is asked to write)."""
        if not self._conn.in_transaction:
            raise RuntimeError("coding job update requires an open write transaction")
        allowed = {
            "state", "execution_worktree_id", "job_worktree_path", "task_id",
            "repair_attempts", "review_verdict", "review_evidence_ref",
            "security_verdict", "security_evidence_ref", "final_reason", "finished_at",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update non-evolving/unknown coding_jobs field(s): {unknown}")
        columns = ["updated_at", *fields.keys()]
        values = [updated_at, *fields.values()]
        assignments = ", ".join(f"{col} = ?" for col in columns)
        self._conn.execute(
            f"UPDATE coding_jobs SET {assignments} WHERE job_id = ?", (*values, job_id),
        )
        return self.get(job_id)

    def get(self, job_id: str) -> CodingJobRow:
        row = self._conn.execute(
            "SELECT * FROM coding_jobs WHERE job_id = ?", (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _row_to_job(row)

    def get_or_none(self, job_id: str) -> CodingJobRow | None:
        try:
            return self.get(job_id)
        except KeyError:
            return None

    def list_for_plan(self, plan_id: str) -> list[CodingJobRow]:
        rows = self._conn.execute(
            "SELECT * FROM coding_jobs WHERE plan_id = ? ORDER BY created_at, rowid",
            (plan_id,),
        ).fetchall()
        return [_row_to_job(row) for row in rows]


def _row_to_job(row: sqlite3.Row) -> CodingJobRow:
    return CodingJobRow(
        job_id=row["job_id"], plan_id=row["plan_id"], repo_id=row["repo_id"],
        primary_worktree_id=row["primary_worktree_id"], created_at=row["created_at"],
        updated_at=row["updated_at"], original_prompt_hash=row["original_prompt_hash"],
        base_revision=row["base_revision"], max_repair_attempts=row["max_repair_attempts"],
        state=row["state"], execution_worktree_id=row["execution_worktree_id"],
        job_worktree_path=row["job_worktree_path"], task_id=row["task_id"],
        repair_attempts=row["repair_attempts"], review_verdict=row["review_verdict"],
        review_evidence_ref=row["review_evidence_ref"], security_verdict=row["security_verdict"],
        security_evidence_ref=row["security_evidence_ref"], final_reason=row["final_reason"],
        finished_at=row["finished_at"],
    )
