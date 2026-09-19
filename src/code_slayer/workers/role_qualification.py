"""Role-specific qualification certification: the durable, generic
registry backing "is worker X certified for role Y under runtime profile
Z" (Role Qualification Certification foundation).

## What this module is, and is not

This is the GENERIC, role-agnostic certificate representation and
recording primitive — it does not itself decide whether any specific
role's qualification evidence is sufficient to certify. That decision
belongs to each role's own "certification boundary" (e.g. `planning.
planner_certification` for `ProductionRole.PLANNER`), which calls
`record_role_certificate()` here only once it has already made that
decision from real, existing qualification evidence. This mirrors
`workers.security_baseline`'s exact "representation and recording, not
evaluation" boundary — see that module's own docstring.

`workers.role_qualification` deliberately does NOT import anything from
`planning` — this package sits below `planning` in this codebase's
dependency direction (`planning.worker_planner`/`planning.qualification`
already import from `workers`, never the reverse), so any role's own
qualification-specific evidence type (e.g. `planning.qualification.
QualificationAttemptResult`) is consumed by that role's OWN certification
boundary module, which then calls down into this generic registry with
already-plain values (`ProductionRole`, `RoleQualificationOutcome`, a
plain evidence-reference string) — never the other way around.

## Roles are a fixed, code-owned vocabulary

`ProductionRole` is the complete set of production-capable roles this
codebase currently recognizes: `PLANNER`, `CODER`, `REVIEWER`,
`REPAIRER`, `SECURITY`. This is a stricter vocabulary than the free-form
`role: str` `workers.trust`/`workers.conformance` already use for
tool-capability trust scoping — a deliberately separate, lower-level,
narrower concept (a trust scope like `(worker_id, "coder", "read_file")`
says nothing about role CERTIFICATION) — see those modules' own
docstrings. A role certificate specifically must name one of this fixed
set, so an arbitrary invented role name can never be silently
certified.

`ProductionRole.SECURITY` here names the **dedicated Security review
role** (an independent reviewer role a Security-role-certified worker
may someday perform, `docs/CODE_SLAYER_VISION.md` §47) — a completely
separate concept from the mandatory Baseline Security Certificate every
production-capable worker/model needs regardless of role (`workers.
security_baseline`). Holding a `SECURITY` role certificate never
substitutes for, and is never implied by, holding a Baseline Security
Certificate, or vice versa — see `workers.production_eligibility`'s own
docstring for how the two combine (never merge) into one eligibility
decision.

## No transitive certification

A certificate for one role says nothing about any other role, and
nothing about the mandatory Baseline Security Certificate. Nothing in
this module reads a certificate to influence recording or looking up a
DIFFERENT role's certificate — there is no code path whereby, e.g., a
`PLANNER` certification could ever produce, widen, or influence a
`CODER` row, or a Baseline Security Certificate.

## Identity and runtime-profile binding

Reuses `workers.security_baseline.RuntimeProfileIdentity` unchanged as
the COMMON, role-independent runtime identity — the same exact-match-
no-wildcard `.matches()` semantics, for the same reason (see that
module's own docstring, including `.is_fully_specified` for why
`workers.production_eligibility` demands a completely specified common
runtime profile before ever trusting either certificate kind for a real
decision).

A role certificate additionally binds a `RoleEvaluationIdentity`: the
canonical, versioned evaluation configuration that materially affected
that role's qualification (common runtime-identity fingerprint, output-
token budget, tool-choice enforcement, role, policy/version). This is
generic enough for future CODER / REVIEWER / REPAIRER / SECURITY role
qualification. Baseline Security must never reuse a Planner (or any
other role) evaluation profile — it matches only the common runtime
identity. `None` for `role_evaluation_fingerprint` is a complete
statement of absence, not a wildcard: a legacy v1 Planner certificate
cannot authorize a current fully specified role/evaluation profile.

## No fabrication, ever

Mirrors `workers.security_baseline.record_baseline_certificate()`
exactly: `evidence_ref` is required on every certificate, `PASS` and
`FAIL` alike — a certificate is never a bare boolean. `classification`
is required too: the richer, role-specific evidence detail behind
`outcome` (e.g. a Planner certification boundary passes through
`planning.qualification.QualificationOutcome`'s own value, such as
`"PASS_FIRST_TRY"`/`"FAIL_POLICY"`) — this module does not interpret
`classification` itself, it only requires it to be a non-blank string,
so each role's own certification boundary owns its own classification
vocabulary without this generic registry needing to know it.

## No grant of trust, permission, or another role

`record_role_certificate()` never touches `worker_trust_events`, never
touches `worker_baseline_security_certificates`, never touches any
OTHER role's own certificate row, and never widens mutation/network/
filesystem authority. A role certificate means only: this exact worker,
under this exact runtime profile, has demonstrated sufficient evidence
for this exact role under this exact qualification policy/version —
nothing more.

## Append-only, like every other evidence table in this codebase

`worker_role_certificates` is fully append-only — a re-evaluation,
including one that reverses an earlier verdict, always creates a NEW
row. There is no in-place "invalidate" transition (a disclosed gap,
mirroring `workers.security_baseline`'s own); staleness is handled
entirely by `workers.production_eligibility` always consulting the most
recent certificate matching the current profile.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from enum import StrEnum

from code_slayer.audit.canonical import canonical_json
from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.models import WorkerRoleCertificate
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkerLifecycleState, WorkersRepo
from code_slayer.workers.security_baseline import (
    RuntimeProfileIdentity,
    require_sha256_hex,
)

ROLE_EVALUATION_SPEC_VERSION = "role-evaluation-spec-v1"


class ProductionRole(StrEnum):
    """The complete, fixed vocabulary of production-capable roles a
    role certificate may name — see the module docstring. Never a
    free-form string a caller can invent."""

    PLANNER = "PLANNER"
    CODER = "CODER"
    REVIEWER = "REVIEWER"
    REPAIRER = "REPAIRER"
    SECURITY = "SECURITY"


class RoleQualificationOutcome(StrEnum):
    """The fixed outcome vocabulary one role certificate can record —
    mirrors `workers.security_baseline.SecurityBaselineOutcome`'s
    `PASS`/`FAIL` half exactly. A role certificate has no `HARD_
    DISQUALIFIED` concept of its own — hard disqualifiers are a
    Security-specific concern (`workers.security_baseline.
    HardDisqualifierCategory`), never a role-qualification one; see the
    module docstring."""

    PASS = "PASS"
    FAIL = "FAIL"


def _non_negative_int(name: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def canonical_role_evaluation_spec(
    *,
    role: ProductionRole,
    runtime_identity_fingerprint: str,
    output_token_budget: int,
    tool_choice_enforcement: str,
    policy_version: str,
) -> dict:
    """The one canonical, versioned role/evaluation document a role
    certificate binds in addition to the common runtime identity.
    Callers never supply this as free-form JSON from a model; every
    field is a caller-verified value.

    Generic across PLANNER / CODER / REVIEWER / REPAIRER / SECURITY:
    the role name and that role's own policy/version are part of the
    identity, so a Planner evaluation profile can never silently
    authorize a Coder (or Baseline Security) decision. The common
    runtime-identity fingerprint is included so a role profile is
    never detached from the runtime it was evaluated on."""
    if not isinstance(role, ProductionRole):
        raise TypeError("role must be a ProductionRole")
    require_sha256_hex("runtime_identity_fingerprint", runtime_identity_fingerprint)
    if not isinstance(tool_choice_enforcement, str) or not tool_choice_enforcement.strip():
        raise ValueError("tool_choice_enforcement must be a non-empty string")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ValueError("policy_version must be a non-empty string")
    return {
        "spec_version": ROLE_EVALUATION_SPEC_VERSION,
        "role": role.value,
        "runtime_identity_fingerprint": runtime_identity_fingerprint,
        "output_token_budget": _non_negative_int("output_token_budget", output_token_budget),
        "tool_choice_enforcement": tool_choice_enforcement,
        "policy_version": policy_version,
    }


def fingerprint_role_evaluation(spec: dict) -> str:
    """SHA-256 of `canonical_json(spec)`. Exact-match identity: any
    evaluation-relevant field change, or a spec-version bump, produces
    a different fingerprint. Never a wildcard. A common-runtime v2
    document is refused — that identity space is separate."""
    if not isinstance(spec, dict):
        raise TypeError("role evaluation spec must be a dict")
    if spec.get("spec_version") != ROLE_EVALUATION_SPEC_VERSION:
        raise ValueError("role evaluation spec_version is missing or unsupported")
    digest = hashlib.sha256(canonical_json(spec).encode("utf-8")).hexdigest()
    return require_sha256_hex("role_evaluation_fingerprint", digest)


def role_evaluation_identity_from_config(
    *,
    role: ProductionRole,
    runtime_identity_fingerprint: str,
    output_token_budget: int,
    tool_choice_enforcement: str,
    policy_version: str,
) -> RoleEvaluationIdentity:
    """The one production-grade constructor: the fingerprint is derived
    from the canonical spec, never supplied independently, and never
    read from a model response."""
    spec = canonical_role_evaluation_spec(
        role=role,
        runtime_identity_fingerprint=runtime_identity_fingerprint,
        output_token_budget=output_token_budget,
        tool_choice_enforcement=tool_choice_enforcement,
        policy_version=policy_version,
    )
    return RoleEvaluationIdentity(
        role=role,
        runtime_identity_fingerprint=runtime_identity_fingerprint,
        output_token_budget=spec["output_token_budget"],
        tool_choice_enforcement=tool_choice_enforcement,
        policy_version=policy_version,
        role_evaluation_fingerprint=fingerprint_role_evaluation(spec),
    )


@dataclass(frozen=True)
class RoleEvaluationIdentity:
    """The evaluation configuration that materially affected one role
    qualification — bound to the common runtime identity, never a
    substitute for it.

    `None` is not representable for the fingerprint: a missing
    evaluation identity is expressed by omitting this object (recording
    stores NULL; production eligibility fails closed). Construction is
    structurally self-verifying: `__post_init__` recomputes
    `canonical_role_evaluation_spec` from this object's own fields and
    refuses a `role_evaluation_fingerprint` that does not match. Direct
    construction with a caller-supplied hash is therefore not a way to
    forge identity; the production-grade constructor remains
    `role_evaluation_identity_from_config`."""

    role: ProductionRole
    runtime_identity_fingerprint: str
    output_token_budget: int
    tool_choice_enforcement: str
    policy_version: str
    role_evaluation_fingerprint: str

    def __post_init__(self) -> None:
        spec = canonical_role_evaluation_spec(
            role=self.role,
            runtime_identity_fingerprint=self.runtime_identity_fingerprint,
            output_token_budget=self.output_token_budget,
            tool_choice_enforcement=self.tool_choice_enforcement,
            policy_version=self.policy_version,
        )
        expected = fingerprint_role_evaluation(spec)
        if self.role_evaluation_fingerprint != expected:
            raise ValueError(
                "role_evaluation_fingerprint does not match canonical role-evaluation-spec-v1",
            )

    def matches(self, other: RoleEvaluationIdentity) -> bool:
        """Exact match on every field — never a wildcard."""
        if not isinstance(other, RoleEvaluationIdentity):
            return False
        return (
            self.role == other.role
            and self.runtime_identity_fingerprint == other.runtime_identity_fingerprint
            and self.output_token_budget == other.output_token_budget
            and self.tool_choice_enforcement == other.tool_choice_enforcement
            and self.policy_version == other.policy_version
            and self.role_evaluation_fingerprint == other.role_evaluation_fingerprint
        )

    @property
    def is_fully_specified(self) -> bool:
        """`True` only when this object's fingerprint recomputes from
        its own fields. Construction already refuses a mismatch, so a
        successfully constructed instance is fully specified; this
        property still rechecks so a later mutation cannot silently
        become authoritative."""
        try:
            spec = canonical_role_evaluation_spec(
                role=self.role,
                runtime_identity_fingerprint=self.runtime_identity_fingerprint,
                output_token_budget=self.output_token_budget,
                tool_choice_enforcement=self.tool_choice_enforcement,
                policy_version=self.policy_version,
            )
        except (TypeError, ValueError):
            return False
        return self.role_evaluation_fingerprint == fingerprint_role_evaluation(spec)


@dataclass(frozen=True)
class RoleCertificationResult:
    ok: bool
    reason: str
    certificate: WorkerRoleCertificate | None = None


def _deny(reason: str) -> RoleCertificationResult:
    return RoleCertificationResult(False, reason)


def record_role_certificate(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    role: ProductionRole,
    runtime_profile: RuntimeProfileIdentity,
    policy_version: str,
    outcome: RoleQualificationOutcome,
    classification: str,
    evidence_ref: str,
    reason: str,
    role_evaluation: RoleEvaluationIdentity | None = None,
    now_fn=utcnow_iso,
    require_active_worker: bool = False,
) -> RoleCertificationResult:
    """Durably record one role-qualification certification decision —
    the ONLY way a `worker_role_certificates` row is ever created. Never
    mutates `workers`/`worker_trust_events`/`worker_baseline_security_
    certificates`/any other role's certificate row; see the module
    docstring's "What this module is, and is not" / "No transitive
    certification" sections.

    Fails closed on any malformed or internally inconsistent request: an
    unknown worker, a non-`ProductionRole` role, a non-
    `RuntimeProfileIdentity` profile, a blank `policy_version`, an
    `outcome` not in `RoleQualificationOutcome`, a blank `classification`,
    a blank `evidence_ref`, a blank `reason`, or a `role_evaluation`
    that disagrees with `role`/`policy_version`/the common runtime
    identity. Recording remains honest about a missing evaluation
    identity (`role_evaluation is None` stores NULL, which is never a
    wildcard). Nothing here decides that the worker IS qualified for
    the role — it decides only whether the caller's already-computed
    decision is well-formed enough to durably trust as evidence. Models
    never self-certify: this function has no notion of a model's own
    claim about itself, only whatever the caller (a code-owned
    certification boundary) already verified.

    `require_active_worker` (H.3 review finding): when `True`, this
    function additionally requires `worker_id` to be administratively
    `ACTIVE` -- reloaded and checked INSIDE this same `BEGIN IMMEDIATE`
    transaction, atomically with the INSERT below, denying
    `worker_archived` otherwise with nothing written. Pass `True` only
    when `conn` is the PRODUCTION connection (every role certificate
    this function has ever recorded already lands directly in
    PRODUCTION -- see `security.live_planner_certification`'s own
    module docstring). Defaults to `False` so every existing caller's
    behavior is unchanged."""
    if not isinstance(worker_id, str) or not worker_id:
        return _deny("malformed_certificate_request")
    if not isinstance(role, ProductionRole):
        return _deny("malformed_certificate_request")
    if not isinstance(runtime_profile, RuntimeProfileIdentity):
        return _deny("malformed_certificate_request")
    if (
        runtime_profile.runtime_identity_fingerprint is not None
        and not runtime_profile.is_verified_current
    ):
        return _deny("unverified_runtime_identity")
    if not isinstance(policy_version, str) or not policy_version.strip():
        return _deny("malformed_certificate_request")
    if not isinstance(outcome, RoleQualificationOutcome):
        return _deny("malformed_certificate_request")
    if not isinstance(classification, str) or not classification.strip():
        return _deny("malformed_certificate_request")
    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        return _deny("missing_evidence_reference")
    if not isinstance(reason, str) or not reason.strip():
        return _deny("malformed_certificate_request")
    if role_evaluation is not None:
        if not isinstance(role_evaluation, RoleEvaluationIdentity):
            return _deny("malformed_certificate_request")
        if not runtime_profile.is_verified_current:
            return _deny("unverified_runtime_identity")
        if role_evaluation.role != role:
            return _deny("malformed_certificate_request")
        if role_evaluation.policy_version != policy_version:
            return _deny("malformed_certificate_request")
        if (
            runtime_profile.runtime_identity_fingerprint is None
            or role_evaluation.runtime_identity_fingerprint
            != runtime_profile.runtime_identity_fingerprint
        ):
            return _deny("malformed_certificate_request")
        try:
            expected_eval = fingerprint_role_evaluation(
                canonical_role_evaluation_spec(
                    role=role_evaluation.role,
                    runtime_identity_fingerprint=role_evaluation.runtime_identity_fingerprint,
                    output_token_budget=role_evaluation.output_token_budget,
                    tool_choice_enforcement=role_evaluation.tool_choice_enforcement,
                    policy_version=role_evaluation.policy_version,
                ),
            )
        except (TypeError, ValueError):
            return _deny("malformed_certificate_request")
        if role_evaluation.role_evaluation_fingerprint != expected_eval:
            return _deny("malformed_certificate_request")

    with transaction(conn):
        worker = WorkersRepo(conn).get(worker_id)
        if worker is None:
            return _deny("unknown_worker")
        if require_active_worker and worker.lifecycle_state != WorkerLifecycleState.ACTIVE:
            return _deny("worker_archived")
        certificate_id = uuid.uuid4().hex
        issued_at = now_fn()
        certificate = RoleCertificatesRepo(conn).record_in_transaction(
            certificate_id=certificate_id,
            worker_id=worker_id,
            role=role.value,
            policy_version=policy_version,
            model_tag=runtime_profile.model_tag,
            model_digest=runtime_profile.model_digest,
            endpoint=runtime_profile.endpoint,
            runtime_version=runtime_profile.runtime_version,
            normalizer_id=runtime_profile.normalizer_id,
            normalizer_version=runtime_profile.normalizer_version,
            runtime_config_fingerprint=runtime_profile.runtime_config_fingerprint,
            runtime_identity_fingerprint=runtime_profile.runtime_identity_fingerprint,
            role_evaluation_fingerprint=(
                None if role_evaluation is None else role_evaluation.role_evaluation_fingerprint
            ),
            outcome=outcome.value,
            classification=classification,
            evidence_ref=evidence_ref,
            reason=reason,
            issued_at=issued_at,
        )
        AuditWriter(conn).append(
            task_id=None,
            event_type=EventType.ROLE_QUALIFICATION_CERTIFICATE_RECORDED,
            actor_type="system",
            actor_id=worker_id,
            payload={
                "certificate_id": certificate_id,
                "worker_id": worker_id,
                "role": role.value,
                "policy_version": policy_version,
                "model_tag": runtime_profile.model_tag,
                "model_digest": runtime_profile.model_digest,
                "endpoint": runtime_profile.endpoint,
                "runtime_version": runtime_profile.runtime_version,
                "normalizer_id": runtime_profile.normalizer_id,
                "normalizer_version": runtime_profile.normalizer_version,
                "runtime_config_fingerprint": runtime_profile.runtime_config_fingerprint,
                "runtime_identity_fingerprint": runtime_profile.runtime_identity_fingerprint,
                "role_evaluation_fingerprint": (
                    None
                    if role_evaluation is None
                    else role_evaluation.role_evaluation_fingerprint
                ),
                "outcome": outcome.value,
                "classification": classification,
                "evidence_ref": evidence_ref,
                "reason": reason,
            },
        )
        return RoleCertificationResult(True, "certificate_recorded", certificate=certificate)
