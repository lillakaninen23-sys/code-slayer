-- Code Slayer schema v9 (Phase 8.2d — durable, server-owned background
-- planning jobs).
--
-- Purely additive: no ALTER of any table; migrations 0001-0008 are
-- byte-for-byte unchanged.
--
-- A planning JOB is a distinct concern from an engineering PLAN
-- (`engineering_plans`, migration 0008): a plan's `state` (DRAFT/
-- NEEDS_INPUT/READY/SUPERSEDED, plus the computed-only STALE) describes
-- the *content*'s own validity; a job's `state` (QUEUED/RUNNING/
-- SUCCEEDED/FAILED) describes whether one *execution attempt* — a real
-- planner turn, run on a background thread the HTTP request never waits
-- on — has completed. A job that finishes a genuine planner turn
-- producing a legitimate NEEDS_INPUT (or even a DRAFT rejected by
-- evidence validation) plan is SUCCEEDED; only a transport/protocol
-- failure that never produced valid structured output is FAILED. Never
-- overloading `engineering_plans.state` with this distinction is
-- deliberate — see `docs/ENGINEERING_PLANNING.md`.
--
-- `plan_id` is always bound to a real `engineering_plans` row (created
-- durably, synchronously, in the SAME HTTP request that accepts the
-- job — before the row below is even inserted), so a client's 202
-- response already names a real, inspectable (if still DRAFT/empty)
-- plan. No large content (planner input/output, evidence validation
-- result, plan content) is ever duplicated here — those remain
-- content-addressed via `planning.provenance`/`content_blobs`,
-- referenced only through `engineering_plans`, exactly as already
-- designed; this table stores only small identity/ownership/outcome
-- fields.
--
-- Ownership/claim fields (`owner_pid`, `owner_pid_started_at`,
-- `owner_generation`) mirror `worker_leases`' own fencing-token pattern
-- (`lease.manager`, migration 0001) at a much smaller scope: a job has
-- no worktree, no external TTL/quiescence protocol, and no renew — its
-- RUNNING duration is bounded to one in-process planner call. A crash
-- leaves a truthful RUNNING row naming the exact dead process; a later
-- claimant (a fresh process, or the same one restarted) uses
-- `lease.liveness.check_process_liveness()` (already generic, not
-- lease-specific) against `owner_pid`/`owner_pid_started_at` before ever
-- reclaiming a RUNNING row — GONE only, never UNKNOWN, exactly ALIVE's
-- fail-closed posture. `owner_generation` increments on every fresh
-- claim (fresh QUEUED->RUNNING, or a proven-dead-owner reclaim) and is
-- the fencing token a claimant's own finalize step re-checks before
-- writing a terminal state, so a stale claimant can never overwrite a
-- newer owner's outcome.

BEGIN;

CREATE TABLE planning_jobs (
  job_id                  TEXT PRIMARY KEY,
  plan_id                 TEXT NOT NULL REFERENCES engineering_plans(plan_id),
  repo_id                 TEXT NOT NULL,
  worktree_id             TEXT NOT NULL,
  created_at              TEXT NOT NULL,
  updated_at              TEXT NOT NULL,
  kind                    TEXT NOT NULL,    -- 'create' | 'replan' -- which service operation this attempts
  state                   TEXT NOT NULL,    -- QUEUED | RUNNING | SUCCEEDED | FAILED
  attempt                 INTEGER NOT NULL DEFAULT 0,
  owner_pid               INTEGER,
  owner_pid_started_at    TEXT,
  owner_generation        INTEGER NOT NULL DEFAULT 0,
  started_at              TEXT,
  finished_at             TEXT,
  failure_category        TEXT,             -- planning.planner.PlannerFailureCategory value, lowercased
  failure_reason          TEXT,             -- the same short machine reason engineering_plans.reason carries
  predecessor_job_id      TEXT REFERENCES planning_jobs(job_id)
);

CREATE INDEX ix_planning_jobs_state ON planning_jobs(state);
CREATE INDEX ix_planning_jobs_plan_id ON planning_jobs(plan_id);
CREATE INDEX ix_planning_jobs_scope ON planning_jobs(repo_id, worktree_id, created_at);

-- A job's identity and originating request binding must never be
-- rewritten after creation -- only its evolving execution-lifecycle
-- fields (state, attempt, owner_*, started_at/finished_at,
-- failure_category/failure_reason, updated_at) may change as it
-- progresses. Mirrors `engineering_plans_no_mutate_identity`
-- (0008_engineering_planning.sql) and `runner_runs_no_mutate_identity`
-- (0005_runner_state.sql).
CREATE TRIGGER planning_jobs_no_mutate_identity
  BEFORE UPDATE ON planning_jobs
  WHEN NEW.job_id != OLD.job_id
    OR NEW.plan_id != OLD.plan_id
    OR NEW.created_at != OLD.created_at
    OR NEW.repo_id != OLD.repo_id
    OR NEW.worktree_id != OLD.worktree_id
    OR NEW.kind != OLD.kind
    OR (NEW.predecessor_job_id IS NOT OLD.predecessor_job_id)
  BEGIN SELECT RAISE(ABORT,
    'planning_jobs: job identity fields cannot be changed after creation'); END;

-- A job that has already reached a terminal state is never silently
-- reopened -- the only two documented paths into RUNNING are QUEUED (a
-- fresh claim) and RUNNING itself (an ownership takeover of a proven-
-- dead owner); the application layer, never SQL, decides which applies
-- (`planning.service.EngineeringPlanningService.claim_job()`), but this
-- trigger closes the one mistake that would be silent and irreversible:
-- overwriting a durable SUCCEEDED/FAILED outcome with anything else.
CREATE TRIGGER planning_jobs_no_reopen_terminal
  BEFORE UPDATE ON planning_jobs
  WHEN OLD.state IN ('SUCCEEDED', 'FAILED') AND NEW.state != OLD.state
  BEGIN SELECT RAISE(ABORT,
    'planning_jobs: a terminal job state can never be changed'); END;
