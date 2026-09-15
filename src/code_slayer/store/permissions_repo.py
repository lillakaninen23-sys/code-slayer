"""Durable permission request/decision/grant/revocation records (CSLR
Governance Foundation, slice G2, schema v10's `permission_requests`/
`permission_decisions`/`permission_grants`/`permission_revocations`).

A thin, transactional persistence primitive only — mirrors `store.
planning_repo.PlanningRepo`/`store.planning_jobs_repo.PlanningJobsRepo`'s
own "repository does not decide legality" pattern.
`permissions.service.PermissionService` decides what is legal (which
definition exists, whether a decision/revocation is a legitimate
first-time event) and when to call these; this module only durably
records the outcome, relying on the schema's own UNIQUE constraints
(migration 0010) to make a double decision/revocation attempt fail
atomically rather than trusting a Python-level check alone.
"""

from __future__ import annotations

import sqlite3

from code_slayer.store.models import (
    PermissionDecisionRow,
    PermissionGrantRow,
    PermissionRequestRow,
    PermissionRevocationRow,
)


class PermissionsRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # -- requests ---------------------------------------------------------

    def create_request_in_transaction(
        self, *, request_id: str, created_at: str, repo_id: str, worktree_id: str,
        permission_key: str, semantic_version: str, resource: str | None, purpose: str,
        requesting_subsystem: str,
    ) -> PermissionRequestRow:
        if not self._conn.in_transaction:
            raise RuntimeError("permission request creation requires an open write transaction")
        self._conn.execute(
            "INSERT INTO permission_requests "
            "(request_id, created_at, repo_id, worktree_id, permission_key, "
            " semantic_version, resource, purpose, requesting_subsystem) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (request_id, created_at, repo_id, worktree_id, permission_key,
             semantic_version, resource, purpose, requesting_subsystem),
        )
        return self.get_request(request_id)

    def get_request(self, request_id: str) -> PermissionRequestRow:
        row = self._conn.execute(
            "SELECT * FROM permission_requests WHERE request_id = ?", (request_id,),
        ).fetchone()
        if row is None:
            raise KeyError(request_id)
        return _row_to_request(row)

    def get_request_or_none(self, request_id: str) -> PermissionRequestRow | None:
        try:
            return self.get_request(request_id)
        except KeyError:
            return None

    def list_requests_for_scope(
        self, repo_id: str, worktree_id: str, *, limit: int, offset: int,
    ) -> list[PermissionRequestRow]:
        rows = self._conn.execute(
            "SELECT * FROM permission_requests WHERE repo_id = ? AND worktree_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?",
            (repo_id, worktree_id, limit, offset),
        ).fetchall()
        return [_row_to_request(row) for row in rows]

    # -- decisions ----------------------------------------------------------

    def create_decision_in_transaction(
        self, *, request_id: str, decision: str, decided_at: str,
    ) -> PermissionDecisionRow:
        """Raises `sqlite3.IntegrityError` if `request_id` already has a
        decision — the schema's own `UNIQUE` constraint on
        `permission_decisions.request_id`, not an application-level
        check, is what makes this atomic under concurrent callers."""
        if not self._conn.in_transaction:
            raise RuntimeError("permission decision creation requires an open write transaction")
        self._conn.execute(
            "INSERT INTO permission_decisions (request_id, decision, decided_at) "
            "VALUES (?, ?, ?)",
            (request_id, decision, decided_at),
        )
        return self.get_decision_for_request(request_id)

    def get_decision_for_request(self, request_id: str) -> PermissionDecisionRow | None:
        row = self._conn.execute(
            "SELECT * FROM permission_decisions WHERE request_id = ?", (request_id,),
        ).fetchone()
        return _row_to_decision(row) if row is not None else None

    # -- grants ---------------------------------------------------------

    def create_grant_in_transaction(
        self, *, grant_id: str, request_id: str, permission_key: str, semantic_version: str,
        resource: str | None, authority_origin: str, granted_at: str, expiry: str | None,
    ) -> PermissionGrantRow:
        if not self._conn.in_transaction:
            raise RuntimeError("permission grant creation requires an open write transaction")
        self._conn.execute(
            "INSERT INTO permission_grants "
            "(grant_id, request_id, permission_key, semantic_version, resource, "
            " authority_origin, granted_at, expiry) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (grant_id, request_id, permission_key, semantic_version, resource,
             authority_origin, granted_at, expiry),
        )
        return self.get_grant(grant_id)

    def get_grant(self, grant_id: str) -> PermissionGrantRow:
        row = self._conn.execute(
            "SELECT * FROM permission_grants WHERE grant_id = ?", (grant_id,),
        ).fetchone()
        if row is None:
            raise KeyError(grant_id)
        return _row_to_grant(row)

    def get_grant_or_none(self, grant_id: str) -> PermissionGrantRow | None:
        try:
            return self.get_grant(grant_id)
        except KeyError:
            return None

    def get_grant_for_request(self, request_id: str) -> PermissionGrantRow | None:
        row = self._conn.execute(
            "SELECT * FROM permission_grants WHERE request_id = ?", (request_id,),
        ).fetchone()
        return _row_to_grant(row) if row is not None else None

    def list_grants_for_scope(
        self, repo_id: str, worktree_id: str, *, limit: int, offset: int,
    ) -> list[PermissionGrantRow]:
        # permission_grants has no repo_id/worktree_id of its own -- scope
        # through its originating request, which does.
        rows = self._conn.execute(
            "SELECT g.* FROM permission_grants g "
            "JOIN permission_requests r ON r.request_id = g.request_id "
            "WHERE r.repo_id = ? AND r.worktree_id = ? "
            "ORDER BY g.granted_at DESC, g.rowid DESC LIMIT ? OFFSET ?",
            (repo_id, worktree_id, limit, offset),
        ).fetchall()
        return [_row_to_grant(row) for row in rows]

    def find_active_grant(
        self, permission_key: str, semantic_version: str, resource: str | None,
    ) -> PermissionGrantRow | None:
        """The most recent, non-revoked grant matching this *exact*
        `(permission_key, semantic_version, resource)` triple — never a
        wildcard, never a parent-scope match. Expiry is checked by the
        caller (`permissions.service.PermissionService.check()`), not
        here, since "now" is a caller-observable moment, not a query
        this repo should own."""
        row = self._conn.execute(
            "SELECT g.* FROM permission_grants g "
            "WHERE g.permission_key = ? AND g.semantic_version = ? AND g.resource IS ? "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM permission_revocations rv WHERE rv.grant_id = g.grant_id"
            ") ORDER BY g.granted_at DESC, g.rowid DESC LIMIT 1",
            (permission_key, semantic_version, resource),
        ).fetchone()
        return _row_to_grant(row) if row is not None else None

    # -- revocations ----------------------------------------------------

    def create_revocation_in_transaction(
        self, *, grant_id: str, revoked_at: str,
    ) -> PermissionRevocationRow:
        """Raises `sqlite3.IntegrityError` if `grant_id` is already
        revoked — the schema's `UNIQUE` constraint on
        `permission_revocations.grant_id`."""
        if not self._conn.in_transaction:
            raise RuntimeError("permission revocation creation requires an open write transaction")
        self._conn.execute(
            "INSERT INTO permission_revocations (grant_id, revoked_at) VALUES (?, ?)",
            (grant_id, revoked_at),
        )
        return self.get_revocation_for_grant(grant_id)

    def get_revocation_for_grant(self, grant_id: str) -> PermissionRevocationRow | None:
        row = self._conn.execute(
            "SELECT * FROM permission_revocations WHERE grant_id = ?", (grant_id,),
        ).fetchone()
        return _row_to_revocation(row) if row is not None else None


def _row_to_request(row: sqlite3.Row) -> PermissionRequestRow:
    return PermissionRequestRow(
        request_id=row["request_id"], created_at=row["created_at"], repo_id=row["repo_id"],
        worktree_id=row["worktree_id"], permission_key=row["permission_key"],
        semantic_version=row["semantic_version"], resource=row["resource"],
        purpose=row["purpose"], requesting_subsystem=row["requesting_subsystem"],
    )


def _row_to_decision(row: sqlite3.Row) -> PermissionDecisionRow:
    return PermissionDecisionRow(
        id=row["id"], request_id=row["request_id"], decision=row["decision"],
        decided_at=row["decided_at"],
    )


def _row_to_grant(row: sqlite3.Row) -> PermissionGrantRow:
    return PermissionGrantRow(
        grant_id=row["grant_id"], request_id=row["request_id"],
        permission_key=row["permission_key"], semantic_version=row["semantic_version"],
        resource=row["resource"], authority_origin=row["authority_origin"],
        granted_at=row["granted_at"], expiry=row["expiry"],
    )


def _row_to_revocation(row: sqlite3.Row) -> PermissionRevocationRow:
    return PermissionRevocationRow(
        id=row["id"], grant_id=row["grant_id"], revoked_at=row["revoked_at"],
    )
