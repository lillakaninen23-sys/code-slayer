-- Code Slayer schema v20 (H.4.1 -- certified, durable Planner
-- inference timeout).
--
-- Purely additive: no table rebuild, no rewrite of existing rows;
-- migrations 0001-0019 are byte-for-byte unchanged.
--
-- H.4's live deployment (schema v19) failed its first real production
-- planning job after ~31.8s with `adapter_error:transport_timeout`,
-- because `config.bindings.planner_for_worker()` constructed the
-- production Planner's `OpenAICompatibleConfig` with no explicit
-- `timeout`, silently inheriting the adapter's own hardcoded 30.0s
-- default. This migration adds a NINTH durable Planner route-binding
-- field alongside H.4's original eight:
--
--   planner_timeout_seconds  -- the certified Planner INFERENCE request
--                                timeout (`OpenAICompatibleConfig.timeout`
--                                for `/v1/chat/completions` calls) this
--                                job's Planner turn must actually execute
--                                with
--
-- Distinct from `security.live_certification.LiveOllamaRuntimeExpectation.
-- timeout`, which only bounds the separate, already-existing
-- `/api/version`/`/api/tags` runtime-attestation probe traffic -- never
-- conflated with it.
--
-- Nullable for backwards compatibility: every planning_jobs row created
-- BEFORE this migration -- including every schema-v19 row, which
-- already has a complete eight-field route binding -- has no durable
-- Planner timeout to record (it was never selected/certified as part
-- of that job's own routing authority) and is never rewritten to claim
-- one it never actually had. `ALTER TABLE ... ADD COLUMN` never
-- re-fires an INSERT trigger against already-existing rows, so every
-- historical row (pre-v19 AND v19) lands NULL without ever passing
-- through the new completeness trigger below. A legacy QUEUED/
-- reclaimable-RUNNING v19 row discovered after this migration is
-- refused at execution time (`planning.routing.route_binding_from_job()`
-- treats a NULL `planner_timeout_seconds` exactly like a NULL
-- `worker_id` -- unbound) -- never guessed forward onto any timeout,
-- certified or not.
--
-- Every NEW row this schema version onward MUST carry a complete
-- nine-field binding -- enforced at two independent layers: `store.
-- planning_jobs_repo.PlanningJobsRepo.create_in_transaction()` requires
-- `planner_timeout_seconds` as a mandatory Python parameter (the
-- ordinary code path), and the extended `planning_jobs_require_route_
-- binding_on_insert` trigger below is the schema-level backstop against
-- any other insertion path ever landing an incomplete row.

BEGIN;

ALTER TABLE planning_jobs ADD COLUMN planner_timeout_seconds REAL;

-- SQLite has no ALTER TRIGGER -- 0019's `planning_jobs_no_mutate_identity`
-- is dropped and recreated with the same original conditions PLUS
-- `planner_timeout_seconds`, so a queued job can never silently adopt a
-- later timeout after creation. If current config/certification later
-- changes the timeout, the job stays bound to the one it was queued
-- with and fails closed at execution (`ROUTE_BINDING_STALE`) -- it is
-- never silently upgraded to the new timeout (see `planning.routing`'s
-- own module docstring for the full no-reroute invariant).
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
    OR (NEW.planner_timeout_seconds IS NOT OLD.planner_timeout_seconds)
  BEGIN SELECT RAISE(ABORT,
    'planning_jobs: job identity fields cannot be changed after creation'); END;

-- A NEW row (this schema version onward) must carry a complete
-- nine-field route binding -- schema-level backstop for the
-- Python-level requirement in `PlanningJobsRepo.create_in_transaction()`.
-- Never fires for a row ALTER TABLE already created above (no INSERT
-- re-fires), so every pre-v20 historical row (including every v19 row)
-- remains exactly as it was.
DROP TRIGGER planning_jobs_require_route_binding_on_insert;
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
    OR NEW.planner_timeout_seconds IS NULL
  BEGIN SELECT RAISE(ABORT,
    'planning_jobs: a new row must carry a complete planner route binding'); END;
