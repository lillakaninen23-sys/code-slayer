-- Code Slayer schema v12 (Role Qualification Certification foundation).
--
-- Purely additive: no ALTER of any prior table. `workers` (schema v1) is
-- referenced, not modified.
--
-- `worker_role_certificates` is durable evidence that one role-specific
-- qualification certification decision was made for a specific worker,
-- a specific fixed-vocabulary role (PLANNER/CODER/REVIEWER/REPAIRER/
-- SECURITY), AND a specific runtime/model/provider configuration
-- ("runtime profile": model_tag/model_digest/endpoint/runtime_version).
-- This is a SEPARATE dimension from `worker_trust_events` (execution
-- authority), `worker_conformance_runs`/`_results` (capability
-- conformance), and `worker_baseline_security_certificates` (the
-- mandatory, role-independent Security floor) -- see
-- `code_slayer.workers.role_qualification`'s module docstring.
--
-- Fully append-only, mirroring `worker_baseline_security_certificates`
-- exactly: a certificate is evidence of one certification decision and
-- is never edited or removed after the fact. Re-evaluating a worker (or
-- the same worker under a changed runtime profile or qualification
-- policy version) always creates a NEW row -- the current certificate
-- for a given `(worker_id, role, runtime profile)` binding is *derived*
-- by reading the most recent matching row (`code_slayer.workers.
-- production_eligibility`), never stored as a separately mutated status
-- column.
--
-- `role`/`outcome` are validated by application code
-- (`code_slayer.workers.role_qualification.record_role_certificate`),
-- not by a SQL CHECK constraint -- the same convention every other
-- evidence table in this codebase already follows.

BEGIN;

CREATE TABLE worker_role_certificates (
  certificate_id    TEXT PRIMARY KEY,
  worker_id         TEXT NOT NULL REFERENCES workers(worker_id),
  role              TEXT NOT NULL,  -- PLANNER | CODER | REVIEWER | REPAIRER | SECURITY
  policy_version    TEXT NOT NULL,  -- the fixed, code-owned qualification policy/version this decision used
  model_tag         TEXT NOT NULL,  -- required: identifies what was actually evaluated
  model_digest      TEXT,           -- optional, further runtime-profile identity
  endpoint          TEXT,           -- optional
  runtime_version   TEXT,           -- optional
  outcome           TEXT NOT NULL,  -- PASS | FAIL
  classification    TEXT NOT NULL,  -- richer role-specific evidence detail behind outcome
  evidence_ref      TEXT NOT NULL,  -- REQUIRED for every outcome, never a bare bool with no provenance
  reason            TEXT NOT NULL,
  issued_at         TEXT NOT NULL
);

-- The exact lookup `code_slayer.workers.production_eligibility` performs:
-- every certificate for one (worker, role), most recent first, to find
-- the latest one whose own recorded runtime-profile fields match the
-- caller's current profile.
CREATE INDEX ix_worker_role_certificates_worker_role
  ON worker_role_certificates(worker_id, role, issued_at);

CREATE TRIGGER worker_role_certificates_no_update
  BEFORE UPDATE ON worker_role_certificates
  BEGIN SELECT RAISE(ABORT,
    'worker_role_certificates is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER worker_role_certificates_no_delete
  BEFORE DELETE ON worker_role_certificates
  BEGIN SELECT RAISE(ABORT,
    'worker_role_certificates is append-only: DELETE is forbidden'); END;
