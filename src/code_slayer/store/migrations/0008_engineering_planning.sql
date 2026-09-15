-- Code Slayer schema v8 (Phase 8.2 — durable engineering planning
-- foundation).
--
-- Purely additive: no ALTER of any table; migrations 0001-0007 are
-- byte-for-byte unchanged.
--
-- `engineering_plans` mirrors `runner_runs` (0005_runner_state.sql)
-- exactly: one mutable orchestration row per plan revision, identity
-- fields locked after creation, evolving fields (state/reason/content
-- hashes/questions) updatable in place. The actual planning content
-- (goal, requirements, affected files, planned changes, risks,
-- verification steps, discovered commands, evidence references, ...)
-- is never a column here -- it is a single content-addressed JSON blob
-- in `content_blobs` (`planning.provenance`, `source_kind=
-- "engineering_plan_content"`), exactly like `repository_intelligence_
-- snapshots`/`runner_runs` already keep their own large payloads out of
-- this schema.
--
-- This is PLANNING ONLY: this table has no `task_id` naming a mutating
-- task, no lease/fencing/checkpoint reference, and no trust-level
-- column -- a row here authorizes nothing.
--
-- Repository binding (`repo_id`, `worktree_id`, `head_sha`,
-- `working_tree_dirty`, `working_tree_fingerprint`,
-- `intelligence_snapshot_id`) is captured at planning time from
-- `intelligence.service.RepositoryIntelligenceService` -- the exact
-- same identity fields `repository_intelligence_snapshots` already
-- durably records (Phase 8.1/8.1a) -- so a plan's binding can be
-- compared against that service's *current* identity later without
-- this schema (or `planning.service`) ever recomputing or duplicating
-- the fingerprint algorithm itself.
--
-- `predecessor_plan_id`/`revision` form an append-only revision chain:
-- `replan()` always INSERTs a new row referencing its predecessor
-- rather than rewriting the old one's content -- the old row's `state`
-- transitions to SUPERSEDED (an evolving field, like `runner_runs.
-- status`), but every historical row, and every content blob it
-- references, remains untouched and readable forever.
--
-- `state` is `planning.models.PlanState`: DRAFT | NEEDS_INPUT | READY |
-- SUPERSEDED. STALE is deliberately NOT a value ever stored here --
-- staleness is a *computed* property (`planning.service.
-- EngineeringPlanningService`), comparing this row's own repository
-- binding against `RepositoryIntelligenceService`'s live identity on
-- every read, exactly mirroring how Phase 8.1a's own `Status.current`
-- is computed rather than durably flipped. Persisting a durable STALE
-- transition would mean either re-checking and rewriting rows nobody
-- asked about, or letting a row silently go stale without anything
-- ever updating it -- both duplicate authority `RepositoryIntelligenceService`
-- already holds.

BEGIN;

CREATE TABLE engineering_plans (
  plan_id                      TEXT PRIMARY KEY,
  created_at                   TEXT NOT NULL,
  updated_at                   TEXT NOT NULL,
  schema_version                TEXT NOT NULL,
  repo_id                      TEXT NOT NULL,
  worktree_id                  TEXT NOT NULL,
  run_id                       TEXT,             -- optional runner_runs association
  request_content_hash         TEXT NOT NULL,    -- the original user engineering request
  predecessor_plan_id          TEXT REFERENCES engineering_plans(plan_id),
  revision                     INTEGER NOT NULL DEFAULT 1,
  state                        TEXT NOT NULL,    -- PlanState: DRAFT|NEEDS_INPUT|READY|SUPERSEDED
  reason                       TEXT,             -- last stable machine-readable outcome reason
  head_sha                     TEXT,             -- repository binding at planning time
  working_tree_dirty           INTEGER NOT NULL DEFAULT 0,
  working_tree_fingerprint     TEXT,             -- Phase 8.1a content-safe identity at bind time
  intelligence_snapshot_id     TEXT,             -- which durable Repository Intelligence snapshot
  planner_input_content_hash   TEXT,             -- content_blobs: bounded PlannerRequest sent
  planner_output_content_hash  TEXT,             -- content_blobs: raw structured planner output
  validation_content_hash      TEXT,             -- content_blobs: evidence validation result
  plan_content_hash             TEXT,             -- content_blobs: validated EngineeringPlanContent
  questions_json                TEXT              -- pending blocking open questions (small only)
);

CREATE INDEX ix_engineering_plans_scope
  ON engineering_plans(repo_id, worktree_id, created_at);

CREATE INDEX ix_engineering_plans_run_id ON engineering_plans(run_id);

-- Every explicit human/application resolution answering one blocked
-- planning ambiguity -- append-only, byte-for-byte the same shape and
-- discipline as `runner_human_resolutions` (0005_runner_state.sql):
-- the *current* resolution for one (plan_id, ambiguity_id) is always
-- its most recent row, never an overwritten column.
CREATE TABLE engineering_plan_human_resolutions (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  plan_id               TEXT NOT NULL REFERENCES engineering_plans(plan_id),
  ambiguity_id          TEXT NOT NULL,
  source                TEXT NOT NULL,   -- EvidenceSource: ORIGINAL_PROMPT | DURABLE_TASK_EVIDENCE
  resolution_kind       TEXT NOT NULL,   -- ResolutionKind: FACT | AUTHORIZATION
  answer_content_hash   TEXT NOT NULL,   -- the human's actual answer text, in content_blobs
  created_at            TEXT NOT NULL
);

CREATE INDEX ix_engineering_plan_human_resolutions_scope
  ON engineering_plan_human_resolutions(plan_id, ambiguity_id, id);

CREATE TRIGGER engineering_plan_human_resolutions_no_update
  BEFORE UPDATE ON engineering_plan_human_resolutions
  BEGIN SELECT RAISE(ABORT,
    'engineering_plan_human_resolutions is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER engineering_plan_human_resolutions_no_delete
  BEFORE DELETE ON engineering_plan_human_resolutions
  BEGIN SELECT RAISE(ABORT,
    'engineering_plan_human_resolutions is append-only: DELETE is forbidden'); END;

-- A plan revision's identity and request binding must never be
-- rewritten after creation -- only its evolving orchestration fields
-- (state, reason, head_sha/working_tree_dirty/working_tree_fingerprint/
-- intelligence_snapshot_id, the four content-hash pointers,
-- questions_json, updated_at) may change as planning progresses.
-- Mirrors `runner_runs_no_mutate_identity` (0005_runner_state.sql).
CREATE TRIGGER engineering_plans_no_mutate_identity
  BEFORE UPDATE ON engineering_plans
  WHEN NEW.plan_id != OLD.plan_id
    OR NEW.created_at != OLD.created_at
    OR NEW.schema_version != OLD.schema_version
    OR NEW.repo_id != OLD.repo_id
    OR NEW.worktree_id != OLD.worktree_id
    OR (NEW.run_id IS NOT OLD.run_id)
    OR NEW.request_content_hash != OLD.request_content_hash
    OR (NEW.predecessor_plan_id IS NOT OLD.predecessor_plan_id)
    OR NEW.revision != OLD.revision
  BEGIN SELECT RAISE(ABORT,
    'engineering_plans: plan identity fields cannot be changed after creation'); END;
