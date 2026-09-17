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

Reuses `workers.security_baseline.RuntimeProfileIdentity` unchanged —
the same identity vocabulary, the same exact-match-no-wildcard
`.matches()` semantics, for the same reason (see that module's own
docstring, including `.is_fully_specified` for why `workers.
production_eligibility` demands a completely specified profile before
ever trusting either certificate kind for a real decision).

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

import sqlite3
import uuid
from dataclasses import dataclass
from enum import StrEnum

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.models import WorkerRoleCertificate
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.security_baseline import RuntimeProfileIdentity


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
    now_fn=utcnow_iso,
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
    a blank `evidence_ref`, or a blank `reason`. Nothing here decides
    that the worker IS qualified for the role — it decides only whether
    the caller's already-computed decision is well-formed enough to
    durably trust as evidence. Models never self-certify: this function
    has no notion of a model's own claim about itself, only whatever the
    caller (a code-owned certification boundary) already verified."""
    if not isinstance(worker_id, str) or not worker_id:
        return _deny("malformed_certificate_request")
    if not isinstance(role, ProductionRole):
        return _deny("malformed_certificate_request")
    if not isinstance(runtime_profile, RuntimeProfileIdentity):
        return _deny("malformed_certificate_request")
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

    with transaction(conn):
        if WorkersRepo(conn).get(worker_id) is None:
            return _deny("unknown_worker")
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
                "outcome": outcome.value,
                "classification": classification,
                "evidence_ref": evidence_ref,
                "reason": reason,
            },
        )
        return RoleCertificationResult(True, "certificate_recorded", certificate=certificate)
