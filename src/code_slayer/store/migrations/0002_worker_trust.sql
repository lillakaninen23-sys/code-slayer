-- Code Slayer schema v2 (Phase 7.2 — durable worker/model trust).
--
-- Purely additive: no ALTER of any Phase 1-6 table. `workers` (schema v1,
-- schema-only since Phase 1 — see adr/0006-worker-model-local-first.md)
-- is unchanged; this migration only adds the append-only trust-history
-- table that references it.
--
-- Deliberate Phase 7.2 minimality (documented here and in
-- code_slayer.workers.trust): trust is scoped by (worker_id, role,
-- capability) only — not a separately normalized model/provider/runtime/
-- version identity. `worker_id` is trusted to identify one concrete
-- configured worker for now; splitting that identity further later is a
-- new column/table addition, not a redesign of this table's transition
-- semantics (from_level/to_level/reason/evidence_ref/occurred_at).
--
-- Append-only by the same convention audit_events already established
-- (Foundation Plan §07, ADR 0004): current trust is DERIVED by reading
-- the latest event for an exact scope, never stored as a separately
-- mutated status column — there is deliberately no way to overwrite or
-- remove a past trust decision's history.

BEGIN;

CREATE TABLE worker_trust_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  worker_id     TEXT NOT NULL REFERENCES workers(worker_id),
  role          TEXT NOT NULL,
  capability    TEXT,               -- NULL = scoped to the whole role
  from_level    TEXT NOT NULL,      -- LOCKED | GUARDED | AUTO (implicit LOCKED when no prior event)
  to_level      TEXT NOT NULL,      -- LOCKED | GUARDED | AUTO
  reason        TEXT NOT NULL,
  evidence_ref  TEXT,               -- required (non-blank) for an upward transition; optional downward
  occurred_at   TEXT NOT NULL
);

-- The exact-scope lookup every current-trust derivation and every
-- transition's optimistic-concurrency re-check performs: latest row for
-- (worker_id, role, capability), ordered by id (insertion order).
CREATE INDEX ix_worker_trust_events_scope
  ON worker_trust_events(worker_id, role, capability, id);

-- Defense in depth, mirroring audit_events' own triggers exactly
-- (migrations/0001_init.sql): application code (WorkerTrustRepo is the
-- sole writer) is the primary guard, but the database itself refuses to
-- let any writer, including a future bug, ever alter or remove a
-- persisted trust event.
CREATE TRIGGER worker_trust_events_no_update BEFORE UPDATE ON worker_trust_events
  BEGIN SELECT RAISE(ABORT, 'worker_trust_events is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER worker_trust_events_no_delete BEFORE DELETE ON worker_trust_events
  BEGIN SELECT RAISE(ABORT, 'worker_trust_events is append-only: DELETE is forbidden'); END;
