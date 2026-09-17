"""Baseline Security Certification: the mandatory, role-independent
security gate every worker/model must hold before it may participate in
any production-capable role (Baseline Security Certification
foundation).

## What this module is, and is not

This module owns the *representation and recording* of one Baseline
Security Certificate — a durable, provenance-bearing evaluation result
bound to a specific worker and the specific runtime/model/provider
configuration ("runtime profile") that was actually evaluated. It is
deliberately NOT:

- **A benchmark suite that actually probes a live model for unsafe
  behavior** (tool/scope adherence, permission boundary adherence,
  secrets handling, unauthorized network behavior, destructive behavior,
  fabricated authority/trust, policy/gate bypass attempts, unsafe
  dependency/tool behavior, refusal to obey execution constraints).
  That evaluation harness now lives in `code_slayer.security.evaluation`
  (`run_baseline_security_evaluation`). Production live issuance —
  verifying a configured Ollama runtime, running that harness through a
  bound `SecurityEvaluationAdapter`, rereading durable evidence, and
  only then recording a certificate — lives in
  `code_slayer.security.live_certification`. This module remains the
  low-level durable recorder: `record_baseline_certificate()` accepts an
  already-derived `SecurityBaselineOutcome` plus its evidence reference
  and never talks to a model. A caller must not treat this recorder as
  the production live-certification boundary.

- **Worker Trust** (`workers.trust`). `record_baseline_certificate()`
  never grants, denies, or otherwise touches any `worker_trust_events`
  row, and a certificate never automatically promotes or downgrades
  trust. See `workers.production_eligibility` for how a certificate and
  trust/role-qualification eventually combine into one eligibility
  decision — still without any of the three mutating each other.
- **Role qualification** (Planner/Coder/Reviewer/Repairer/Security-role).
  A Baseline Security Certificate says nothing about whether a worker is
  *good* at any particular role, only whether it has cleared the
  mandatory, role-independent safety floor every production-capable role
  requires *in addition to* its own role-specific qualification. Passing
  this baseline for one worker never implies anything about any role
  qualification, transitively or otherwise.
- **The dedicated Security review role.** A future role in which a
  Security-role-certified worker acts as an independent reviewer is a
  completely separate concept from this mandatory baseline every
  production-capable worker/model needs — see `docs/CODE_SLAYER_VISION.md`
  §47 "Independent review" for that (unrelated, not-yet-built) role.

## Identity and runtime-profile binding

A certificate binds to the existing canonical worker identity
(`worker_id`, `store.workers_repo.WorkersRepo` — the same identity
`workers.trust`/`workers.conformance` already use) plus an explicit
`RuntimeProfileIdentity`: `model_tag` (required) and optional
`model_digest`/`endpoint`/`runtime_version` plus the optional
compatibility-normalizer identity (`normalizer_id`/`normalizer_version`,
both `None` meaning native-only transport) and the optional
`runtime_config_fingerprint` (SHA-256 of the canonical
`runtime-config-spec-v1` document) — deliberately the same identity
vocabulary `planning.qualification.RuntimeContextProfile` already
established for "what runtime was actually verified," reused here
rather than inventing a second, differently-named identity shape
(`docs/CODE_SLAYER_VISION.md` §42/§43's own "model-specific, provider-
specific, runtime-specific, version-specific" scoping intent).

`worker_id` alone is not enough: nothing in this codebase's `workers`
table durably records which concrete model/endpoint a `worker_id`
currently points at — an operator is free to repoint the same
`worker_id` string at a different runtime entirely (see `workers.
trust`'s own module docstring: "`worker_id` is trusted to identify one
concrete configured worker for now"). Binding a certificate's own
profile fields, and requiring `workers.production_eligibility` to match
them exactly against the CURRENT profile before ever trusting a
certificate, is what stops a certificate issued against one runtime from
silently covering a different one now answering to the same `worker_id`.
The normalizer identity is part of that exact match: a certificate for a
native-only runtime must never silently authorize a runtime using a
compatibility normalizer, and vice versa.

The production exact-match identity is the SHA-256 of the canonical
`runtime-identity-spec-v2` document (`canonical_runtime_identity_spec` /
`runtime_profile_identity_from_config`). That document is the COMMON,
role-independent runtime identity shared by Baseline Security and every
production role: spec version, model tag/digest, endpoint, runtime
version, compatibility-normalizer pair, effective context capacity, and
sampling temperature (when it is fixed adapter/runtime configuration).
It deliberately does NOT include role/request-specific fields such as
`output_token_budget`, `tool_choice_enforcement`, qualification class,
retry count, qualification policy, or security-case configuration —
those belong on a separate role/evaluation identity
(`workers.role_qualification.RoleEvaluationIdentity`) so a Baseline
Security certificate and a Planner certificate can name the SAME
underlying runtime while retaining independent evaluation configuration.

`runtime-config-spec-v1` / `runtime_config_fingerprint` (schema v14) is
historical evidence of the identity space that produced it. This module
never silently reinterprets a v1 hash under v2 semantics: fingerprinting
a v1 document still requires the v1 spec, fingerprinting a v2 document
requires the v2 spec, and a v1 hash never equals a v2 hash of "the same"
runtime. Existing certificate rows are never rewritten.

`model_digest`/`endpoint`/`runtime_version` remain optional here
deliberately — `record_baseline_certificate()` still accepts a
certificate bound only to `model_tag`, since that is sometimes the only
fact genuinely available at *evaluation* time, and this module's job is
recording evidence honestly, not demanding more identity than was
actually established. `normalizer_id`/`normalizer_version` are optional
in the same honest-recording sense (`None`/`None` is a complete, exact
statement of "native-only," not a wildcard). `runtime_identity_fingerprint`
is optional in that same sense (`None` means the common runtime identity
was not established — a complete statement of absence, not a wildcard,
and not an invitation to treat a pre-v15 row as covering a later fully-
specified runtime). `runtime_config_fingerprint` remains the historical
v1 component and is likewise never a wildcard.
`RuntimeProfileIdentity.is_fully_specified` exists for the separate,
stricter question a *production* decision must ask: `workers.
production_eligibility` refuses to treat ANY certificate as
authoritative for a real eligibility decision unless the CURRENT
profile it is asked to check against has every *model/runtime* field
populated AND a v2 runtime-identity fingerprint — a loosely-specified
profile (e.g. `model_tag` alone, or tag/digest/endpoint/runtime
without a v2 fingerprint) could otherwise let two meaningfully different
runtimes silently share a certificate. Native-only (`normalizer_id is
None`) is fully specified with respect to the compatibility layer;
enabling a normalizer is a different identity, never a missing one.
This keeps recording lenient/honest and production consultation strict,
without changing what this module itself accepts. The production-grade
constructor is `runtime_profile_identity_from_config()`:
it derives the v2 fingerprint from caller-verified configuration, never
from a model response, and never as a caller-supplied hash. The returned
object retains `effective_context_tokens` and `temperature` so the
fingerprint stays recomputable from the object's own fields
(`is_verified_current`). A persisted certificate binding reconstructed
from stored columns (`runtime_profile_binding_from_stored`) carries only
the stored fingerprint and must never masquerade as that current
identity: production eligibility requires the *current* runtime side to
be verified from concrete configuration, and `record_baseline_certificate()`
refuses to persist a v2 fingerprint that was not derived that way.

## No fabrication, ever

A certificate is never a bare boolean. `evidence_ref` is REQUIRED on
every certificate, `PASS`/`FAIL`/`HARD_DISQUALIFIED` alike — an
assertion with no referenceable evidence is refused before it can ever
be persisted (see `record_baseline_certificate()`). A hard disqualifier
can never be silently downgraded to `PASS`: `outcome=HARD_DISQUALIFIED`
requires at least one `HardDisqualifierCategory` member in
`hard_disqualifiers`, and every other outcome requires an empty list —
an inconsistent combination is rejected outright, never coerced into
whichever field looks more convenient.

## Append-only, like every other evidence table in this codebase

`worker_baseline_security_certificates` is fully append-only — a
re-evaluation, including one that reverses an earlier verdict, always
creates a NEW row. There is no in-place "invalidate" transition
implemented by this revision (a disclosed gap, not an oversight); the
"no silent stale reuse" requirement is instead satisfied entirely by
`workers.production_eligibility` always consulting the *most recent*
certificate matching the *current* runtime profile — an older,
superseded certificate for the same profile is simply never the one
selected, and a certificate for a different profile is never selected
at all.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from enum import StrEnum

from code_slayer.audit.canonical import canonical_json
from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.models import WorkerBaselineSecurityCertificate
from code_slayer.store.workers_repo import WorkersRepo

# The fixed, code-owned identifier of the baseline security check
# taxonomy/policy this module currently implements -- mirrors
# `workers.conformance.SUITE_VERSION`'s own role: a persisted
# `baseline_version` identifies the exact evaluation semantics a
# certificate was checked against, so a later change to what "baseline
# security" even means never gets silently applied to old evidence.
BASELINE_VERSION = "baseline-security-v1"

# Versioned canonical document of historical qualification-relevant
# runtime / inference configuration (schema v14). Bumping this
# identifier is a new identity space: fingerprints computed under a
# later spec never match v1, so old certificates cannot silently cover
# a changed spec. v1 hashes are NEVER reinterpreted as v2 common-runtime
# identity -- `runtime-identity-spec-v2` is a separate document.
RUNTIME_CONFIG_SPEC_VERSION = "runtime-config-spec-v1"

# Versioned canonical document of the COMMON, role-independent runtime
# identity shared by Baseline Security and every production role.
# Role/request-specific fields (output_token_budget,
# tool_choice_enforcement, qualification policy, ...) live on
# `role-evaluation-spec-v1` instead.
RUNTIME_IDENTITY_SPEC_VERSION = "runtime-identity-spec-v2"

_FINGERPRINT_HEX_LENGTH = 64
_SHA256_HEX_CHARS = "0123456789abcdef"


def require_sha256_hex(name: str, value: object) -> str:
    """Exact-match identity component: a 64-char lowercase SHA-256 hex
    digest, never a wildcard, never a caller-invented token."""
    if (
        not isinstance(value, str)
        or len(value) != _FINGERPRINT_HEX_LENGTH
        or any(ch not in _SHA256_HEX_CHARS for ch in value)
    ):
        raise ValueError(f"{name} must be a 64-char lowercase sha256 hex digest")
    return value


def _canonical_temperature(value: object) -> float:
    """Coerce a numeric temperature to float. `bool` is rejected (it is
    an `int` subclass). Range matches `OpenAICompatibleConfig`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("temperature must be a number")
    temperature = float(value)
    if not (0.0 <= temperature <= 2.0):
        raise ValueError("temperature must be between 0.0 and 2.0")
    return temperature


def _non_negative_int(name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validated_runtime_identity_fields(
    *,
    model_tag: str,
    model_digest: str | None,
    endpoint: str | None,
    runtime_version: str | None,
    normalizer_id: str | None,
    normalizer_version: int | None,
) -> dict:
    if not isinstance(model_tag, str) or not model_tag.strip():
        raise ValueError("model_tag must be a non-empty string")
    for name, value in (
        ("model_digest", model_digest),
        ("endpoint", endpoint),
        ("runtime_version", runtime_version),
        ("normalizer_id", normalizer_id),
    ):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise ValueError(f"{name} must be a non-empty string or None")
    if (normalizer_id is None) != (normalizer_version is None):
        raise ValueError(
            "normalizer_id and normalizer_version must both be set or both be None",
        )
    if normalizer_version is not None and (
        not isinstance(normalizer_version, int)
        or isinstance(normalizer_version, bool)
        or normalizer_version < 1
    ):
        raise ValueError("normalizer_version must be a positive integer or None")
    return {
        "model_tag": model_tag,
        "model_digest": model_digest,
        "endpoint": endpoint,
        "runtime_version": runtime_version,
        "normalizer_id": normalizer_id,
        "normalizer_version": normalizer_version,
    }


def canonical_runtime_config_spec(
    *,
    model_tag: str,
    model_digest: str | None,
    endpoint: str | None,
    runtime_version: str | None,
    normalizer_id: str | None,
    normalizer_version: int | None,
    effective_context_tokens: int,
    output_token_budget: int,
    temperature: float,
    tool_choice_enforcement: str,
) -> dict:
    """Historical canonical `runtime-config-spec-v1` document.

    Kept so existing v1 evidence and v1 certificate fingerprints remain
    independently verifiable under the semantics that produced them.
    This is NOT the production identity of a current runtime: production
    uses `canonical_runtime_identity_spec` (v2, common runtime) plus
    `workers.role_qualification.canonical_role_evaluation_spec` (role/
    evaluation). A v1 hash is never treated as a v2 hash.

    `temperature` is required here (not `None`): omitting it from a
    request is a different, weaker statement than sending an explicit
    `0.0`, and a production identity must not leave that ambiguous."""
    identity = _validated_runtime_identity_fields(
        model_tag=model_tag,
        model_digest=model_digest,
        endpoint=endpoint,
        runtime_version=runtime_version,
        normalizer_id=normalizer_id,
        normalizer_version=normalizer_version,
    )
    if not isinstance(tool_choice_enforcement, str) or not tool_choice_enforcement.strip():
        raise ValueError("tool_choice_enforcement must be a non-empty string")
    return {
        "spec_version": RUNTIME_CONFIG_SPEC_VERSION,
        **identity,
        "effective_context_tokens": _non_negative_int(
            "effective_context_tokens",
            effective_context_tokens,
        ),
        "output_token_budget": _non_negative_int("output_token_budget", output_token_budget),
        "temperature": _canonical_temperature(temperature),
        "tool_choice_enforcement": tool_choice_enforcement,
    }


def fingerprint_runtime_config(spec: dict) -> str:
    """SHA-256 of `canonical_json(spec)` for a v1 document only.
    Exact-match historical identity: never a wildcard, never a v2
    document silently accepted as v1."""
    if not isinstance(spec, dict):
        raise TypeError("runtime config spec must be a dict")
    if spec.get("spec_version") != RUNTIME_CONFIG_SPEC_VERSION:
        raise ValueError("runtime config spec_version is missing or unsupported")
    digest = hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()
    return require_sha256_hex("runtime_config_fingerprint", digest)


def canonical_runtime_identity_spec(
    *,
    model_tag: str,
    model_digest: str | None,
    endpoint: str | None,
    runtime_version: str | None,
    normalizer_id: str | None,
    normalizer_version: int | None,
    effective_context_tokens: int,
    temperature: float,
) -> dict:
    """The one canonical, versioned COMMON runtime-identity document a
    production Baseline Security or role certificate binds to. Callers
    never supply this as free-form JSON from a model; every field is a
    caller-verified value.

    Role/request-specific fields (`output_token_budget`,
    `tool_choice_enforcement`, qualification policy, retry count,
    qualification class, security-case configuration) are deliberately
    omitted: they belong on the role/evaluation identity. Timeout and
    similar operational-only adapter settings are omitted too.

    `temperature` is required here (not `None`): omitting it from a
    request is a different, weaker statement than sending an explicit
    `0.0`, and a production identity must not leave that ambiguous."""
    identity = _validated_runtime_identity_fields(
        model_tag=model_tag,
        model_digest=model_digest,
        endpoint=endpoint,
        runtime_version=runtime_version,
        normalizer_id=normalizer_id,
        normalizer_version=normalizer_version,
    )
    return {
        "spec_version": RUNTIME_IDENTITY_SPEC_VERSION,
        **identity,
        "effective_context_tokens": _non_negative_int(
            "effective_context_tokens",
            effective_context_tokens,
        ),
        "temperature": _canonical_temperature(temperature),
    }


def fingerprint_runtime_identity(spec: dict) -> str:
    """SHA-256 of `canonical_json(spec)` for a v2 common-runtime document
    only. A v1 `runtime-config-spec-v1` document is refused: v1 hashes
    are never reinterpreted under v2 semantics."""
    if not isinstance(spec, dict):
        raise TypeError("runtime identity spec must be a dict")
    if spec.get("spec_version") != RUNTIME_IDENTITY_SPEC_VERSION:
        raise ValueError("runtime identity spec_version is missing or unsupported")
    digest = hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()
    return require_sha256_hex("runtime_identity_fingerprint", digest)


def runtime_profile_identity_from_config(
    *,
    model_tag: str,
    model_digest: str | None,
    endpoint: str | None,
    runtime_version: str | None,
    effective_context_tokens: int,
    temperature: float,
    normalizer_id: str | None = None,
    normalizer_version: int | None = None,
) -> RuntimeProfileIdentity:
    """The one production-grade constructor for a CURRENT, verified
    COMMON runtime identity: the v2 fingerprint is derived from the
    canonical spec of caller-verified configuration, never supplied
    independently, and never read from a model response.
    Role/request-specific fields are not parameters: they cannot be
    smuggled into the shared runtime identity. The returned object
    retains `effective_context_tokens` and `temperature` so the
    fingerprint stays recomputable from its own fields."""
    spec = canonical_runtime_identity_spec(
        model_tag=model_tag,
        model_digest=model_digest,
        endpoint=endpoint,
        runtime_version=runtime_version,
        normalizer_id=normalizer_id,
        normalizer_version=normalizer_version,
        effective_context_tokens=effective_context_tokens,
        temperature=temperature,
    )
    return RuntimeProfileIdentity(
        model_tag=model_tag,
        model_digest=model_digest,
        endpoint=endpoint,
        runtime_version=runtime_version,
        normalizer_id=normalizer_id,
        normalizer_version=normalizer_version,
        runtime_identity_fingerprint=fingerprint_runtime_identity(spec),
        effective_context_tokens=spec["effective_context_tokens"],
        temperature=spec["temperature"],
    )


def runtime_profile_binding_from_stored(
    *,
    model_tag: str,
    model_digest: str | None,
    endpoint: str | None,
    runtime_version: str | None,
    normalizer_id: str | None = None,
    normalizer_version: int | None = None,
    runtime_config_fingerprint: str | None = None,
    runtime_identity_fingerprint: str | None = None,
) -> RuntimeProfileIdentity:
    """Reconstruct a persisted certificate binding from stored columns.

    Concrete inference configuration is intentionally absent: a stored
    fingerprint is historical evidence of what was recorded, not a
    freshly verified current-runtime identity. `is_verified_current` is
    therefore False, and this object cannot masquerade as one produced
    by `runtime_profile_identity_from_config`. Matching against a
    current identity still uses `.matches()` on the stored fields."""
    return RuntimeProfileIdentity(
        model_tag=model_tag,
        model_digest=model_digest,
        endpoint=endpoint,
        runtime_version=runtime_version,
        normalizer_id=normalizer_id,
        normalizer_version=normalizer_version,
        runtime_config_fingerprint=runtime_config_fingerprint,
        runtime_identity_fingerprint=runtime_identity_fingerprint,
    )


class SecurityBaselineOutcome(StrEnum):
    """The complete, fixed outcome vocabulary one Baseline Security
    evaluation can produce — code-owned, never a value a caller invents
    ad hoc. `PASS`/`FAIL` are the ordinary binary result; `HARD_
    DISQUALIFIED` is a strictly stronger, separately distinguishable
    verdict for the specific category of failure that must never be
    outweighed by any role-qualification score (see `workers.
    production_eligibility`'s "hard disqualifiers always win" rule) —
    kept distinct from plain `FAIL` purely for audit/provenance clarity,
    since both already deny production eligibility identically."""

    PASS = "PASS"
    FAIL = "FAIL"
    HARD_DISQUALIFIED = "HARD_DISQUALIFIED"


class HardDisqualifierCategory(StrEnum):
    """The fixed, code-owned vocabulary of hard Security disqualifier
    categories — never a free-form string a caller can invent, so a
    disqualifier's meaning can never silently drift between callers or
    over time. Each member names one class of behavior serious enough
    that no role-qualification result may ever compensate for it (see
    the module docstring)."""

    TOOL_SCOPE_VIOLATION = "TOOL_SCOPE_VIOLATION"
    PERMISSION_BOUNDARY_VIOLATION = "PERMISSION_BOUNDARY_VIOLATION"
    SECRETS_HANDLING_VIOLATION = "SECRETS_HANDLING_VIOLATION"
    UNAUTHORIZED_NETWORK_BEHAVIOR = "UNAUTHORIZED_NETWORK_BEHAVIOR"
    DESTRUCTIVE_BEHAVIOR = "DESTRUCTIVE_BEHAVIOR"
    FABRICATED_AUTHORITY_OR_TRUST = "FABRICATED_AUTHORITY_OR_TRUST"
    POLICY_OR_GATE_BYPASS_ATTEMPT = "POLICY_OR_GATE_BYPASS_ATTEMPT"
    UNSAFE_DEPENDENCY_OR_TOOL_BEHAVIOR = "UNSAFE_DEPENDENCY_OR_TOOL_BEHAVIOR"
    EXECUTION_CONSTRAINT_REFUSAL = "EXECUTION_CONSTRAINT_REFUSAL"


@dataclass(frozen=True)
class RuntimeProfileIdentity:
    """The exact COMMON runtime/model/provider configuration one Baseline
    Security evaluation was actually run against — deliberately the same
    identity vocabulary `planning.qualification.RuntimeContextProfile`
    already established (`model_tag`/`model_digest`/`endpoint`/
    `runtime_version` plus the compatibility-normalizer identity),
    reused here rather than invented afresh. A caller-*verified* value,
    never introspected or assumed by this module itself (same posture
    `RuntimeContextProfile` already takes).

    `normalizer_id`/`normalizer_version` are security-relevant runtime
    configuration, not per-turn behavior: both `None` means native-only
    transport; both set names the exact `workers.protocol_normalization`
    decoder this runtime is configured to use. An incomplete pair is
    refused at construction — never treated as a wildcard, and never
    inferred from a model response.

    `runtime_identity_fingerprint` is the SHA-256 of the canonical
    `runtime-identity-spec-v2` document (`canonical_runtime_identity_spec`
    / `runtime_profile_identity_from_config`). It is the generic,
    versioned COMMON runtime identity (effective context capacity,
    sampling temperature, plus the identity fields above) without
    role/request-specific fields. `None` means that component was not
    established — a complete statement of absence, not a wildcard.

    A production decision (`is_fully_specified`) requires a *verified
    current* identity: the object must carry the concrete
    `effective_context_tokens` and `temperature` the fingerprint was
    derived from. Direct construction with an arbitrary SHA-256 is a
    persisted binding, never a current runtime. `record_baseline_
    certificate()` will not persist that hash as a v2 identity.

    `effective_context_tokens` / `temperature` being both set means this
    object is a CURRENT runtime identity and its fingerprint is
    recomputed from those fields at construction (mismatch fails). Both
    being `None` means this is a persisted certificate binding or a
    historical/incomplete recording. A mixed pair is refused.

    `runtime_config_fingerprint` is the historical SHA-256 of a
    `runtime-config-spec-v1` document. It is NEVER used as the current
    production identity and is NEVER reinterpreted as a v2 hash. `None`
    is a complete statement of absence of that historical component,
    not a wildcard."""

    model_tag: str
    model_digest: str | None = None
    endpoint: str | None = None
    runtime_version: str | None = None
    normalizer_id: str | None = None
    normalizer_version: int | None = None
    runtime_config_fingerprint: str | None = None
    runtime_identity_fingerprint: str | None = None
    effective_context_tokens: int | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model_tag, str) or not self.model_tag.strip():
            raise ValueError("model_tag must be a non-empty string")
        for name in ("model_digest", "endpoint", "runtime_version", "normalizer_id"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")
        if (self.normalizer_id is None) != (self.normalizer_version is None):
            raise ValueError(
                "normalizer_id and normalizer_version must both be set or both be None",
            )
        if self.normalizer_version is not None and (
            not isinstance(self.normalizer_version, int)
            or isinstance(self.normalizer_version, bool)
            or self.normalizer_version < 1
        ):
            raise ValueError("normalizer_version must be a positive integer or None")
        if self.runtime_config_fingerprint is not None:
            require_sha256_hex(
                "runtime_config_fingerprint",
                self.runtime_config_fingerprint,
            )
        if self.runtime_identity_fingerprint is not None:
            require_sha256_hex(
                "runtime_identity_fingerprint",
                self.runtime_identity_fingerprint,
            )
        has_tokens = self.effective_context_tokens is not None
        has_temperature = self.temperature is not None
        if has_tokens != has_temperature:
            raise ValueError(
                "effective_context_tokens and temperature must both be set "
                "to verify current runtime identity, or both be None for a "
                "persisted binding",
            )
        if not has_tokens:
            return
        spec = canonical_runtime_identity_spec(
            model_tag=self.model_tag,
            model_digest=self.model_digest,
            endpoint=self.endpoint,
            runtime_version=self.runtime_version,
            normalizer_id=self.normalizer_id,
            normalizer_version=self.normalizer_version,
            effective_context_tokens=self.effective_context_tokens,
            temperature=self.temperature,
        )
        expected = fingerprint_runtime_identity(spec)
        object.__setattr__(self, "effective_context_tokens", spec["effective_context_tokens"])
        object.__setattr__(self, "temperature", spec["temperature"])
        if self.runtime_identity_fingerprint is None:
            object.__setattr__(self, "runtime_identity_fingerprint", expected)
        elif self.runtime_identity_fingerprint != expected:
            raise ValueError(
                "runtime_identity_fingerprint does not match canonical "
                "runtime-identity-spec-v2",
            )

    def matches(self, other: RuntimeProfileIdentity) -> bool:
        """Exact match on the COMMON runtime identity — `None` only ever
        matches `None`, never treated as a wildcard. This is what stops
        a certificate issued against a partially-described profile
        (e.g. no digest recorded) from being silently treated as
        covering a DIFFERENT, now-current profile that also happens to
        omit a digest for an unrelated reason, what stops a native-only
        certificate from silently covering a runtime that uses a
        compatibility normalizer (or vice versa), and what stops a
        certificate issued against one common runtime-identity
        fingerprint from covering a later temperature, context,
        digest, endpoint, runtime-version, or normalizer change.

        Historical `runtime_config_fingerprint` (v1) is not part of
        this match: a Baseline Security certificate and a Planner
        certificate must be able to name the same underlying runtime
        even when their evaluation protocols (output-token budget,
        tool-choice enforcement, ...) differ. Role/evaluation matching
        is `workers.role_qualification.RoleEvaluationIdentity`, not
        this type."""
        if not isinstance(other, RuntimeProfileIdentity):
            return False
        return (
            self.model_tag == other.model_tag
            and self.model_digest == other.model_digest
            and self.endpoint == other.endpoint
            and self.runtime_version == other.runtime_version
            and self.normalizer_id == other.normalizer_id
            and self.normalizer_version == other.normalizer_version
            and self.runtime_identity_fingerprint == other.runtime_identity_fingerprint
        )

    @property
    def is_verified_current(self) -> bool:
        """`True` only for a CURRENT runtime identity whose v2
        fingerprint was derived from this object's own
        `effective_context_tokens` and `temperature`. A persisted
        certificate binding reconstructed from stored columns is never
        this: it cannot masquerade as factory-derived configuration."""
        return (
            self.effective_context_tokens is not None
            and self.temperature is not None
            and self.runtime_identity_fingerprint is not None
        )

    @property
    def is_fully_specified(self) -> bool:
        """`True` only when this is a verified current identity AND
        every *model/runtime* identity field is populated.
        `model_tag` alone (or any strict subset, including all model
        fields plus a caller-supplied fingerprint without concrete
        config) is a legitimate binding for a *recorded* certificate
        (`record_baseline_certificate()` never required more, and this
        property does not retroactively change that), but it is NOT a
        strong enough binding for `workers.production_eligibility` to
        treat as authoritative for a real production decision: two
        meaningfully different runtimes (different digest, different
        endpoint, different runtime version, different inference
        configuration) could otherwise share the same loosely-specified
        profile, or an arbitrary SHA-256 could be asserted as the
        current runtime. `workers.production_eligibility` refuses to
        evaluate eligibility at all against a `runtime_profile` for
        which this is `False` — unknown identity fails closed rather
        than wildcard-matching (see that module's own docstring).

        `normalizer_id`/`normalizer_version` being `None` is a complete
        statement of native-only transport, not a missing identity
        field, so it does not make this property `False`. An incomplete
        pair cannot be constructed at all (see `__post_init__`).
        `runtime_identity_fingerprint` being `None`, or being present
        without the concrete config that produced it, IS a missing
        production-identity component: an old certificate recorded
        before this field existed, a persisted binding reconstructed
        from stored columns, or an evaluation that never established
        the common runtime identity, cannot authorize a fully-specified
        current runtime. A historical v1 `runtime_config_fingerprint`
        does not substitute for it."""
        return self.is_verified_current and None not in (
            self.model_tag,
            self.model_digest,
            self.endpoint,
            self.runtime_version,
            self.runtime_identity_fingerprint,
        )


@dataclass(frozen=True)
class SecurityCertificationResult:
    ok: bool
    reason: str
    certificate: WorkerBaselineSecurityCertificate | None = None


def _deny(reason: str) -> SecurityCertificationResult:
    return SecurityCertificationResult(False, reason)


def record_baseline_certificate(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    runtime_profile: RuntimeProfileIdentity,
    outcome: SecurityBaselineOutcome,
    evidence_ref: str,
    reason: str,
    hard_disqualifiers: tuple[HardDisqualifierCategory, ...] = (),
    now_fn=utcnow_iso,
) -> SecurityCertificationResult:
    """Durably record one Baseline Security evaluation result — the ONLY
    way a `worker_baseline_security_certificates` row is ever created.
    Never mutates `workers`/`worker_trust_events`/any role-qualification
    state; see the module docstring's "What this module is, and is not"
    section.

    Fails closed on any malformed or internally inconsistent request: an
    unknown worker, a non-`RuntimeProfileIdentity` profile, an `outcome`
    not in `SecurityBaselineOutcome`, a blank `evidence_ref`/`reason`,
    `hard_disqualifiers` entries that are not `HardDisqualifierCategory`
    members, or `hard_disqualifiers` that disagrees with `outcome` (see
    `HardDisqualifierCategory`'s own docstring for what "disagrees"
    means). Nothing here decides that the worker IS safe or unsafe — it
    decides only whether the caller's already-computed result is
    well-formed enough to durably trust as evidence."""
    if not isinstance(worker_id, str) or not worker_id:
        return _deny("malformed_certificate_request")
    if not isinstance(runtime_profile, RuntimeProfileIdentity):
        return _deny("malformed_certificate_request")
    if (
        runtime_profile.runtime_identity_fingerprint is not None
        and not runtime_profile.is_verified_current
    ):
        return _deny("unverified_runtime_identity")
    if not isinstance(outcome, SecurityBaselineOutcome):
        return _deny("malformed_certificate_request")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        return _deny("missing_evidence_reference")
    if not isinstance(reason, str) or not reason.strip():
        return _deny("malformed_certificate_request")
    if not isinstance(hard_disqualifiers, tuple) or any(
        not isinstance(item, HardDisqualifierCategory) for item in hard_disqualifiers
    ):
        return _deny("malformed_certificate_request")
    if outcome == SecurityBaselineOutcome.HARD_DISQUALIFIED and not hard_disqualifiers:
        return _deny("hard_disqualified_requires_at_least_one_disqualifier")
    if outcome != SecurityBaselineOutcome.HARD_DISQUALIFIED and hard_disqualifiers:
        return _deny("hard_disqualifiers_only_valid_for_hard_disqualified_outcome")

    with transaction(conn):
        if WorkersRepo(conn).get(worker_id) is None:
            return _deny("unknown_worker")
        certificate_id = uuid.uuid4().hex
        issued_at = now_fn()
        certificate = BaselineSecurityCertificatesRepo(conn).record_in_transaction(
            certificate_id=certificate_id,
            worker_id=worker_id,
            baseline_version=BASELINE_VERSION,
            model_tag=runtime_profile.model_tag,
            model_digest=runtime_profile.model_digest,
            endpoint=runtime_profile.endpoint,
            runtime_version=runtime_profile.runtime_version,
            normalizer_id=runtime_profile.normalizer_id,
            normalizer_version=runtime_profile.normalizer_version,
            runtime_config_fingerprint=runtime_profile.runtime_config_fingerprint,
            runtime_identity_fingerprint=runtime_profile.runtime_identity_fingerprint,
            outcome=outcome.value,
            hard_disqualifiers_json=json.dumps([d.value for d in hard_disqualifiers]),
            evidence_ref=evidence_ref,
            reason=reason,
            issued_at=issued_at,
        )
        AuditWriter(conn).append(
            task_id=None,
            event_type=EventType.SECURITY_BASELINE_CERTIFICATE_RECORDED,
            actor_type="system",
            actor_id=worker_id,
            payload={
                "certificate_id": certificate_id,
                "worker_id": worker_id,
                "baseline_version": BASELINE_VERSION,
                "model_tag": runtime_profile.model_tag,
                "model_digest": runtime_profile.model_digest,
                "endpoint": runtime_profile.endpoint,
                "runtime_version": runtime_profile.runtime_version,
                "normalizer_id": runtime_profile.normalizer_id,
                "normalizer_version": runtime_profile.normalizer_version,
                "runtime_config_fingerprint": runtime_profile.runtime_config_fingerprint,
                "runtime_identity_fingerprint": runtime_profile.runtime_identity_fingerprint,
                "outcome": outcome.value,
                "hard_disqualifiers": [d.value for d in hard_disqualifiers],
                "evidence_ref": evidence_ref,
                "reason": reason,
            },
        )
        return SecurityCertificationResult(True, "certificate_recorded", certificate=certificate)
