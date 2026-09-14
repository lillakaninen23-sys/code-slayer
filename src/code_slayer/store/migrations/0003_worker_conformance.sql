-- Code Slayer schema v3 (Phase 7.3 — durable worker conformance runs).
--
-- Purely additive: no ALTER of any Phase 1-6 table, and 0001_init.sql/
-- 0002_worker_trust.sql are both unchanged. `workers` (schema v1) and
-- `worker_trust_events` (schema v2) are referenced, not modified.
--
-- A conformance suite executes as ONE coherent run: every case result
-- durably references the exact run_id it was produced under
-- (worker_conformance_results.run_id), and current promotion eligibility
-- (code_slayer.workers.promotion) only ever reads results scoped to one
-- exact run — individual PASS results from different historical runs
-- can never be composed into one passing suite (see UNIQUE(run_id,
-- case_name) below, and the app-level "every required case in THIS run"
-- check).

BEGIN;

CREATE TABLE worker_conformance_runs (
  run_id         TEXT PRIMARY KEY,
  worker_id      TEXT NOT NULL REFERENCES workers(worker_id),
  role           TEXT NOT NULL,
  suite_version  TEXT NOT NULL,      -- the fixed, code-owned case list this run was checked against
  started_at     TEXT NOT NULL,
  completed_at   TEXT,               -- NULL while RUNNING
  status         TEXT NOT NULL       -- RUNNING | PASSED | FAILED
);

CREATE INDEX ix_worker_conformance_runs_scope
  ON worker_conformance_runs(worker_id, role, status);

-- A run finalizes exactly once: this trigger fires on ANY update to a
-- row whose *current* status is already terminal (PASSED/FAILED),
-- refusing to silently rewrite a finalized verdict. It does not block
-- the one legitimate RUNNING -> {PASSED,FAILED} transition, since that
-- update's OLD.status is still RUNNING when it happens.
CREATE TRIGGER worker_conformance_runs_no_mutate_finalized
  BEFORE UPDATE ON worker_conformance_runs
  WHEN OLD.status != 'RUNNING'
  BEGIN SELECT RAISE(ABORT,
    'worker_conformance_runs: a finalized run cannot be mutated'); END;

CREATE TRIGGER worker_conformance_runs_no_delete BEFORE DELETE ON worker_conformance_runs
  BEGIN SELECT RAISE(ABORT,
    'worker_conformance_runs is durable evidence: DELETE is forbidden'); END;

CREATE TABLE worker_conformance_results (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id               TEXT NOT NULL REFERENCES worker_conformance_runs(run_id),
  case_name            TEXT NOT NULL,
  passed               INTEGER NOT NULL,   -- 0/1
  reason               TEXT NOT NULL,
  detail_content_hash  TEXT REFERENCES content_blobs(content_hash),  -- reserved; unpopulated in Phase 7.3
  occurred_at          TEXT NOT NULL,
  UNIQUE (run_id, case_name)  -- one result per case per run -- duplicate recording is impossible, not merely discouraged
);

-- Fully append-only, mirroring audit_events/worker_trust_events exactly:
-- a case result is evidence of what actually happened during a specific
-- run and is never edited or removed after the fact.
CREATE TRIGGER worker_conformance_results_no_update BEFORE UPDATE ON worker_conformance_results
  BEGIN SELECT RAISE(ABORT,
    'worker_conformance_results is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER worker_conformance_results_no_delete BEFORE DELETE ON worker_conformance_results
  BEGIN SELECT RAISE(ABORT,
    'worker_conformance_results is append-only: DELETE is forbidden'); END;
