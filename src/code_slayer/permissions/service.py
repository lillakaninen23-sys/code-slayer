"""`PermissionService`: the one public entry point for CSLR's Permission
Engine (CSLR Governance Foundation, slice G2).

## The runtime invariant this module exists to establish

```
no permission -> no authority
```

and

```
model output -> NEVER user consent
```

`check()` answers "is there currently an active grant for exactly this
`(permission_key, semantic_version, resource)`" — nothing more, nothing
"close enough." `require()` builds on `check()` to give a caller a clear
failure when the answer is no. Neither method, nor anything else in this
module, ever manufactures a grant: the *only* way a grant comes into
existence is `decide()` recording a real `ALLOW` for a request a human
saw and decided on — see `docs/PERMISSIONS_MODEL.md` and
`docs/SECURITY_PRIVACY_ARCHITECTURE.md`§10 "Model authority."

## No feature queries the database directly

`store.permissions_repo.PermissionsRepo` is a thin, non-deciding
persistence primitive; every legality decision (does this definition/
version exist, is this the first decision for this request, does this
resource conform to the definition's own resource type) is made here,
once, so nothing else in the codebase re-implements — or, worse,
subtly disagrees with — this logic.

## Fail closed, exactly, always

Every one of the following makes `check()` return `False` (never raise,
never guess, never partially match): the `(permission_key,
semantic_version)` pair is not a registered definition; the `resource`
does not conform to that definition's own `resource_type`; no grant
exists for the exact triple; the matching grant has been revoked; the
matching grant has expired. `check()` is side-effect free — it never
writes to the database, and never audits (auditing every successful
check would flood the log for something that is, by design, meant to be
checked often and cheaply).
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.permissions.definitions import PERMISSION_DEFINITIONS, PermissionDefinition
from code_slayer.repo import identity
from code_slayer.store import db as db_module
from code_slayer.store import location
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.models import PermissionGrantRow, PermissionRequestRow
from code_slayer.store.permissions_repo import PermissionsRepo

_MAX_RESOURCE_LEN = 500
_MAX_PURPOSE_LEN = 2000
_MAX_SUBSYSTEM_LEN = 200


class PermissionEngineError(RuntimeError):
    """Base class for Permission Engine errors that reject a request
    before any durable row is created — an unknown definition, a
    malformed resource, or a malformed decision. Never raised as a
    consequence of `check()`, which never raises at all."""


class UnknownPermissionDefinitionError(PermissionEngineError):
    """The requested `(permission_key, semantic_version)` pair does not
    exist in the trusted, code-owned registry. Fails closed: no request
    row is ever created for an unknown definition."""


class InvalidResourceScopeError(PermissionEngineError):
    """`resource` does not conform to the named definition's own
    `resource_type` — e.g. a non-`None` resource against a `"none"`-typed
    definition. This is exactly the "ambiguous resource scope" case that
    must fail closed rather than be guessed at."""


class PermissionDeniedError(PermissionEngineError):
    """Raised by `require()` when `check()` is `False`. Carries only a
    safe, structured reason — never raw request/grant content, and
    never anything that could contain a secret."""

    def __init__(self, *, permission_key: str, semantic_version: str, resource: str | None):
        self.permission_key = permission_key
        self.semantic_version = semantic_version
        self.resource = resource
        super().__init__(
            f"permission denied: {permission_key}:{semantic_version} resource={resource!r}"
        )


@dataclass(frozen=True)
class PermissionDefinitionView:
    """The complete, JSON-serializable explanation metadata for one
    `PermissionDefinition` — exactly what the WebUI's progressive
    disclosure (simple / explanation / technical details) renders. Never
    constructed from anything but the trusted registry."""

    permission_key: str
    semantic_version: str
    action: str
    resource_type: str
    sensitivity: str
    user_title: str
    user_summary: str
    what_it_does: tuple[str, ...]
    what_it_does_not_do: tuple[str, ...]
    data_observed: tuple[str, ...]
    data_retained: tuple[str, ...]
    data_transmitted: tuple[str, ...]
    revocable: bool
    technical_details: tuple[str, ...]
    implementation_reference: str
    user_selectable_scope: bool

    @classmethod
    def from_definition(cls, definition: PermissionDefinition) -> PermissionDefinitionView:
        data = asdict(definition)
        data["sensitivity"] = definition.sensitivity.value
        return cls(**data)


@dataclass(frozen=True)
class PermissionRequestRecord:
    """A structured, HTTP/WebUI-safe view of one permission request —
    `state` is always derived from the append-only request/decision
    history, never a stored status column."""

    request_id: str
    created_at: str
    permission_key: str
    semantic_version: str
    resource: str | None
    purpose: str
    requesting_subsystem: str
    state: str  # PENDING | ALLOWED | DENIED
    decision: str | None
    decided_at: str | None
    grant_id: str | None
    definition: PermissionDefinitionView | None


@dataclass(frozen=True)
class PermissionGrantRecord:
    """A structured, HTTP/WebUI-safe view of one grant — `state` is
    always derived from the append-only grant/revocation history plus
    `expiry`, never a stored status column."""

    grant_id: str
    request_id: str
    permission_key: str
    semantic_version: str
    resource: str | None
    authority_origin: str
    granted_at: str
    expiry: str | None
    revoked_at: str | None
    state: str  # ACTIVE | REVOKED | EXPIRED
    definition: PermissionDefinitionView | None


class PermissionService:
    """One persistent service bound to exactly one primary repository —
    mirrors `planning.service.EngineeringPlanningService`'s own
    construction/connection-ownership shape. `definitions` defaults to
    the real, trusted, code-owned registry; a caller may inject a
    different mapping only for tests that need to exercise version-
    mismatch semantics without polluting the production registry (see
    `tests/unit/test_permissions.py`) — never for production use, where
    only the default (`PERMISSION_DEFINITIONS`) is ever passed."""

    def __init__(
        self, primary_repo_path: Path | str, *, state_root_override: str | Path | None = None,
        definitions: dict[tuple[str, str], PermissionDefinition] | None = None,
    ) -> None:
        self._primary = identity.resolve(primary_repo_path)
        self._state_root_override = state_root_override
        self._definitions = definitions if definitions is not None else PERMISSION_DEFINITIONS
        self._db_path = location.db_path(
            self._primary.repo_id, self._primary.worktree_id, override=state_root_override,
        )
        location.ensure_dirs(
            self._primary.repo_id, self._primary.worktree_id, override=state_root_override,
        )
        self._conn = db_module.connect(self._db_path)
        db_module.migrate(self._conn)

    def close(self) -> None:
        self._conn.close()

    def _audit(self, related_id: str | None, event_type: EventType, payload: dict) -> None:
        AuditWriter(self._conn).append(
            task_id=None, event_type=event_type, actor_type="system",
            actor_id="permission-service",
            payload={"related_id": related_id, **payload} if related_id else dict(payload),
        )

    # -- definition lookup ------------------------------------------------

    def _lookup_definition(
        self, permission_key: str, semantic_version: str,
    ) -> PermissionDefinition | None:
        if not isinstance(permission_key, str) or not isinstance(semantic_version, str):
            return None
        return self._definitions.get((permission_key, semantic_version))

    def definitions(self) -> list[PermissionDefinitionView]:
        """Every known permission definition — read-only, code-owned
        metadata. Safe to expose in full: never a secret, never derived
        from anything untrusted."""
        return [PermissionDefinitionView.from_definition(d) for d in self._definitions.values()]

    @staticmethod
    def _resource_conforms(definition: PermissionDefinition, resource: str | None) -> bool:
        if definition.resource_type == "none":
            return resource is None
        # A concrete resource_type requires a real, bounded, non-empty
        # resource string -- never None, never unbounded.
        return isinstance(resource, str) and 0 < len(resource) <= _MAX_RESOURCE_LEN

    # -- request lifecycle --------------------------------------------------

    def request(
        self, *, permission_key: str, semantic_version: str, resource: str | None, purpose: str,
        requesting_subsystem: str,
    ) -> PermissionRequestRecord:
        """Durably create a new `PENDING` permission request. Callable
        only by trusted backend code — see the module docstring. Fails
        closed (raises, creates no row) for an unknown definition or a
        resource that does not conform to the definition's own
        `resource_type`."""
        definition = self._lookup_definition(permission_key, semantic_version)
        if definition is None:
            raise UnknownPermissionDefinitionError(
                f"unknown permission definition: {permission_key}:{semantic_version}"
            )
        if not self._resource_conforms(definition, resource):
            raise InvalidResourceScopeError(
                f"resource {resource!r} does not conform to "
                f"{permission_key}:{semantic_version}'s resource_type={definition.resource_type!r}"
            )
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > _MAX_PURPOSE_LEN:
            raise ValueError("purpose must be a non-empty, bounded string")
        if (
            not isinstance(requesting_subsystem, str) or not requesting_subsystem.strip()
            or len(requesting_subsystem) > _MAX_SUBSYSTEM_LEN
        ):
            raise ValueError("requesting_subsystem must be a non-empty, bounded string")

        request_id = uuid.uuid4().hex
        now = utcnow_iso()
        with transaction(self._conn):
            PermissionsRepo(self._conn).create_request_in_transaction(
                request_id=request_id, created_at=now, repo_id=self._primary.repo_id,
                worktree_id=self._primary.worktree_id, permission_key=permission_key,
                semantic_version=semantic_version, resource=resource, purpose=purpose,
                requesting_subsystem=requesting_subsystem,
            )
            self._audit(request_id, EventType.PERMISSION_REQUESTED, {
                "permission_key": permission_key, "semantic_version": semantic_version,
                "resource": resource, "requesting_subsystem": requesting_subsystem,
            })
        return self.get_request(request_id)

    def decide(self, request_id: str, decision: str) -> PermissionRequestRecord:
        """Record the user's `ALLOW`/`DENY` decision for `request_id`,
        creating a grant iff `ALLOW`. Idempotent under a race: the
        schema's own `UNIQUE` constraint on `permission_decisions.
        request_id` means at most one decision is ever durably
        committed — a second, concurrent, or duplicate call always
        observes the same authoritative outcome, never a second
        conflicting decision or a second grant."""
        if decision not in ("ALLOW", "DENY"):
            raise ValueError("decision must be exactly 'ALLOW' or 'DENY'")
        repo = PermissionsRepo(self._conn)
        request = repo.get_request(request_id)  # KeyError if unknown -- propagates to caller
        now = utcnow_iso()
        try:
            with transaction(self._conn):
                repo.create_decision_in_transaction(
                    request_id=request_id, decision=decision, decided_at=now,
                )
                if decision == "ALLOW":
                    repo.create_grant_in_transaction(
                        grant_id=uuid.uuid4().hex, request_id=request_id,
                        permission_key=request.permission_key,
                        semantic_version=request.semantic_version, resource=request.resource,
                        authority_origin="USER_EXPLICIT", granted_at=now, expiry=None,
                    )
                event = (
                    EventType.PERMISSION_ALLOWED if decision == "ALLOW"
                    else EventType.PERMISSION_DENIED
                )
                self._audit(request_id, event, {
                    "permission_key": request.permission_key,
                    "semantic_version": request.semantic_version,
                })
        except sqlite3.IntegrityError:
            # Already decided (by this call racing another, or a genuine
            # duplicate call) -- never a second decision/grant; fall
            # through to read back the one authoritative outcome.
            pass
        return self.get_request(request_id)

    def pending(self, *, limit: int = 50, offset: int = 0) -> list[PermissionRequestRecord]:
        return [
            r for r in self.list_requests(limit=limit, offset=offset) if r.state == "PENDING"
        ]

    def list_requests(self, *, limit: int = 50, offset: int = 0) -> list[PermissionRequestRecord]:
        rows = PermissionsRepo(self._conn).list_requests_for_scope(
            self._primary.repo_id, self._primary.worktree_id, limit=limit, offset=offset,
        )
        return [self._request_record(row) for row in rows]

    def get_request(self, request_id: str) -> PermissionRequestRecord:
        row = PermissionsRepo(self._conn).get_request(request_id)
        return self._request_record(row)

    def _request_record(self, row: PermissionRequestRow) -> PermissionRequestRecord:
        repo = PermissionsRepo(self._conn)
        decision_row = repo.get_decision_for_request(row.request_id)
        grant_row = repo.get_grant_for_request(row.request_id)
        if decision_row is None:
            state = "PENDING"
        elif decision_row.decision == "ALLOW":
            state = "ALLOWED"
        else:
            state = "DENIED"
        definition = self._lookup_definition(row.permission_key, row.semantic_version)
        return PermissionRequestRecord(
            request_id=row.request_id, created_at=row.created_at,
            permission_key=row.permission_key, semantic_version=row.semantic_version,
            resource=row.resource, purpose=row.purpose,
            requesting_subsystem=row.requesting_subsystem, state=state,
            decision=decision_row.decision if decision_row else None,
            decided_at=decision_row.decided_at if decision_row else None,
            grant_id=grant_row.grant_id if grant_row else None,
            definition=(
                PermissionDefinitionView.from_definition(definition) if definition else None
            ),
        )

    # -- grants / revocation ----------------------------------------------

    def grants(self, *, limit: int = 50, offset: int = 0) -> list[PermissionGrantRecord]:
        rows = PermissionsRepo(self._conn).list_grants_for_scope(
            self._primary.repo_id, self._primary.worktree_id, limit=limit, offset=offset,
        )
        return [self._grant_record(row) for row in rows]

    def get_grant(self, grant_id: str) -> PermissionGrantRecord:
        row = PermissionsRepo(self._conn).get_grant(grant_id)
        return self._grant_record(row)

    def _grant_record(self, row: PermissionGrantRow) -> PermissionGrantRecord:
        repo = PermissionsRepo(self._conn)
        revocation = repo.get_revocation_for_grant(row.grant_id)
        now = utcnow_iso()
        if revocation is not None:
            state = "REVOKED"
        elif row.expiry is not None and row.expiry <= now:
            state = "EXPIRED"
        else:
            state = "ACTIVE"
        definition = self._lookup_definition(row.permission_key, row.semantic_version)
        return PermissionGrantRecord(
            grant_id=row.grant_id, request_id=row.request_id, permission_key=row.permission_key,
            semantic_version=row.semantic_version, resource=row.resource,
            authority_origin=row.authority_origin, granted_at=row.granted_at,
            expiry=row.expiry, revoked_at=revocation.revoked_at if revocation else None,
            state=state,
            definition=(
                PermissionDefinitionView.from_definition(definition) if definition else None
            ),
        )

    def revoke(self, grant_id: str) -> PermissionGrantRecord:
        """Durably revoke `grant_id`. Idempotent: revoking an
        already-revoked grant is a safe no-op (the schema's `UNIQUE`
        constraint on `permission_revocations.grant_id` prevents a
        second row; this call simply returns the already-revoked
        state). Never erases the grant or its history — see
        `docs/SECURITY_PRIVACY_ARCHITECTURE.md`§8 "Revocation"."""
        repo = PermissionsRepo(self._conn)
        grant = repo.get_grant(grant_id)  # KeyError if unknown
        now = utcnow_iso()
        try:
            with transaction(self._conn):
                repo.create_revocation_in_transaction(grant_id=grant_id, revoked_at=now)
                self._audit(grant.request_id, EventType.PERMISSION_REVOKED, {
                    "grant_id": grant_id, "permission_key": grant.permission_key,
                    "semantic_version": grant.semantic_version,
                })
        except sqlite3.IntegrityError:
            pass  # already revoked
        return self.get_grant(grant_id)

    # -- authorization checks -----------------------------------------------

    def check(
        self, *, permission_key: str, semantic_version: str, resource: str | None = None,
    ) -> bool:
        """Side-effect free. `True` only if an exact, currently-active,
        non-expired grant exists for `(permission_key, semantic_version,
        resource)`. Never raises; never writes; never audits. Fails
        closed on every ambiguous/unknown/mismatched input — see the
        module docstring."""
        definition = self._lookup_definition(permission_key, semantic_version)
        if definition is None:
            return False
        if not self._resource_conforms(definition, resource):
            return False
        grant = PermissionsRepo(self._conn).find_active_grant(
            permission_key, semantic_version, resource,
        )
        if grant is None:
            return False
        revoked = PermissionsRepo(self._conn).get_revocation_for_grant(grant.grant_id)
        if revoked is not None:
            return False
        if grant.expiry is not None and grant.expiry <= utcnow_iso():
            return False
        return True

    def require(
        self, *, permission_key: str, semantic_version: str, resource: str | None = None,
        subsystem: str,
    ) -> None:
        """Raises `PermissionDeniedError` if `check()` is `False`.
        Audits only the denial (a security-relevant event) — never the
        success case, which would flood the audit log for something
        meant to be checked cheaply and often."""
        if self.check(
            permission_key=permission_key, semantic_version=semantic_version, resource=resource,
        ):
            return
        self._audit(None, EventType.PERMISSION_CHECK_DENIED, {
            "permission_key": permission_key, "semantic_version": semantic_version,
            "resource": resource, "subsystem": subsystem,
        })
        raise PermissionDeniedError(
            permission_key=permission_key, semantic_version=semantic_version, resource=resource,
        )
