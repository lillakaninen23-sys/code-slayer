-- Code Slayer schema v5 (Phase 7.7 — persistent local worker runner
-- control state).
--
-- Purely additive: no ALTER of any table; migrations 0001-0004 are
-- byte-for-byte unchanged.
--
-- One durable "run" row per `runner.local_worker_runner.LocalWorkerRunner.
-- start()` call. This is an APPLICATION-level orchestration record,
-- layered ABOVE the existing task state machine (`tasks.state`/
-- `current_phase`, `core.transitions`) -- never a replacement or
-- redesign of it. A run can exist, and be `BLOCKED_ON_QUESTIONS`,
-- before any `tasks` row exists at all: Prompt Analyst / Question Gate
-- evaluation happens before repository/task setup
-- (`docs/CODE_SLAYER_VISION.md` §32-35). Once execution begins,
-- `runner_runs.task_id` names the real task -- in whichever database is
-- that run's chosen execution plane (the control database itself for a
-- non-isolated read-only run, or a managed job worktree's own separate
-- `state.db` for an isolated one, `repo.job_worktree`) -- driving the
-- rest of the already-existing Phase 1-7.6 machinery completely
-- unmodified.
--
-- `runner_runs` always lives in the CONTROL database (the primary
-- repository's own worktree state directory, `store.location`):
-- durable worker/trust/conformance evidence is control-plane-only
-- (Phase 7.2/7.3), and a run's own orchestration state is control-plane
-- evidence for exactly the same reason -- it must survive independently
-- of whichever disposable job worktree a given run's execution happened
-- to use.

BEGIN;

CREATE TABLE runner_runs (
  run_id                    TEXT PRIMARY KEY,
  created_at                TEXT NOT NULL,
  updated_at                TEXT NOT NULL,
  repo_id                   TEXT NOT NULL,
  primary_worktree_id       TEXT NOT NULL,
  original_prompt_hash      TEXT NOT NULL,
  worker_id                 TEXT NOT NULL,
  role                      TEXT NOT NULL,
  requires_mutation         INTEGER NOT NULL DEFAULT 0,
  status                    TEXT NOT NULL,   -- RunStatus (application-level; never a TaskState)
  task_id                   TEXT,            -- set once an execution-plane task exists
  execution_worktree_id     TEXT,            -- primary_worktree_id, or a job worktree's own
  job_worktree_path         TEXT,            -- set only when isolation was actually used
  analysis_content_hash     TEXT,            -- content_blobs reference (workers.prompt_provenance)
  final_text_content_hash   TEXT,            -- content_blobs reference; never raw text in this row
  tool_operation_id         TEXT,            -- the durable tool_operations row this run produced
  questions_json            TEXT,            -- pending ASK questions (small; never large content)
  reason                    TEXT             -- last stable machine-readable outcome reason
);

CREATE INDEX ix_runner_runs_status ON runner_runs(status);

-- Every explicit human/application resolution answering one blocked
-- ambiguity -- append-only, matching `worker_trust_events`' own
-- durable-history convention (0002_worker_trust.sql): the *current*
-- resolution for one (run_id, ambiguity_id) is always its most recent
-- row here, never an overwritten column. A human revising an earlier
-- answer adds a new row; the old one remains a truthful historical
-- record.
CREATE TABLE runner_human_resolutions (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id                TEXT NOT NULL REFERENCES runner_runs(run_id),
  ambiguity_id          TEXT NOT NULL,
  source                TEXT NOT NULL,   -- EvidenceSource: ORIGINAL_PROMPT | DURABLE_TASK_EVIDENCE
  resolution_kind       TEXT NOT NULL,   -- ResolutionKind: FACT | AUTHORIZATION
  answer_content_hash   TEXT NOT NULL,   -- the human's actual answer text, in content_blobs
  created_at            TEXT NOT NULL
);

CREATE INDEX ix_runner_human_resolutions_scope
  ON runner_human_resolutions(run_id, ambiguity_id, id);

CREATE TRIGGER runner_human_resolutions_no_update
  BEFORE UPDATE ON runner_human_resolutions
  BEGIN SELECT RAISE(ABORT, 'runner_human_resolutions is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER runner_human_resolutions_no_delete
  BEFORE DELETE ON runner_human_resolutions
  BEGIN SELECT RAISE(ABORT, 'runner_human_resolutions is append-only: DELETE is forbidden'); END;

-- A run's identity and original-prompt binding must never be rewritten
-- after creation -- only its evolving orchestration fields (status,
-- task_id, execution_worktree_id, job_worktree_path,
-- analysis_content_hash, final_text_content_hash, tool_operation_id,
-- questions_json, reason, updated_at) may change as a run progresses.
-- Mirrors `worker_conformance_runs_no_mutate_identity`
-- (0004_worker_conformance_identity_lock.sql)'s own unconditional,
-- every-column-listed pattern.
CREATE TRIGGER runner_runs_no_mutate_identity
  BEFORE UPDATE ON runner_runs
  WHEN NEW.run_id != OLD.run_id
    OR NEW.created_at != OLD.created_at
    OR NEW.repo_id != OLD.repo_id
    OR NEW.primary_worktree_id != OLD.primary_worktree_id
    OR NEW.original_prompt_hash != OLD.original_prompt_hash
    OR NEW.worker_id != OLD.worker_id
    OR NEW.role != OLD.role
    OR NEW.requires_mutation != OLD.requires_mutation
  BEGIN SELECT RAISE(ABORT,
    'runner_runs: run identity fields cannot be changed after creation'); END;
