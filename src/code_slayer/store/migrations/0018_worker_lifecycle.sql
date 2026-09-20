-- Code Slayer schema v18 (H.3: durable administrative worker lifecycle).
--
-- Purely additive: two nullable/defaulted columns on `workers`, one
-- application-level `CHECK` on the new column only (never referencing
-- any other column, so `ALTER TABLE ADD COLUMN` applies it without a
-- table rebuild). No ALTER of any other table, no rewrite or deletion
-- of any existing row, no change to any certificate/history table.
--
-- `lifecycle_state` is the ONE authoritative source of whether a
-- worker may receive new production/certification work
-- (`code_slayer.workers.lifecycle`, `code_slayer.workers.
-- production_eligibility`) -- a purely administrative dimension,
-- deliberately separate from `availability_state` (runtime health/
-- reachability). Every existing worker migrates to 'ACTIVE'
-- automatically via the column DEFAULT; no row's other columns are
-- touched.
--
-- `lifecycle_changed_at` is NULL for every worker that has never been
-- archived or reactivated (including every pre-v18 worker after this
-- migration) -- it is set only by an explicit lifecycle transition,
-- never backfilled or guessed for historical rows.
--
-- The CHECK constraint is enforced by SQLite on UPDATE as well as
-- INSERT (verified against the project's bundled SQLite 3.46), so an
-- invalid lifecycle value is rejected at the database layer even if a
-- future caller bypassed `WorkersRepo`'s own validation.

BEGIN;

ALTER TABLE workers
  ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'ACTIVE'
    CHECK (lifecycle_state IN ('ACTIVE', 'ARCHIVED'));

ALTER TABLE workers
  ADD COLUMN lifecycle_changed_at TEXT;
