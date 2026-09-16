"""Production eligibility gate: the minimal, code-owned decision point a
future router should query instead of deciding trust/qualification/
security for itself (Baseline Security Certification foundation).

## Core invariant

Production eligibility requires BOTH a valid Baseline Security
Certificate (`workers.security_baseline`) AND a valid role-specific
qualification result — never either one alone, and never a strong
role-qualification result compensating for a missing/failed/
disqualifying Baseline Security Certificate. Security is always checked
first, and a hard disqualifier is decided before role qualification is
even consulted — see "Hard disqualifiers always win" below.

## Role qualification is a caller-supplied input, not something this module derives

This codebase has no generic, durable, per-role qualification store yet
— `planning.qualification` is a Planner-specific, wholly in-memory
evaluation harness that "never writes durable state" (its own module
docstring). Rather than invent a parallel, likely-mismatched persistence
model for "role qualification" just to make this gate self-contained,
`role_qualification` is accepted here as an explicit
`RoleQualificationStatus` the caller already determined however it
currently can. This keeps the one thing this module actually owns —
combining Baseline Security with *a* role-qualification signal into one
fail-closed decision — honest about what it does and does not derive on
its own. A future generic role-qualification registry can replace the
caller's own determination of this value without this gate's own
combination logic changing at all. This is also what makes "no
transitive certification" structural rather than merely a convention: a
`PASS` computed for one `(worker_id, role)` pair is never consulted, and
has no way to be consulted, when evaluating a different role for the
same worker — each call supplies its own `role_qualification` value
independently.

## Hard disqualifiers always win

A `SecurityBaselineOutcome.HARD_DISQUALIFIED` certificate denies
eligibility unconditionally, before `role_qualification` is even
inspected — no role-qualification result, however strong, can ever
compensate for it (the literal requirement this gate exists to enforce).
Plain `FAIL` denies identically in terms of eligibility, but is reported
under its own distinct reason code for audit/provenance clarity (see
`workers.security_baseline.SecurityBaselineOutcome`'s own docstring for
why the two remain distinguishable at all).

## Runtime-profile binding: no silent stale reuse

A certificate is consulted only if its own recorded `RuntimeProfileIdentity`
matches the CALLER-SUPPLIED current profile exactly
(`RuntimeProfileIdentity.matches()`). An existing certificate for a
*different* profile bound to the same `worker_id` is treated exactly the
same as no certificate at all, never silently reused — see `workers.
security_baseline`'s module docstring for why `worker_id` alone cannot
establish this. Among certificates that DO match the current profile,
the most recent one always wins (`store.baseline_security_certificates_
repo.BaselineSecurityCertificatesRepo.list_for_worker`'s own ordering):
an older, superseded certificate for that same exact profile is never
preferred over a newer one, even if the newer one reversed the verdict.

## No trust/qualification mutation, ever

This module only reads `workers`/`worker_baseline_security_certificates`
— it never writes to `worker_trust_events`, never issues or invalidates
a certificate, and never grants/downgrades trust of any kind. It answers
"is this worker eligible right now," nothing else.

## Fail closed

Every ambiguous, missing, or stale condition denies eligibility —
unknown worker, no certificate at all, a certificate for the wrong
runtime profile, a non-`PASS` certificate outcome, or a malformed
request all deny with a distinct, stable reason code. There is no
permissive default anywhere in this module: a persistence/read failure
(`sqlite3.Error`) is never caught here and never silently converted into
"eligible" — it propagates, so a caller can never mistake "we could not
determine eligibility" for "this worker was found eligible."

## Not yet implemented (disclosed gaps)

This module does not itself select, rank, or route to any worker — it
only answers a single yes/no eligibility question for one already-named
`(worker_id, role, runtime_profile)`. A real router, a durable role-
qualification registry, and the dedicated Security-role certification
concept are all future work this gate is built to support without
requiring its own combination logic to change.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.security_baseline import RuntimeProfileIdentity, SecurityBaselineOutcome


class RoleQualificationStatus(StrEnum):
    """The caller-determined role-specific qualification result for
    exactly one `(worker_id, role)` pair — see the module docstring's
    "Role qualification is a caller-supplied input" section. `MISSING`
    (no qualification evidence exists at all) is kept distinct from
    `FAIL` (qualification was attempted and did not pass) purely for
    audit/provenance clarity; both deny eligibility identically."""

    PASS = "PASS"
    FAIL = "FAIL"
    MISSING = "MISSING"


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason: str
    certificate_id: str | None = None


def _deny(reason: str, *, certificate_id: str | None = None) -> EligibilityDecision:
    return EligibilityDecision(False, reason, certificate_id=certificate_id)


def _lookup_certificate(conn, worker_id, runtime_profile):
    """`(certificate, None)` for the most recent certificate matching
    `runtime_profile` exactly, or `(None, reason)` naming exactly why
    none qualifies — no certificate exists at all for this worker, or
    certificates exist but none match the current profile (see the
    module docstring's "Runtime-profile binding" section)."""
    certificates = BaselineSecurityCertificatesRepo(conn).list_for_worker(worker_id)
    if not certificates:
        return None, "no_baseline_security_certificate"
    for certificate in certificates:
        candidate = RuntimeProfileIdentity(
            model_tag=certificate.model_tag, model_digest=certificate.model_digest,
            endpoint=certificate.endpoint, runtime_version=certificate.runtime_version,
        )
        if candidate.matches(runtime_profile):
            return certificate, None
    return None, "baseline_security_certificate_profile_mismatch"


def is_worker_eligible(
    conn: sqlite3.Connection, *, worker_id: str, role: str,
    runtime_profile: RuntimeProfileIdentity, role_qualification: RoleQualificationStatus,
) -> EligibilityDecision:
    """The one production-eligibility gate a future router should query
    instead of deciding trust/qualification/security for itself. See the
    module docstring for the full rule set. Read-only: never opens a
    write transaction, never mutates any durable state."""
    if not isinstance(worker_id, str) or not worker_id:
        return _deny("malformed_eligibility_request")
    if not isinstance(role, str) or not role:
        return _deny("malformed_eligibility_request")
    if not isinstance(runtime_profile, RuntimeProfileIdentity):
        return _deny("malformed_eligibility_request")
    if not isinstance(role_qualification, RoleQualificationStatus):
        return _deny("malformed_eligibility_request")

    if WorkersRepo(conn).get(worker_id) is None:
        return _deny("unknown_worker")

    certificate, deny_reason = _lookup_certificate(conn, worker_id, runtime_profile)
    if certificate is None:
        return _deny(deny_reason)

    # Security is decided completely before role qualification is ever
    # inspected -- a hard disqualifier or a plain security FAIL denies
    # outright, regardless of how strong role_qualification is.
    if certificate.outcome == SecurityBaselineOutcome.HARD_DISQUALIFIED.value:
        return _deny("security_hard_disqualifier", certificate_id=certificate.certificate_id)
    if certificate.outcome != SecurityBaselineOutcome.PASS.value:
        return _deny("security_baseline_fail", certificate_id=certificate.certificate_id)

    if role_qualification == RoleQualificationStatus.MISSING:
        return _deny("role_qualification_missing", certificate_id=certificate.certificate_id)
    if role_qualification == RoleQualificationStatus.FAIL:
        return _deny("role_qualification_fail", certificate_id=certificate.certificate_id)

    return EligibilityDecision(True, "eligible", certificate_id=certificate.certificate_id)
