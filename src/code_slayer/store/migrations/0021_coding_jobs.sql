-- Code Slayer schema v21 (Autonomous Engineering Loop V1 -- durable
-- Coder/Reviewer/Repairer/Security job records).
--
-- Purely additive: no table rebuild, no rewrite of existing rows;
-- migrations 0001-0020 are byte-for-byte unchanged.
--
-- One row per coding job -- mirrors `planning_jobs` (0009/0019/0020)'s own
-- "identity locked at creation, lifecycle/outcome fields evolve" shape.
-- Rich per-attempt evidence (review findings, security findings, tool
-- calls, repair reasoning) is NOT duplicated into new columns here -- it
-- already has a durable, append-only, task-scoped home in `audit_events`
-- (`FINALIZATION_DECIDED`, `REVIEW_STARTED`/`REVIEW_FINDING`,
-- `REPAIR_STARTED`/`REPAIR_FINISHED`, and this migration's own two new
-- event types below), addressable forever via `coding_jobs.task_id`. This
-- table is deliberately a small identity/pointer row, never a second
-- ledger competing with the audit log for the same facts.
--
-- `state` is `code_slayer.coding.pipeline_types.CodingJobState`'s own
-- string vocabulary -- code-owned, never a client-supplied value; this
-- migration adds no CHECK constraint enumerating it (the same posture
-- `planning_jobs.state`/`tasks.state` already take: the Python layer is
-- the single source of truth for legal values, and a stricter DB-level
-- enum would only duplicate that list and risk drifting from it).
--
-- Identity fields (job_id, plan_id, repo_id, primary_worktree_id,
-- created_at, original_prompt_hash, base_revision, max_repair_attempts)
-- are locked at creation by the trigger below, exactly like
-- `planning_jobs_no_mutate_identity`. Everything else
-- (updated_at/state/execution_worktree_id/job_worktree_path/task_id/
-- repair_attempts/review_verdict/review_evidence_ref/security_verdict/
-- security_evidence_ref/final_reason/finished_at) evolves as the job
-- progresses.

BEGIN;

CREATE TABLE coding_jobs (
  job_id TEXT PRIMARY KEY,
  plan_id TEXT NOT NULL REFERENCES engineering_plans(plan_id),
  repo_id TEXT NOT NULL,
  primary_worktree_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  original_prompt_hash TEXT NOT NULL,
  base_revision TEXT NOT NULL,
  max_repair_attempts INTEGER NOT NULL,
  state TEXT NOT NULL,
  execution_worktree_id TEXT,
  job_worktree_path TEXT,
  task_id TEXT,
  repair_attempts INTEGER NOT NULL DEFAULT 0,
  review_verdict TEXT,
  review_evidence_ref TEXT,
  security_verdict TEXT,
  security_evidence_ref TEXT,
  final_reason TEXT,
  finished_at TEXT
);

CREATE INDEX ix_coding_jobs_plan_id ON coding_jobs(plan_id);
CREATE INDEX ix_coding_jobs_task_id ON coding_jobs(task_id);

CREATE TRIGGER coding_jobs_no_mutate_identity
  BEFORE UPDATE ON coding_jobs
  WHEN NEW.job_id != OLD.job_id
    OR NEW.plan_id != OLD.plan_id
    OR NEW.repo_id != OLD.repo_id
    OR NEW.primary_worktree_id != OLD.primary_worktree_id
    OR NEW.created_at != OLD.created_at
    OR NEW.original_prompt_hash != OLD.original_prompt_hash
    OR NEW.base_revision != OLD.base_revision
    OR NEW.max_repair_attempts != OLD.max_repair_attempts
  BEGIN SELECT RAISE(ABORT,
    'coding_jobs: job identity fields cannot be changed after creation'); END;
