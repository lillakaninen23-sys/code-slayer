-- Code Slayer schema v4 (Phase 7.3 hardening — conformance run identity
-- immutability).
--
-- Purely additive: no ALTER of any table, and 0001_init.sql/
-- 0002_worker_trust.sql/0003_worker_conformance.sql are all unchanged.
--
-- Gap this closes: 0003's `worker_conformance_runs_no_mutate_finalized`
-- trigger only fires once a run's status is already terminal
-- (PASSED/FAILED) — it says nothing about a row that is still RUNNING,
-- during which any UPDATE, including one that silently rewrites
-- run_id/worker_id/role/suite_version/started_at, was previously
-- unguarded. A conformance run is security evidence; its identity must
-- never be rewritable, at any point in its lifecycle, only its
-- legitimate terminal fields (status, completed_at) may ever change,
-- exactly once, exactly as 0003 already established.
--
-- This trigger is unconditional (no WHEN OLD.status = ...): it fires on
-- every UPDATE and refuses it unless every identity column's NEW value
-- equals its OLD value — which is exactly what the one legitimate
-- RUNNING -> {PASSED,FAILED} finalization UPDATE already does (it only
-- ever sets status/completed_at), so that transition is unaffected.

BEGIN;

CREATE TRIGGER worker_conformance_runs_no_mutate_identity
  BEFORE UPDATE ON worker_conformance_runs
  WHEN NEW.run_id != OLD.run_id
    OR NEW.worker_id != OLD.worker_id
    OR NEW.role != OLD.role
    OR NEW.suite_version != OLD.suite_version
    OR NEW.started_at != OLD.started_at
  BEGIN SELECT RAISE(ABORT,
    'worker_conformance_runs: run identity fields cannot be changed after creation'); END;
