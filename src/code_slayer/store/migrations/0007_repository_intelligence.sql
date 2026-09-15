-- Code Slayer schema v7 (Phase 8.1 — deterministic repository
-- intelligence foundation).
--
-- Purely additive: no ALTER of any table; migrations 0001-0006 are
-- byte-for-byte unchanged.
--
-- A snapshot row is a small, durable identity/pointer record only — the
-- actual bounded inventory/project/command/symbol/graph content is a
-- single content-addressed JSON blob in the existing `content_blobs`
-- table (`intelligence.store`, `source_kind="repository_intelligence_
-- snapshot"`), never duplicated into this schema. This mirrors every
-- other durable-evidence table in this codebase (`worker_conformance_
-- runs`, `runner_runs`): a thin row plus a blob reference, never the
-- large payload itself in a TEXT column.
--
-- Append-only, like `worker_trust_events`/`runner_human_resolutions`: a
-- fresh `inspect()` call always INSERTs a new row for the same
-- (repo_id, worktree_id) scope rather than overwriting the previous one
-- — "current" is simply the most recently created row for that scope,
-- and stale snapshots remain a truthful historical record rather than
-- being destroyed.
--
-- `head_sha` is nullable (an unborn repository has none).
-- `working_tree_fingerprint` is a deterministic, cheap (size + mtime,
-- never full content) fingerprint over the exact bounded file set this
-- snapshot actually indexed — together with `head_sha` and
-- `working_tree_dirty`, it is what lets a later caller detect that the
-- working tree has changed since this snapshot was taken even when
-- `head_sha` itself has not moved (Phase 8.1's own "snapshot identity
-- must make stale information detectable" requirement).

BEGIN;

CREATE TABLE repository_intelligence_snapshots (
  snapshot_id               TEXT PRIMARY KEY,
  repo_id                   TEXT NOT NULL,
  worktree_id               TEXT NOT NULL,
  head_sha                  TEXT,
  working_tree_dirty        INTEGER NOT NULL,
  working_tree_fingerprint  TEXT NOT NULL,
  index_version             TEXT NOT NULL,
  created_at                TEXT NOT NULL,
  snapshot_content_hash     TEXT NOT NULL,
  file_count                INTEGER NOT NULL,
  inventory_truncated       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX ix_repository_intelligence_snapshots_scope
  ON repository_intelligence_snapshots(repo_id, worktree_id, created_at);

CREATE TRIGGER repository_intelligence_snapshots_no_update
  BEFORE UPDATE ON repository_intelligence_snapshots
  BEGIN SELECT RAISE(ABORT,
    'repository_intelligence_snapshots is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER repository_intelligence_snapshots_no_delete
  BEFORE DELETE ON repository_intelligence_snapshots
  BEGIN SELECT RAISE(ABORT,
    'repository_intelligence_snapshots is append-only: DELETE is forbidden'); END;
