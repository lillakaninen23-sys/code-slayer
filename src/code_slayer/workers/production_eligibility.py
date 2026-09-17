"""Production eligibility gate: the minimal, code-owned decision point a
future router should query instead of deciding trust/qualification/
security for itself (Baseline Security Certification foundation, Role
Qualification Certification foundation).

## Core invariant

Production eligibility requires ALL of:

1. a valid Baseline Security Certificate (`workers.security_baseline`)
2. a valid Role Certificate for the EXACT requested role
   (`workers.role_qualification`)
3. no hard Security disqualifier

— never any subset, and never a strong role certificate compensating for
a missing/failed/disqualifying Baseline Security Certificate. Security
is always decided first, and a hard disqualifier is decided before the
role certificate is even looked up — see "Hard disqualifiers always
win" below.

## `evaluate_production_eligibility()` is the ONE authoritative path

Both the Baseline Security Certificate AND the Role Certificate are
loaded here, by this function, from durable, backend-owned evidence — no
caller ever supplies a role-qualification verdict directly. **No caller
can fabricate eligibility by asserting a `PASS` status**: this function's
signature has no parameter for one, structurally, not merely by
convention. Compare this to the previous revision of this module, which
accepted a caller-supplied `RoleQualificationStatus` — that shape has
been removed entirely, not merely deprecated, because keeping it around
as "a pure helper" would still have been a real, public, `conn`-backed
function capable of producing a fabricated `EligibilityDecision`.

This is also what makes "no transitive certification" structural rather
than a convention: a `PASS` role certificate for one role is looked up
under an index keyed by that exact `(worker_id, role)` pair
(`store.role_certificates_repo.RoleCertificatesRepo.list_for_worker_role`)
— there is no code path by which evaluating one role could ever surface
or be influenced by a different role's certificate.

## Hard disqualifiers always win

A `SecurityBaselineOutcome.HARD_DISQUALIFIED` certificate denies
eligibility unconditionally, before the role certificate is even looked
up — no role certificate, however strong, can ever compensate for it
(the literal requirement this gate exists to enforce). Plain `FAIL`
denies identically in terms of eligibility, but is reported under its
own distinct reason code for audit/provenance clarity.

## Runtime-profile binding: no silent stale reuse, and no weak binding

A Baseline Security certificate is consulted only if its own recorded
COMMON runtime identity (`RuntimeProfileIdentity.matches()`) matches
the CALLER-SUPPLIED current common runtime exactly. A role certificate
is consulted only if it matches that same common runtime identity AND
its recorded `role_evaluation_fingerprint` matches the CALLER-SUPPLIED
current `RoleEvaluationIdentity`. A certificate for a *different*
common runtime, or a role certificate for a *different* evaluation
profile, bound to the same `worker_id`/role is treated exactly the same
as no certificate at all, never silently reused. Among certificates
that DO match, the most recent one always wins (`store.
*_certificates_repo`'s own `issued_at DESC` ordering): an older,
superseded certificate is never preferred over a newer one, even if the
newer one reversed the verdict.

**This function additionally refuses to evaluate eligibility at all
against an incompletely-specified `runtime_profile` or
`role_evaluation`** (`RuntimeProfileIdentity.is_fully_specified` —
every one of `model_tag`/`model_digest`/`endpoint`/`runtime_version`/
`runtime_identity_fingerprint` must be populated; `RoleEvaluationIdentity`
is fully specified by construction). A certificate MAY legitimately be
recorded with only `model_tag` known, or without a v2 runtime-identity
fingerprint or role-evaluation fingerprint (`workers.security_baseline`/
`workers.role_qualification` still accept that — recording should stay
honest about what evaluation time actually established), but a real
PRODUCTION decision must never pretend a loosely-specified profile is a
strong enough runtime-profile binding: two meaningfully different
runtimes could otherwise share the same `model_tag`-only profile, a
pre-v15 NULL-identity certificate could otherwise authorize a later
temperature/context change, or a legacy v1 Planner certificate
(`runtime_config_fingerprint` only) could otherwise wildcard-match a
current fully specified common identity plus role/evaluation profile.
Unknown identity fails closed here rather than wildcard-matching.
Historical v1 `runtime_config_fingerprint` values are never
reinterpreted as v2 common-runtime identity.

## No trust/qualification mutation, ever

This module only reads `workers`/`worker_baseline_security_certificates`
/`worker_role_certificates` — it never writes to any of them, never
issues or invalidates a certificate, and never grants/downgrades trust
of any kind. It answers "is this worker eligible right now for this
role," nothing else.

## Fail closed

Every ambiguous, missing, or stale condition denies eligibility —
unknown worker, no certificate of either kind, a certificate for the
wrong runtime profile, a non-`PASS` certificate outcome, an
incompletely-specified runtime profile, or a malformed request all deny
with a distinct, stable reason code. There is no permissive default
anywhere in this module: a persistence/read failure (`sqlite3.Error`) is
never caught here and never silently converted into "eligible" — it
propagates, so a caller can never mistake "we could not determine
eligibility" for "this worker was found eligible."

## Policy/version staleness

A certificate recorded under a policy/baseline version this code no
longer recognizes as current is treated exactly like a certificate for
the wrong runtime profile — evidence that once meant something, but not
under the semantics production code evaluates against today. For the
Baseline Security Certificate, the expected version is this module's own
import of `workers.security_baseline.BASELINE_VERSION` (there is only
ever one current baseline policy, and it lives beside the certificate
representation itself). For the Role Certificate, the expected version
is `expected_role_policy_version` — a REQUIRED caller-supplied
parameter, never a value this module guesses: each role's own
certification boundary (e.g. `planning.planner_certification.
PLANNER_CERTIFICATION_POLICY_VERSION` for `PLANNER`) is a higher layer
this package must not import (`workers` sits below `planning` in this
codebase's dependency direction), so the caller — which already knows
which role it is asking about — supplies the current version for it.

## Not yet implemented (disclosed gaps)

This module does not itself select, rank, or route to any worker — it
only answers a single yes/no eligibility question for one already-named
`(worker_id, role, runtime_profile)`. A real router, and a registry that
maps each `ProductionRole` to its own current policy version
automatically (so a caller need not already know it), remain future
work.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.role_qualification import (
    ProductionRole,
    RoleEvaluationIdentity,
    RoleQualificationOutcome,
)
from code_slayer.workers.security_baseline import (
    BASELINE_VERSION,
    RuntimeProfileIdentity,
    SecurityBaselineOutcome,
)


@dataclass(frozen=True)
class EligibilityDecision:
    eligible: bool
    reason: str
    security_certificate_id: str | None = None
    role_certificate_id: str | None = None


def _deny(reason: str, **kwargs) -> EligibilityDecision:
    return EligibilityDecision(False, reason, **kwargs)


def _profile_from_certificate(certificate) -> RuntimeProfileIdentity:
    """Rebuild the COMMON runtime identity a certificate recorded.
    Historical `runtime_config_fingerprint` is preserved on the object
    but is not part of `.matches()` — v1 hashes are never reinterpreted
    as v2 common-runtime identity."""
    return RuntimeProfileIdentity(
        model_tag=certificate.model_tag,
        model_digest=certificate.model_digest,
        endpoint=certificate.endpoint,
        runtime_version=certificate.runtime_version,
        normalizer_id=certificate.normalizer_id,
        normalizer_version=certificate.normalizer_version,
        runtime_config_fingerprint=certificate.runtime_config_fingerprint,
        runtime_identity_fingerprint=certificate.runtime_identity_fingerprint,
    )


def _matching_security_certificate(certificates, runtime_profile):
    """The first (most recent, by the repo's own ordering) Baseline
    Security certificate whose recorded COMMON runtime identity matches
    `runtime_profile` exactly, or `None`."""
    for certificate in certificates:
        if _profile_from_certificate(certificate).matches(runtime_profile):
            return certificate
    return None


def _matching_role_certificate(certificates, runtime_profile, role_evaluation):
    """The first (most recent) role certificate whose recorded COMMON
    runtime identity matches `runtime_profile` AND whose recorded
    role/evaluation fingerprint matches `role_evaluation`. A NULL
    `role_evaluation_fingerprint` never wildcards a current fully
    specified evaluation profile."""
    expected = role_evaluation.role_evaluation_fingerprint
    for certificate in certificates:
        if not _profile_from_certificate(certificate).matches(runtime_profile):
            continue
        recorded = certificate.role_evaluation_fingerprint
        if recorded is None or recorded != expected:
            continue
        return certificate
    return None


def evaluate_production_eligibility(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    role: ProductionRole,
    runtime_profile: RuntimeProfileIdentity,
    role_evaluation: RoleEvaluationIdentity,
    expected_role_policy_version: str,
) -> EligibilityDecision:
    """The one production-eligibility gate a future router should query
    instead of deciding trust/qualification/security for itself. See the
    module docstring for the full rule set, including "Policy/version
    staleness" for `expected_role_policy_version`. Read-only: never opens
    a write transaction, never mutates any durable state, and never
    accepts a caller-supplied verdict for either certificate kind — both
    are loaded here from durable, backend-owned evidence. Identity
    fingerprints are taken from caller-verified `runtime_profile` /
    `role_evaluation` objects, never from model output."""
    if not isinstance(worker_id, str) or not worker_id:
        return _deny("malformed_eligibility_request")
    if not isinstance(role, ProductionRole):
        return _deny("malformed_eligibility_request")
    if not isinstance(runtime_profile, RuntimeProfileIdentity):
        return _deny("malformed_eligibility_request")
    if not isinstance(role_evaluation, RoleEvaluationIdentity):
        return _deny("malformed_eligibility_request")
    if (
        not isinstance(expected_role_policy_version, str)
        or not expected_role_policy_version.strip()
    ):
        return _deny("malformed_eligibility_request")
    if not runtime_profile.is_fully_specified:
        return _deny("insufficient_runtime_profile_identity")
    if not role_evaluation.is_fully_specified:
        return _deny("insufficient_role_evaluation_identity")
    if role_evaluation.role != role:
        return _deny("role_evaluation_role_mismatch")
    if role_evaluation.policy_version != expected_role_policy_version:
        return _deny("role_evaluation_policy_version_mismatch")
    if (
        role_evaluation.runtime_identity_fingerprint
        != runtime_profile.runtime_identity_fingerprint
    ):
        return _deny("role_evaluation_runtime_identity_mismatch")

    if WorkersRepo(conn).get(worker_id) is None:
        return _deny("unknown_worker")

    security_certificates = BaselineSecurityCertificatesRepo(conn).list_for_worker(worker_id)
    if not security_certificates:
        return _deny("no_baseline_security_certificate")
    security_certificate = _matching_security_certificate(
        security_certificates,
        runtime_profile,
    )
    if security_certificate is None:
        return _deny("baseline_security_certificate_profile_mismatch")
    if security_certificate.baseline_version != BASELINE_VERSION:
        return _deny(
            "baseline_security_certificate_policy_version_stale",
            security_certificate_id=security_certificate.certificate_id,
        )

    # Security is decided completely before the role certificate is ever
    # looked up -- a hard disqualifier or a plain security FAIL denies
    # outright, regardless of what role evidence exists.
    if security_certificate.outcome == SecurityBaselineOutcome.HARD_DISQUALIFIED.value:
        return _deny(
            "security_hard_disqualifier",
            security_certificate_id=security_certificate.certificate_id,
        )
    if security_certificate.outcome != SecurityBaselineOutcome.PASS.value:
        return _deny(
            "security_baseline_fail",
            security_certificate_id=security_certificate.certificate_id,
        )

    role_certificates = RoleCertificatesRepo(conn).list_for_worker_role(worker_id, role.value)
    if not role_certificates:
        return _deny(
            "no_role_certificate",
            security_certificate_id=security_certificate.certificate_id,
        )
    role_certificate = _matching_role_certificate(
        role_certificates,
        runtime_profile,
        role_evaluation,
    )
    if role_certificate is None:
        # Distinguish common-runtime mismatch from evaluation-profile
        # mismatch so a Planner budget change cannot be confused with a
        # different model/runtime.
        common_match = _matching_security_certificate(role_certificates, runtime_profile)
        reason = (
            "role_certificate_evaluation_profile_mismatch"
            if common_match is not None
            else "role_certificate_profile_mismatch"
        )
        return _deny(
            reason,
            security_certificate_id=security_certificate.certificate_id,
        )
    if role_certificate.policy_version != expected_role_policy_version:
        return _deny(
            "role_certificate_policy_version_stale",
            security_certificate_id=security_certificate.certificate_id,
            role_certificate_id=role_certificate.certificate_id,
        )
    if role_certificate.outcome != RoleQualificationOutcome.PASS.value:
        return _deny(
            "role_qualification_fail",
            security_certificate_id=security_certificate.certificate_id,
            role_certificate_id=role_certificate.certificate_id,
        )

    return EligibilityDecision(
        True,
        "eligible",
        security_certificate_id=security_certificate.certificate_id,
        role_certificate_id=role_certificate.certificate_id,
    )
