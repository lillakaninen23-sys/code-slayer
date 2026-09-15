"""The server-owned permission-definition registry (CSLR Governance
Foundation, slice G2 — `docs/PERMISSIONS_MODEL.md`).

## Definitions are trusted CSLR code, never untrusted input

Every `PermissionDefinition` below is a Python constant, defined in this
module and nowhere else. `PERMISSION_DEFINITIONS` is a read-only mapping
(`types.MappingProxyType`) keyed by the exact `(permission_key,
semantic_version)` pair a caller must name — mirroring
`tools.registry.CAPABILITIES`'s own "the closed, code-owned set of things
that may ever be offered" pattern. Definitions MUST NOT, and structurally
cannot, come from an HTTP client, model output, planner output,
repository content, or arbitrary caller-supplied configuration: nothing
in `permissions.service.PermissionService` ever constructs a
`PermissionDefinition` from anything other than a lookup into this
module's own registry.

## Versioned meaning

A permission's semantic meaning is versioned
(`docs/PERMISSIONS_MODEL.md`§4): `network.discovery.local` version `"1"`
means exactly what its fields below say, forever. If a future need
requires broader behavior, that is a *new* semantic version (or a new
permission key) registered here — never a mutation of what version
`"1"` already means. `PermissionService` matches grants by the exact
`(permission_key, semantic_version)` pair; a grant recorded against
version `"1"` never satisfies a check against version `"2"`, even for
the same `permission_key` (see `tests/unit/test_permissions.py`).

## This phase registers metadata only — it implements no discovery

`network.discovery.local` is registered here so the Permission Engine
and the consent UX have a real, canonical, security-sensitive permission
to model end to end. **Registering this definition does not implement
network discovery.** Nothing in this module, or in `permissions.service`,
opens a socket, sends an mDNS probe, performs an ARP scan, or makes any
network request of any kind — see `tests/unit/test_permissions.py`'s own
no-network regression test. A future discovery subsystem, once actually
built, is the thing this definition's `what_it_does`/`what_it_does_not_do`
fields describe; until then, this is authority semantics and consent
copy only.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class AuthorityOrigin(StrEnum):
    """Where a grant's authority actually came from
    (`docs/PERMISSIONS_MODEL.md`§1 `origin_of_authority`). In this phase,
    `USER_EXPLICIT` is the *only* valid origin — an explicit human ALLOW
    decision, made through `PermissionService.decide()`. There is
    deliberately no `MODEL`/`PLANNER`/`WORKER` member: those are never
    valid authority origins, by construction, not merely by convention —
    see `docs/SECURITY_PRIVACY_ARCHITECTURE.md`§10 "Model authority" and
    `tests/unit/test_permissions.py`'s model-boundary tests. Future
    authority sources (e.g. a signed administrator policy) may be added
    later; none is invented here ahead of an actual need."""

    USER_EXPLICIT = "USER_EXPLICIT"


class Sensitivity(StrEnum):
    """How security/privacy-sensitive a permission definition is — used
    only to inform UI presentation (e.g. which permissions default to a
    more prominent consent flow); it never itself grants or denies
    anything."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class PermissionDefinition:
    """One immutable, code-owned permission definition
    (`docs/PERMISSIONS_MODEL.md`§1). `resource_type` names what kind of
    `resource` string a request/grant against this definition may
    legitimately carry: `"none"` means this definition is not scoped to
    any specific resource at all (a request/grant's `resource` field
    MUST be `None` — a non-`None` resource against a `"none"`-typed
    definition is exactly the "ambiguous resource scope" case that fails
    closed). A future resource-scoped definition (e.g. one specific NAS
    share) would use a concrete `resource_type` and require a real
    resource string. `user_selectable_scope=False` means the user may
    only ALLOW or DENY exactly the resource that was requested — no
    scope-widening choice is offered at decision time (see
    `docs/PERMISSIONS_MODEL.md`§6 and this phase's own decision-endpoint
    contract, which currently accepts no scope field at all)."""

    permission_key: str
    semantic_version: str
    action: str
    resource_type: str
    sensitivity: Sensitivity
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
    user_selectable_scope: bool = False


_NETWORK_DISCOVERY_LOCAL_V1 = PermissionDefinition(
    permission_key="network.discovery.local",
    semantic_version="1",
    action="discover",
    resource_type="none",
    sensitivity=Sensitivity.HIGH,
    user_title="Look for compatible devices on your local network",
    user_summary=(
        "CSLR may search the local network for compatible devices/services."
    ),
    what_it_does=(
        "Searches the local network for devices/services CSLR could work with "
        "in the future (for example, a storage device or a local AI server).",
    ),
    what_it_does_not_do=(
        "Does not connect to anything it finds.",
        "Does not authenticate to anything it finds.",
        "Does not read files from anything it finds.",
        "Does not write files to anything it finds.",
        "Does not configure any discovered device.",
        "Does not send anything to an AI model or server.",
        "Does not download any model.",
        "Does not grant general internet access.",
    ),
    data_observed=(
        "Advertised device/service metadata on the local network (for "
        "example, a hostname or advertised service type) — never file "
        "contents, credentials, or repository data.",
    ),
    data_retained=(
        "A list of what was found and when, stored locally on this machine "
        "only.",
    ),
    data_transmitted=(
        "Nothing leaves the local network as a result of this permission "
        "alone.",
    ),
    revocable=True,
    technical_details=(
        "No discovery mechanism is implemented yet in this version of CSLR "
        "— this permission's technical behavior is not yet defined beyond "
        "the guarantees above. This section will describe the actual "
        "protocol(s) used once a discovery implementation exists.",
    ),
    implementation_reference="src/code_slayer/permissions/definitions.py",
)

# The complete, closed set of permission definitions this version of CSLR
# knows about — keyed by the exact (permission_key, semantic_version)
# pair a request/check must name. Adding a definition is a deliberate
# code change (a new entry here), never something a request, a model, or
# a config file can do at runtime.
PERMISSION_DEFINITIONS: MappingProxyType[tuple[str, str], PermissionDefinition] = (
    MappingProxyType({
        (_NETWORK_DISCOVERY_LOCAL_V1.permission_key, _NETWORK_DISCOVERY_LOCAL_V1.semantic_version):
            _NETWORK_DISCOVERY_LOCAL_V1,
    })
)
