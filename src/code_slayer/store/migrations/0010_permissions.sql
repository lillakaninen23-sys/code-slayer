-- Code Slayer schema v10 (CSLR Governance Foundation, slice G2 — the
-- first machine-enforced Permission Engine).
--
-- Purely additive: no ALTER of any table; migrations 0001-0009 are
-- byte-for-byte unchanged.
--
-- Implements the durable half of `docs/PERMISSIONS_MODEL.md`: an
-- append-only request -> decision -> grant -> revocation chain, never a
-- single mutable "permission" row. Every table here is INSERT-only
-- (triggers below refuse UPDATE and DELETE unconditionally) — historical
-- permission evidence must never become silently mutable or deletable,
-- exactly like `runner_human_resolutions`/`repository_intelligence_
-- snapshots`/`engineering_plan_human_resolutions` already established.
-- Effective state (PENDING vs ALLOWED/DENIED, ACTIVE vs REVOKED) is
-- always *derived* by reading this append-only history
-- (`permissions.service.PermissionService`), never stored as a mutable
-- status column anyone could flip out from under the evidence.
--
-- `permission_requests` rows are created only by trusted backend
-- application code (`permissions.service.PermissionService.request()`)
-- — never accepted verbatim from an HTTP body, model output, or
-- planner output. `permission_key`/`semantic_version` here name an
-- entry in the code-owned `permissions.definitions.PERMISSION_
-- DEFINITIONS` registry (never itself stored in this schema — trusted
-- definitions live in Python source, not a mutable database row).
--
-- `permission_decisions.request_id UNIQUE` is the DB-level defense
-- (independent of any Python-level check) that makes "double decision
-- cannot create duplicate/conflicting grants" and "concurrent ALLOW/
-- DENY race has one authoritative durable outcome" true regardless of
-- application-layer bugs: a second INSERT attempt for an
-- already-decided request always fails with a constraint violation,
-- never a second, conflicting decision row.
--
-- `permission_grants.request_id UNIQUE` mirrors the same discipline: at
-- most one grant per request, created only alongside an ALLOW decision.
-- `permission_revocations.grant_id UNIQUE` gives the identical guarantee
-- for revocation — a grant can be revoked at most once; a second revoke
-- attempt is a safe no-op at the service layer, never a second row.
--
-- `authority_origin` on `permission_grants` is always `USER_EXPLICIT` in
-- this phase (`permissions.definitions.AuthorityOrigin`) — no other
-- origin is valid; MODEL/PLANNER/WORKER are never valid values here, by
-- construction (nothing in `permissions.service` ever writes them).

BEGIN;

CREATE TABLE permission_requests (
  request_id            TEXT PRIMARY KEY,
  created_at            TEXT NOT NULL,
  repo_id               TEXT NOT NULL,
  worktree_id           TEXT NOT NULL,
  permission_key        TEXT NOT NULL,
  semantic_version      TEXT NOT NULL,
  resource              TEXT,
  purpose               TEXT NOT NULL,
  requesting_subsystem  TEXT NOT NULL
);

CREATE INDEX ix_permission_requests_scope
  ON permission_requests(repo_id, worktree_id, created_at);

CREATE TRIGGER permission_requests_no_update
  BEFORE UPDATE ON permission_requests
  BEGIN SELECT RAISE(ABORT, 'permission_requests is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER permission_requests_no_delete
  BEFORE DELETE ON permission_requests
  BEGIN SELECT RAISE(ABORT, 'permission_requests is append-only: DELETE is forbidden'); END;

CREATE TABLE permission_decisions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id    TEXT NOT NULL UNIQUE REFERENCES permission_requests(request_id),
  decision      TEXT NOT NULL,   -- ALLOW | DENY
  decided_at    TEXT NOT NULL
);

CREATE INDEX ix_permission_decisions_request ON permission_decisions(request_id);

CREATE TRIGGER permission_decisions_no_update
  BEFORE UPDATE ON permission_decisions
  BEGIN SELECT RAISE(ABORT, 'permission_decisions is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER permission_decisions_no_delete
  BEFORE DELETE ON permission_decisions
  BEGIN SELECT RAISE(ABORT, 'permission_decisions is append-only: DELETE is forbidden'); END;

CREATE TABLE permission_grants (
  grant_id            TEXT PRIMARY KEY,
  request_id          TEXT NOT NULL UNIQUE REFERENCES permission_requests(request_id),
  permission_key      TEXT NOT NULL,
  semantic_version    TEXT NOT NULL,
  resource            TEXT,
  authority_origin    TEXT NOT NULL,   -- USER_EXPLICIT (only valid value this phase)
  granted_at          TEXT NOT NULL,
  expiry              TEXT
);

CREATE INDEX ix_permission_grants_lookup
  ON permission_grants(permission_key, semantic_version, resource);

CREATE TRIGGER permission_grants_no_update
  BEFORE UPDATE ON permission_grants
  BEGIN SELECT RAISE(ABORT, 'permission_grants is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER permission_grants_no_delete
  BEFORE DELETE ON permission_grants
  BEGIN SELECT RAISE(ABORT, 'permission_grants is append-only: DELETE is forbidden'); END;

CREATE TABLE permission_revocations (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  grant_id      TEXT NOT NULL UNIQUE REFERENCES permission_grants(grant_id),
  revoked_at    TEXT NOT NULL
);

CREATE INDEX ix_permission_revocations_grant ON permission_revocations(grant_id);

CREATE TRIGGER permission_revocations_no_update
  BEFORE UPDATE ON permission_revocations
  BEGIN SELECT RAISE(ABORT, 'permission_revocations is append-only: UPDATE is forbidden'); END;

CREATE TRIGGER permission_revocations_no_delete
  BEFORE DELETE ON permission_revocations
  BEGIN SELECT RAISE(ABORT, 'permission_revocations is append-only: DELETE is forbidden'); END;
