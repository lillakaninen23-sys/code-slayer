"""Durable background-planning-job records (Phase 8.2d, schema v9's
`planning_jobs`).

A thin, transactional persistence primitive only — mirrors `store.
planning_repo.PlanningRepo`/`store.runner_repo.RunnerRepo`'s own
"repository does not decide legality" pattern. `planning.service.
EngineeringPlanningService` decides whether a claim/reclaim/finish is
legal (liveness evidence, fencing generation match); this module only
durably records the outcome of that decision.
"""

from __future__ import annotations

import sqlite3

from code_slayer.store.models import PlanningJobRow


class PlanningJobsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_in_transaction(
        self, *, job_id: str, plan_id: str, repo_id: str, worktree_id: str, created_at: str,
        kind: str,
        worker_id: str, runtime_identity_fingerprint: str, role_evaluation_fingerprint: str,
        security_certificate_id: str, role_certificate_id: str, output_token_budget: int,
        tool_choice_enforcement: str, planner_timeout_seconds: float,
        planner_policy_version: str,
        predecessor_job_id: str | None = None,
    ) -> PlanningJobRow:
        """H.4 (schema v19) + H.4.1 (schema v20): every new job requires
        a complete Planner route binding -- `worker_id` through
        `planner_timeout_seconds` are all mandatory (never `None`/
        omitted), mirroring `store.migrations.0019_planner_worker_
        routing`/`0020_planner_timeout_binding`'s own `planning_jobs_
        require_route_binding_on_insert` trigger, which refuses at the
        schema level if this method (or any other insertion path) ever
        tried to land an incomplete row. This method itself decides no
        eligibility/legality -- the caller (`planning.service.
        EngineeringPlanningService.create_job()`/`.replan_job()`) has
        already selected and, inside this SAME open transaction,
        atomically reverified the binding's worker lifecycle before
        calling here."""
        if not self._conn.in_transaction:
            raise RuntimeError("planning job creation requires an open write transaction")
        self._conn.execute(
            "INSERT INTO planning_jobs "
            "(job_id, plan_id, repo_id, worktree_id, created_at, updated_at, kind, state, "
            " attempt, owner_generation, predecessor_job_id, worker_id, "
            " runtime_identity_fingerprint, role_evaluation_fingerprint, "
            " security_certificate_id, role_certificate_id, output_token_budget, "
            " tool_choice_enforcement, planner_policy_version, planner_timeout_seconds) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'QUEUED', 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, plan_id, repo_id, worktree_id, created_at, created_at, kind,
             predecessor_job_id, worker_id, runtime_identity_fingerprint,
             role_evaluation_fingerprint, security_certificate_id, role_certificate_id,
             output_token_budget, tool_choice_enforcement, planner_policy_version,
             planner_timeout_seconds),
        )
        return self.get(job_id)

    def claim_in_transaction(
        self, job_id: str, *, owner_pid: int, owner_pid_started_at: str | None, now: str,
    ) -> PlanningJobRow:
        """Unconditionally transitions `job_id` to `RUNNING` under a
        fresh generation/attempt — the caller (`EngineeringPlanningService.
        claim_job()`) must have already decided this is legal (row is
        `QUEUED`, or `RUNNING` with proven-dead prior ownership) inside
        this same open transaction; this method performs no legality
        check of its own, matching every other repo in this package."""
        if not self._conn.in_transaction:
            raise RuntimeError("planning job claim requires an open write transaction")
        current = self.get(job_id)
        self._conn.execute(
            "UPDATE planning_jobs SET updated_at = ?, state = 'RUNNING', "
            "attempt = ?, owner_pid = ?, owner_pid_started_at = ?, owner_generation = ?, "
            "started_at = ?, finished_at = NULL, failure_category = NULL, failure_reason = NULL "
            "WHERE job_id = ?",
            (now, current.attempt + 1, owner_pid, owner_pid_started_at,
             current.owner_generation + 1, now, job_id),
        )
        return self.get(job_id)

    def finish_in_transaction(
        self, job_id: str, *, state: str, expected_generation: int, now: str,
        failure_category: str | None = None, failure_reason: str | None = None,
    ) -> PlanningJobRow | None:
        """Terminalizes `job_id` (`SUCCEEDED`/`FAILED`) only if it is
        still owned under `expected_generation` — the fencing check that
        stops a stale claimant (one whose ownership was already taken
        over) from overwriting a newer owner's outcome. Returns `None`,
        writing nothing, if the generation no longer matches."""
        if not self._conn.in_transaction:
            raise RuntimeError("planning job finish requires an open write transaction")
        current = self.get_or_none(job_id)
        if current is None or current.owner_generation != expected_generation:
            return None
        if current.state != "RUNNING":
            return None
        self._conn.execute(
            "UPDATE planning_jobs SET updated_at = ?, state = ?, finished_at = ?, "
            "failure_category = ?, failure_reason = ? WHERE job_id = ?",
            (now, state, now, failure_category, failure_reason, job_id),
        )
        return self.get(job_id)

    def get(self, job_id: str) -> PlanningJobRow:
        row = self._conn.execute(
            "SELECT * FROM planning_jobs WHERE job_id = ?", (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _row_to_job(row)

    def get_or_none(self, job_id: str) -> PlanningJobRow | None:
        try:
            return self.get(job_id)
        except KeyError:
            return None

    def list_for_plan(self, plan_id: str) -> list[PlanningJobRow]:
        rows = self._conn.execute(
            "SELECT * FROM planning_jobs WHERE plan_id = ? ORDER BY created_at, rowid",
            (plan_id,),
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def list_for_scope(
        self, repo_id: str, worktree_id: str, *, limit: int, offset: int,
    ) -> list[PlanningJobRow]:
        rows = self._conn.execute(
            "SELECT * FROM planning_jobs WHERE repo_id = ? AND worktree_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (repo_id, worktree_id, limit, offset),
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def list_non_terminal(self, repo_id: str, worktree_id: str) -> list[PlanningJobRow]:
        """Every `QUEUED`/`RUNNING` job for this scope — the discovery
        set a dispatcher (`planning.executor.PlanningJobExecutor`)
        evaluates on every pass, including at process startup."""
        rows = self._conn.execute(
            "SELECT * FROM planning_jobs WHERE repo_id = ? AND worktree_id = ? "
            "AND state IN ('QUEUED', 'RUNNING') ORDER BY created_at, rowid",
            (repo_id, worktree_id),
        ).fetchall()
        return [_row_to_job(row) for row in rows]

    def has_status_for_worker_in_transaction(
        self, worker_id: str, *, statuses: frozenset[str] | tuple[str, ...],
    ) -> bool:
        """H.4: `True` if any `planning_jobs` row bound to `worker_id`
        currently has a state in `statuses` -- a dedicated, uncapped
        existence query, mirroring `store.runner_repo.RunnerRepo.
        has_status_for_worker_in_transaction()` (H.3 review finding)
        exactly, for the same reason: an active-work authority check
        must never be built on a paginated/capped history view. `SQL
        NULL` never equals `worker_id` (a real, non-NULL string), so a
        legacy pre-H.4 row with `worker_id IS NULL` can never match
        here regardless of its own `state` -- it blocks archiving no
        specific worker, exactly the documented H.4 policy. Requires an
        open write transaction -- this method's only real caller
        (`workers.lifecycle.archive_worker()`) always calls it from
        inside an already-open `BEGIN IMMEDIATE` transaction, where it
        must see the same consistent snapshot as the lifecycle write
        that follows."""
        if not self._conn.in_transaction:
            raise RuntimeError(
                "has_status_for_worker_in_transaction requires an open write transaction"
            )
        statuses = tuple(statuses)
        placeholders = ",".join("?" for _ in statuses)
        row = self._conn.execute(
            f"SELECT 1 FROM planning_jobs WHERE worker_id = ? "
            f"AND state IN ({placeholders}) LIMIT 1",
            (worker_id, *statuses),
        ).fetchone()
        return row is not None


def _row_to_job(row: sqlite3.Row) -> PlanningJobRow:
    keys = row.keys()
    return PlanningJobRow(
        job_id=row["job_id"], plan_id=row["plan_id"], repo_id=row["repo_id"],
        worktree_id=row["worktree_id"], created_at=row["created_at"],
        updated_at=row["updated_at"], kind=row["kind"], state=row["state"],
        attempt=row["attempt"], owner_pid=row["owner_pid"],
        owner_pid_started_at=row["owner_pid_started_at"],
        owner_generation=row["owner_generation"], started_at=row["started_at"],
        finished_at=row["finished_at"], failure_category=row["failure_category"],
        failure_reason=row["failure_reason"], predecessor_job_id=row["predecessor_job_id"],
        # H.4 (schema v19) route-binding columns, read defensively: a
        # migration-in-progress test may construct this repo against a
        # database frozen at a pre-v19 schema version (mirrors `store.
        # workers_repo._row_to_worker`'s own precedent for H.3's
        # lifecycle columns).
        worker_id=row["worker_id"] if "worker_id" in keys else None,
        runtime_identity_fingerprint=(
            row["runtime_identity_fingerprint"] if "runtime_identity_fingerprint" in keys else None
        ),
        role_evaluation_fingerprint=(
            row["role_evaluation_fingerprint"] if "role_evaluation_fingerprint" in keys else None
        ),
        security_certificate_id=(
            row["security_certificate_id"] if "security_certificate_id" in keys else None
        ),
        role_certificate_id=row["role_certificate_id"] if "role_certificate_id" in keys else None,
        output_token_budget=(
            row["output_token_budget"] if "output_token_budget" in keys else None
        ),
        tool_choice_enforcement=(
            row["tool_choice_enforcement"] if "tool_choice_enforcement" in keys else None
        ),
        planner_policy_version=(
            row["planner_policy_version"] if "planner_policy_version" in keys else None
        ),
        # H.4.1 (schema v20): also `None` for a v19-or-earlier row (the
        # column did not exist yet), and for any legitimate v19 job row
        # (the column exists but that row predates it) -- never invented.
        planner_timeout_seconds=(
            row["planner_timeout_seconds"] if "planner_timeout_seconds" in keys else None
        ),
    )
