-- Code Slayer schema v19 (H.4 -- durable, production-eligible,
-- worker-bound Planner routing).
--
-- Purely additive: no table rebuild, no rewrite of existing rows;
-- migrations 0001-0018 are byte-for-byte unchanged.
--
-- H.3 deliberately documented (`docs/ENGINEERING_PLANNING.md`, "Known
-- limitations") that no durable `planning_jobs` row recorded which
-- registered worker/model actually supplied the Planner for a turn,
-- and that a lifecycle gate could not be added to `/api/plans` until
-- planner runtime identity became an explicit, durably recorded part
-- of planning-job state. This migration is that missing state.
--
-- Every new H.4 `planning_jobs` row durably records the EXACT
-- backend-selected routing authority for that one job:
--
--   worker_id                     -- which registered worker (workers.worker_id)
--   runtime_identity_fingerprint  -- workers.security_baseline.RuntimeProfileIdentity, at
--                                     selection time
--   role_evaluation_fingerprint   -- workers.role_qualification.RoleEvaluationIdentity, at
--                                     selection time
--   security_certificate_id       -- the exact worker_baseline_security_certificates row
--                                     that made this worker eligible
--   role_certificate_id           -- the exact worker_role_certificates row that made
--                                     this worker eligible for PLANNER
--   output_token_budget           -- the certified completion-length cap this job's
--                                     Planner turn must actually execute with
--   tool_choice_enforcement       -- the certified tool-choice transport profile
--   planner_policy_version        -- the certified role-certification policy version
--
-- All eight are produced ONLY by `planning.routing.select_planner_route()`
-- (server-owned: `workers.production_eligibility.
-- evaluate_production_eligibility()` plus current persistent runtime
-- config) -- never client input, never guessed, never inferred from
-- model tag/adapter type/DB order/config order.
--
-- Nullable for backwards compatibility: every planning_jobs row created
-- BEFORE this migration has no worker identity to record (it never
-- existed) and is never rewritten to claim one it never actually had --
-- `ALTER TABLE ... ADD COLUMN` never re-fires an INSERT trigger against
-- already-existing rows, so historical rows land NULL without ever
-- passing through the new completeness trigger below. A legacy QUEUED/
-- reclaimable-RUNNING row discovered after this migration is refused at
-- execution time (`worker_id IS NULL` => `planner_worker_unbound`,
-- `planning.routing`) -- never guessed forward onto any worker,
-- certified or not.
--
-- Every NEW row this schema version onward MUST carry a complete
-- binding -- enforced at two independent layers: `store.
-- planning_jobs_repo.PlanningJobsRepo.create_in_transaction()` requires
-- every field as a mandatory Python parameter (the ordinary code path),
-- and the `planning_jobs_require_route_binding_on_insert` trigger below
-- is the schema-level backstop against any other insertion path ever
-- landing an incomplete row.

BEGIN;

ALTER TABLE planning_jobs ADD COLUMN worker_id TEXT REFERENCES workers(worker_id);
ALTER TABLE planning_jobs ADD COLUMN runtime_identity_fingerprint TEXT;
ALTER TABLE planning_jobs ADD COLUMN role_evaluation_fingerprint TEXT;
ALTER TABLE planning_jobs ADD COLUMN security_certificate_id TEXT;
ALTER TABLE planning_jobs ADD COLUMN role_certificate_id TEXT;
ALTER TABLE planning_jobs ADD COLUMN output_token_budget INTEGER;
ALTER TABLE planning_jobs ADD COLUMN tool_choice_enforcement TEXT;
ALTER TABLE planning_jobs ADD COLUMN planner_policy_version TEXT;

CREATE INDEX ix_planning_jobs_worker_id ON planning_jobs(worker_id);

-- SQLite has no ALTER TRIGGER -- 0009's `planning_jobs_no_mutate_identity`
-- is dropped and recreated with the same original conditions PLUS the
-- eight new route-binding fields, so a queued job can never be silently
-- rerouted to a different worker or authority after creation. If its
-- bound worker later becomes ineligible, the job stays bound to that
-- worker and fails closed at execution -- it is never reassigned to a
-- different currently-eligible worker (see `planning.routing`'s own
-- module docstring for the full no-reroute invariant).
DROP TRIGGER planning_jobs_no_mutate_identity;
CREATE TRIGGER planning_jobs_no_mutate_identity
  BEFORE UPDATE ON planning_jobs
  WHEN NEW.job_id != OLD.job_id
    OR NEW.plan_id != OLD.plan_id
    OR NEW.created_at != OLD.created_at
    OR NEW.repo_id != OLD.repo_id
    OR NEW.worktree_id != OLD.worktree_id
    OR NEW.kind != OLD.kind
    OR (NEW.predecessor_job_id IS NOT OLD.predecessor_job_id)
    OR (NEW.worker_id IS NOT OLD.worker_id)
    OR (NEW.runtime_identity_fingerprint IS NOT OLD.runtime_identity_fingerprint)
    OR (NEW.role_evaluation_fingerprint IS NOT OLD.role_evaluation_fingerprint)
    OR (NEW.security_certificate_id IS NOT OLD.security_certificate_id)
    OR (NEW.role_certificate_id IS NOT OLD.role_certificate_id)
    OR (NEW.output_token_budget IS NOT OLD.output_token_budget)
    OR (NEW.tool_choice_enforcement IS NOT OLD.tool_choice_enforcement)
    OR (NEW.planner_policy_version IS NOT OLD.planner_policy_version)
  BEGIN SELECT RAISE(ABORT,
    'planning_jobs: job identity fields cannot be changed after creation'); END;

-- A NEW row (this schema version onward) must carry a complete route
-- binding -- schema-level backstop for the Python-level requirement in
-- `PlanningJobsRepo.create_in_transaction()`. Never fires for a row
-- ALTER TABLE already created above (no INSERT re-fires), so every
-- pre-v19 historical row remains exactly as it was.
CREATE TRIGGER planning_jobs_require_route_binding_on_insert
  BEFORE INSERT ON planning_jobs
  WHEN NEW.worker_id IS NULL
    OR NEW.runtime_identity_fingerprint IS NULL
    OR NEW.role_evaluation_fingerprint IS NULL
    OR NEW.security_certificate_id IS NULL
    OR NEW.role_certificate_id IS NULL
    OR NEW.output_token_budget IS NULL
    OR NEW.tool_choice_enforcement IS NULL
    OR NEW.planner_policy_version IS NULL
  BEGIN SELECT RAISE(ABORT,
    'planning_jobs: a new row must carry a complete planner route binding'); END;
