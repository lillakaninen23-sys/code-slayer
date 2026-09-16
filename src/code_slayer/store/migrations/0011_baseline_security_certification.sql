-- Code Slayer schema v11 (Baseline Security Certification foundation).
--
-- Purely additive: no ALTER of any prior table. `workers` (schema v1) is
-- referenced, not modified.
--
-- `worker_baseline_security_certificates` is durable evidence that one
-- Baseline Security evaluation was performed against a specific worker
-- AND a specific runtime/model/provider configuration ("runtime
-- profile": model_tag/model_digest/endpoint/runtime_version). This is a
-- SEPARATE dimension from `worker_trust_events` (execution authority)
-- and `worker_conformance_runs`/`_results` (capability conformance) --
-- see `code_slayer.workers.security_baseline`'s module docstring.
--
-- Fully append-only, mirroring `worker_conformance_results`/
-- `worker_trust_events` exactly: a certificate is evidence of one
-- evaluation and is never edited or removed after the fact.
-- Re-evaluating a worker (or the same worker under a changed runtime
-- profile) always creates a NEW row -- the current certificate for a
-- given (worker_id, runtime profile) binding is *derived* by reading
-- the most recent matching row (`code_slayer.workers.
-- production_eligibility`), never stored as a separately mutated status
-- column. A certificate for a runtime profile that no longer matches
-- the one currently in use is never silently reused -- it is simply not
-- a match, exactly like no certificate existing at all.
--
-- `outcome`/`hard_disqualifiers_json` are validated by application code
-- (`code_slayer.workers.security_baseline.record_baseline_certificate`),
-- not by a SQL CHECK constraint -- the same convention
-- `worker_conformance_runs.status`/`worker_trust_events.to_level`
-- already follow in this codebase.

BEGIN;

CREATE TABLE worker_baseline_security_certificates (
  certificate_id           TEXT PRIMARY KEY,
  worker_id                TEXT NOT NULL REFERENCES workers(worker_id),
  baseline_version         TEXT NOT NULL,  -- the fixed, code-owned baseline check semantics this evaluation used
  model_tag                TEXT NOT NULL,  -- required: identifies what was actually evaluated
  model_digest             TEXT,           -- optional, further runtime-profile identity
  endpoint                 TEXT,           -- optional
  runtime_version          TEXT,           -- optional
  outcome                  TEXT NOT NULL,  -- PASS | FAIL | HARD_DISQUALIFIED
  hard_disqualifiers_json  TEXT NOT NULL DEFAULT '[]',  -- non-empty only when outcome = HARD_DISQUALIFIED
  evidence_ref             TEXT NOT NULL,  -- REQUIRED for every outcome, never a bare bool with no provenance
  reason                   TEXT NOT NULL,
  issued_at                TEXT NOT NULL
);

-- The exact lookup `code_slayer.workers.production_eligibility` performs:
-- every certificate for one worker, most recent first, to find the
-- latest one whose own recorded runtime-profile fields match the
-- caller's current profile.
CREATE INDEX ix_worker_baseline_security_certificates_worker
  ON worker_baseline_security_certificates(worker_id, issued_at);

CREATE TRIGGER worker_baseline_security_certificates_no_update
  BEFORE UPDATE ON worker_baseline_security_certificates
  BEGIN SELECT RAISE(ABORT,
    'worker_baseline_security_certificates is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER worker_baseline_security_certificates_no_delete
  BEFORE DELETE ON worker_baseline_security_certificates
  BEGIN SELECT RAISE(ABORT,
    'worker_baseline_security_certificates is append-only: DELETE is forbidden'); END;
