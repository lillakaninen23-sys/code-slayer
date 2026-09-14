-- Code Slayer schema v1 (Foundation Plan, Revision 2.1).
--
-- This file is executed inside a single transaction opened by
-- code_slayer.store.db.migrate() (it begins with BEGIN; and does not
-- COMMIT — the migration runner commits it together with the
-- schema_migrations row that records this version, so the schema upgrade
-- and its own bookkeeping are atomic).
--
-- Two tables here (worker_leases, checkpoints) are schema only in Phase 1:
-- their business logic (lease acquisition/fencing, checkpoint plumbing)
-- is explicitly out of scope until later phases (Foundation Plan §12/§08).
-- Creating their structure now avoids a schema migration later purely to
-- add them.
--
-- Deliberate Phase-1 deviation from the canonical Revision 2.1 schema,
-- explicitly authorized by the Phase 1 brief: tool_operations.lease_generation
-- is NULLABLE here (Revision 2.1 specifies NOT NULL) because no lease
-- manager exists yet to assign a real generation. It becomes NOT NULL in
-- the migration that introduces the lease manager. See
-- adr/0005-operation-journal.md.

BEGIN;

CREATE TABLE schema_migrations (
  version     INTEGER PRIMARY KEY,
  applied_at  TEXT NOT NULL
);

CREATE TABLE tasks (
  task_id         TEXT PRIMARY KEY,
  description     TEXT NOT NULL,
  repo_root       TEXT NOT NULL,        -- display only; identity is repo_id/worktree_id
  repo_id         TEXT NOT NULL,        -- shared across a repo's worktrees
  worktree_id     TEXT NOT NULL,        -- the unit of mutation ownership (future lease manager)
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  state           TEXT NOT NULL,        -- cached current FSM state; no transition validity
                                         --   is enforced by this schema or by TaskRepo (§19 Phase 2)
  current_phase   TEXT,
  config_json     TEXT NOT NULL DEFAULT '{}'
);

-- At most one non-terminal task per worktree (Foundation Plan INV-2).
-- SQLite re-evaluates a partial index's WHERE clause on every UPDATE, so
-- this stays enforced as a task's state changes, not just at INSERT time.
CREATE UNIQUE INDEX ux_worktree_single_active_task
  ON tasks(worktree_id) WHERE state NOT IN ('COMPLETED', 'FAILED');

-- Append-only, hash-chained audit log (Foundation Plan §07, INV-3/INV-4).
-- event_hash = sha256(canonical_json({task_id, seq, event_type, occurred_at,
--   actor_type, actor_id, payload, prev_event_hash})) — see
--   code_slayer.audit.canonical.event_hash. This covers the full event
--   tuple, not payload alone, so a forged actor/time/type/sequence is also
--   detectable by re-verification. It is an integrity signal, not a
--   security boundary: it does not stop a local user with filesystem
--   access from rewriting the database file itself.
CREATE TABLE audit_events (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id          TEXT REFERENCES tasks(task_id),
  seq              INTEGER NOT NULL,     -- monotonic per task_id
  event_type       TEXT NOT NULL,
  occurred_at      TEXT NOT NULL,        -- UTC ISO-8601
  actor_type       TEXT NOT NULL,        -- user | worker | system
  actor_id         TEXT,
  payload_json     TEXT NOT NULL,
  prev_event_hash  TEXT,
  event_hash       TEXT NOT NULL
);

CREATE UNIQUE INDEX ux_audit_task_seq ON audit_events(task_id, seq);

-- Defense in depth: application code (AuditWriter is the sole writer) is
-- the primary guard, but the database itself refuses to let ANY writer,
-- including a future bug, ever update or delete a persisted event.
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events
  BEGIN SELECT RAISE(ABORT, 'audit_events is append-only: DELETE is forbidden'); END;

-- Content-addressed evidence store (Foundation Plan §07/§11, Revision 2.1).
-- exportable defaults to 0 (deny by default): a blob must be explicitly
-- classified exportable, at creation time, based on what it structurally
-- is (source_kind) — never by scanning its bytes for secret-looking
-- content. No heuristic secret scanner is used or required.
CREATE TABLE content_blobs (
  content_hash  TEXT PRIMARY KEY,         -- sha256 of raw bytes; stored at blobs/<hh>/<hash>
  media_type    TEXT NOT NULL,
  source_kind   TEXT NOT NULL,            -- rules_snapshot|plan|patch_proposal|command_output|tool_read_output|...
  byte_size     INTEGER NOT NULL,
  truncated     INTEGER NOT NULL DEFAULT 0,  -- 1 if capture was cut at a configured max size
  exportable    INTEGER NOT NULL DEFAULT 0,  -- deny by default
  created_at    TEXT NOT NULL
);

-- Write-ahead journal for every side effect SQLite cannot make atomic
-- with itself: a filesystem write, a Git plumbing call, a subprocess exec
-- (Foundation Plan §04/§15). No FK to worker_leases(worktree_id): the
-- lease manager does not exist yet in Phase 1, and a real FK there would
-- make it impossible to journal an operation without one, which is
-- exactly what Phase 1 needs to be able to test independently.
CREATE TABLE tool_operations (
  operation_id         TEXT PRIMARY KEY,
  task_id              TEXT NOT NULL REFERENCES tasks(task_id),
  worktree_id          TEXT NOT NULL,
  worker_id            TEXT NOT NULL,
  worker_session_id    TEXT NOT NULL,
  lease_generation     INTEGER,             -- nullable in Phase 1 — see file header
  tool_name            TEXT NOT NULL,
  risk_class           TEXT NOT NULL,       -- READ|WRITE|EXECUTE|GIT_MUTATION|DESTRUCTIVE
  request_hash         TEXT NOT NULL,       -- sha256 of the canonical tool input
  target_resource      TEXT NOT NULL,       -- path / ref / command identity
  child_pid            INTEGER,             -- set only if this operation spawns a subprocess
  child_pid_started_at TEXT,
  started_at           TEXT NOT NULL,
  finished_at          TEXT,
  status               TEXT NOT NULL,       -- STARTED | SUCCEEDED | FAILED | UNKNOWN
  before_evidence      TEXT,                -- hash / sentinel of target pre-state
  after_evidence       TEXT,                -- hash / sentinel of target post-state
  result_json          TEXT
);

CREATE INDEX ix_toolops_task_status ON tool_operations(task_id, status);

-- The exact set a future checkpoint is built from (Foundation Plan §08).
-- Schema only in Phase 1 — no writer populates this table yet (no WRITE
-- tools exist), but its shape must not need to change once one does.
CREATE TABLE task_owned_paths (
  task_id                   TEXT NOT NULL REFERENCES tasks(task_id),
  path                      TEXT NOT NULL,
  first_owned_at            TEXT NOT NULL,
  last_operation_id         TEXT REFERENCES tool_operations(operation_id),
  deleted                   INTEGER NOT NULL DEFAULT 0,
  pre_existing_override_of  INTEGER REFERENCES audit_events(id),
  PRIMARY KEY (task_id, path)
);

-- Every baseline-dirty or baseline-untracked path, protected by default
-- (Foundation Plan §09/§17, INV-7). Schema only in Phase 1 — no baseline
-- inspector exists yet to populate it.
CREATE TABLE baseline_protected_paths (
  task_id   TEXT NOT NULL REFERENCES tasks(task_id),
  path      TEXT NOT NULL,
  reason    TEXT NOT NULL,   -- pre_existing_dirty | pre_existing_untracked
  PRIMARY KEY (task_id, path)
);

CREATE TABLE repo_baselines (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id         TEXT NOT NULL REFERENCES tasks(task_id),
  recorded_at     TEXT NOT NULL,
  head_sha        TEXT,
  branch          TEXT,
  is_clean        INTEGER NOT NULL,
  dirty_files_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE rules_snapshots (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id         TEXT NOT NULL REFERENCES tasks(task_id),
  source_path     TEXT NOT NULL,
  content_hash    TEXT NOT NULL REFERENCES content_blobs(content_hash),
  precedence_rank INTEGER NOT NULL,
  loaded_at       TEXT NOT NULL
);

CREATE TABLE plans (
  plan_id       TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES tasks(task_id),
  content_hash  TEXT NOT NULL REFERENCES content_blobs(content_hash),
  proposed_by   TEXT NOT NULL,
  accepted_at   TEXT,
  accepted_by   TEXT
);

CREATE TABLE patch_proposals (
  proposal_id   TEXT PRIMARY KEY,
  task_id       TEXT NOT NULL REFERENCES tasks(task_id),
  phase         TEXT NOT NULL,
  worker_id     TEXT NOT NULL,
  content_hash  TEXT NOT NULL REFERENCES content_blobs(content_hash),
  decision      TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | APPLIED | REJECTED
  proposed_at   TEXT NOT NULL,
  decided_at    TEXT
);

CREATE TABLE test_results (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id              TEXT NOT NULL REFERENCES tasks(task_id),
  checkpoint_id        TEXT,
  operation_id         TEXT REFERENCES tool_operations(operation_id),
  command              TEXT NOT NULL,
  status               TEXT NOT NULL,          -- PASS | FAIL | ERROR
  started_at           TEXT NOT NULL,
  finished_at          TEXT,
  output_content_hash  TEXT REFERENCES content_blobs(content_hash),
  summary              TEXT
);

-- Schema only in Phase 1 — no lease manager exists yet (Foundation Plan §12).
CREATE TABLE worker_leases (
  worktree_id           TEXT PRIMARY KEY,
  task_id               TEXT NOT NULL REFERENCES tasks(task_id),
  worker_id             TEXT NOT NULL,
  worker_session_id     TEXT NOT NULL,
  generation            INTEGER NOT NULL DEFAULT 0,
  acquired_at           TEXT NOT NULL,
  heartbeat_at          TEXT NOT NULL,
  status                TEXT NOT NULL,       -- ACTIVE | QUIESCING | EXPIRED | RELEASED
  worker_pid            INTEGER,
  worker_pid_started_at TEXT,
  checkpoint_id         TEXT
);

CREATE TABLE workers (
  worker_id           TEXT PRIMARY KEY,
  kind                TEXT NOT NULL,      -- provider-defined worker kind
  network_class       TEXT NOT NULL,      -- local | cloud
  capabilities_json   TEXT NOT NULL DEFAULT '[]',
  availability_state  TEXT NOT NULL,
  available_after     TEXT,
  last_probe_at       TEXT,
  last_error          TEXT
);

-- Schema only in Phase 1 — no checkpoint manager exists yet (Foundation Plan §08).
CREATE TABLE checkpoints (
  checkpoint_id     TEXT PRIMARY KEY,
  task_id           TEXT NOT NULL REFERENCES tasks(task_id),
  seq               INTEGER NOT NULL,
  parent_checkpoint TEXT,
  created_at        TEXT NOT NULL,
  phase             TEXT NOT NULL,
  status            TEXT NOT NULL,        -- WIP | COMPLETE
  safe_to_resume    INTEGER NOT NULL,     -- 0/1
  git_ref           TEXT,                 -- refs/codeslayer/checkpoints/<task>/<seq>
  git_branch        TEXT,
  worker_id         TEXT,
  completed_json    TEXT NOT NULL DEFAULT '[]',
  pending_json      TEXT NOT NULL DEFAULT '[]',
  verified_json     TEXT NOT NULL DEFAULT '{}',
  changed_files_json TEXT NOT NULL DEFAULT '[]',
  next_action       TEXT
);
