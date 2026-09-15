"""The CSLR Permission Engine (Governance Foundation, slice G2 —
`docs/PERMISSIONS_MODEL.md`).

The runtime invariant this package establishes: `no permission -> no
authority`, and `model output -> NEVER user consent`. See
`permissions.service.PermissionService` for the public entry point every
other component should use — nothing outside this package (and
`store.permissions_repo`, its thin persistence primitive) ever queries
`permission_requests`/`permission_decisions`/`permission_grants`/
`permission_revocations` directly, and nothing outside
`permissions.definitions` ever constructs a `PermissionDefinition`.

This package implements the Permission Engine itself — it does not
implement network discovery, NAS integration, AI-server discovery,
model downloading, or any other feature that will eventually *use* it.
See `docs/PERMISSIONS_MODEL.md` and `docs/SECURITY_PRIVACY_ARCHITECTURE.md`
for the full normative specification this package is the first real,
machine-enforced piece of.
"""

from code_slayer.permissions.definitions import (
    PERMISSION_DEFINITIONS,
    AuthorityOrigin,
    PermissionDefinition,
    Sensitivity,
)
from code_slayer.permissions.service import (
    InvalidResourceScopeError,
    PermissionDefinitionView,
    PermissionDeniedError,
    PermissionEngineError,
    PermissionGrantRecord,
    PermissionRequestRecord,
    PermissionService,
    UnknownPermissionDefinitionError,
)

__all__ = [
    "PERMISSION_DEFINITIONS",
    "AuthorityOrigin",
    "InvalidResourceScopeError",
    "PermissionDefinition",
    "PermissionDefinitionView",
    "PermissionDeniedError",
    "PermissionEngineError",
    "PermissionGrantRecord",
    "PermissionRequestRecord",
    "PermissionService",
    "Sensitivity",
    "UnknownPermissionDefinitionError",
]
