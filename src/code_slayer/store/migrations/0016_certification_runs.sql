-- Code Slayer schema v16 (Certification Center durable runs).
--
-- Purely additive. A certification RUN is distinct from a Baseline
-- Security CERTIFICATE: incomplete/unverified attempts may exist with
-- no certificate row. Certificates remain append-only in
-- worker_baseline_security_certificates. This table is the operator
-- job record the WebUI polls; identity fields are immutable after
-- insert. Terminal states cannot be reopened.

BEGIN;

CREATE TABLE certification_runs (
  run_id                                  TEXT PRIMARY KEY,
  worker_id                               TEXT NOT NULL REFERENCES workers(worker_id),
  kind                                    TEXT NOT NULL,
  environment                             TEXT NOT NULL,
  state                                   TEXT NOT NULL,
  reason                                  TEXT,
  preflight_json                          TEXT NOT NULL DEFAULT '[]',
  expected_runtime_identity_fingerprint   TEXT,
  model_tag                               TEXT,
  model_digest                            TEXT,
  ollama_root                             TEXT,
  certificate_id                          TEXT,
  evidence_ref                            TEXT,
  hard_disqualifiers_json                 TEXT NOT NULL DEFAULT '[]',
  created_at                              TEXT NOT NULL,
  updated_at                              TEXT NOT NULL,
  started_at                              TEXT,
  finished_at                             TEXT,
  attempt                                 INTEGER NOT NULL DEFAULT 0,
  owner_pid                               INTEGER,
  owner_pid_started_at                    TEXT,
  owner_generation                        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX ix_certification_runs_worker ON certification_runs(worker_id, created_at);
CREATE INDEX ix_certification_runs_state ON certification_runs(state);

CREATE TRIGGER certification_runs_no_mutate_identity
  BEFORE UPDATE ON certification_runs
  WHEN NEW.run_id != OLD.run_id
    OR NEW.worker_id != OLD.worker_id
    OR NEW.kind != OLD.kind
    OR NEW.environment != OLD.environment
    OR NEW.created_at != OLD.created_at
  BEGIN SELECT RAISE(ABORT,
    'certification_runs: identity fields cannot be changed after creation'); END;

CREATE TRIGGER certification_runs_no_reopen_terminal
  BEFORE UPDATE ON certification_runs
  WHEN OLD.state IN ('PASS', 'FAIL', 'HARD_DISQUALIFIED', 'INCOMPLETE')
   AND NEW.state != OLD.state
  BEGIN SELECT RAISE(ABORT,
    'certification_runs: a terminal run state can never be changed'); END;

