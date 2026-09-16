"""Planner role certification boundary: the code-owned decision point
that turns completed `planning.qualification` evidence into a durable
`ProductionRole.PLANNER` role certificate (Role Qualification
Certification foundation).

## Qualification evidence != certificate

`planning.qualification` is, by its own module docstring, pure evidence:
"it never writes durable state, never selects a production planner, and
never grants trust, permission, or policy authority to anything." This
module is the one place that bridges that evidence across into the
durable, backend-owned `workers.role_qualification` registry — it is
the ONLY caller in this codebase authorized to turn a `planning.
qualification.QualificationAttemptResult` sequence into a
`workers.role_qualification.record_role_certificate()` call. A model
never self-certifies: nothing here ever reads a model's own claim about
itself, only whatever `planning.qualification` already deterministically
classified from structural inspection of its actual, exact protocol
output (never prose recovery — see that module's own docstring).

`workers.role_qualification` deliberately does not import `planning`
(the reverse dependency direction is established throughout this
codebase — `planning.worker_planner`/`planning.qualification` already
import from `workers`, never vice versa). This module is what makes that
possible: it lives in `planning`, imports both `planning.qualification`
and `workers.role_qualification`, and translates between them — the
generic registry never needs to know Planner-specific evidence shapes.

## Certification policy: strict, no partial credit

Every `QualificationAttemptResult` in the supplied evidence must have
outcome `PASS_FIRST_TRY` or `PASS_AFTER_FEEDBACK` for the certificate to
be `PASS` — mirrors `workers.conformance`/`workers.promotion`'s own "every
required case in this run must pass" strictness and this codebase's "no
presentation score overrides a hard failure" posture. A single failing
instance anywhere in the evidence denies the whole certificate; there is
no partial-credit averaging, and no threshold tuned to make any specific
model pass. `bounded_correction` evidence (`QualificationOutcome.
PASS_AFTER_FEEDBACK`) is real, legitimate qualification evidence exactly
as `planning.qualification`'s own module docstring intends it to be — it
is never treated as a failure — but it remains distinguishable from
strict first-pass success in the recorded `classification`: a
certificate is only classified `"PASS_FIRST_TRY"` when EVERY supplied
instance passed on its first attempt; if even one needed a correction,
the whole certificate is classified `"PASS_AFTER_FEEDBACK"` instead, so
this distinction is never silently discarded.

A `FAIL_POLICY`/`FAIL_SCOPE`/`FAIL_CAPABILITY`/`FAIL_RUNTIME`/`FAIL_
TRANSPORT_TIMEOUT`/`OUTPUT_BUDGET_EXHAUSTED`/`INVALID_ENVIRONMENT`
outcome anywhere denies the certificate (`RoleQualificationOutcome.
FAIL`), and the certificate's `classification` records exactly which
`QualificationOutcome` (the first failing one encountered, in supplied
order) drove that denial — the richer distinction survives into durable
evidence rather than being flattened to a bare "FAIL".

## This function never certifies from incomplete or ambiguous evidence

Refuses outright (no certificate is ever recorded — this is a
certification-boundary-level refusal to judge, not itself a `FAIL`
verdict about the model) when:

- `results` is empty
- `early_stopped=True` was reported by `run_corrected_planner_case()`
  (repeated consecutive transport failures — the evaluation could not
  even complete, so there is no complete evidence to certify from)
- any instance's `provenance` is empty (it was run with `context_profile
  =None`/`unsafe_allow_unverified_environment=True` — `planning.
  qualification`'s own "no verified profile" escape hatch; evidence
  produced that way was never bound to a specific, verified runtime and
  can never be certified against one)
- the `(model_tag, model_digest, endpoint, runtime_version)` recorded in
  every attempt's `AttemptProvenance`, across every instance, does not
  agree exactly — ambiguous evidence about which runtime was actually
  tested is never resolved by guessing one
- the agreed-upon runtime profile is not fully specified (`workers.
  security_baseline.RuntimeProfileIdentity.is_fully_specified`) — see
  that property's own docstring for why a loosely-specified profile is
  never treated as a strong enough production binding

## Evidence reference

`planning.qualification` durably stores nothing of its own, so this
module builds `evidence_ref` itself: a deterministic SHA-256 fingerprint
over each instance's outcome, attempt outcomes, and each attempt's
existing, already-bounded `AttemptProvenance.request_fingerprint` values
— never raw prompt/model text (every value hashed into it was already a
bounded fingerprint or a fixed enum value before this function ever saw
it)."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from code_slayer.planning.qualification import QualificationAttemptResult, QualificationOutcome
from code_slayer.workers.role_qualification import (
    ProductionRole,
    RoleCertificationResult,
    RoleQualificationOutcome,
    record_role_certificate,
)
from code_slayer.workers.security_baseline import RuntimeProfileIdentity

# The fixed, code-owned identifier of the Planner certification policy
# this module currently implements -- mirrors `workers.conformance.
# SUITE_VERSION`/`workers.security_baseline.BASELINE_VERSION`'s own
# role: a persisted `policy_version` identifies the exact certification
# semantics a certificate was decided under, so a later change to what
# "Planner-qualified" even means never gets silently applied to old
# evidence.
PLANNER_CERTIFICATION_POLICY_VERSION = "planner-certification-v1"

_PASS_OUTCOMES = frozenset({
    QualificationOutcome.PASS_FIRST_TRY, QualificationOutcome.PASS_AFTER_FEEDBACK,
})


def _deny(reason: str) -> RoleCertificationResult:
    return RoleCertificationResult(False, reason)


def _agreed_runtime_profile(
    results: tuple[QualificationAttemptResult, ...],
) -> RuntimeProfileIdentity | None:
    """`None` if any instance has no provenance at all, or if the
    provenance recorded across every attempt in every instance does not
    agree on exactly one `(model_tag, model_digest, endpoint,
    runtime_version)` tuple -- see the module docstring."""
    identities: set[tuple[str, str | None, str | None, str | None]] = set()
    for result in results:
        if not result.provenance:
            return None
        for attempt in result.provenance:
            identities.add(
                (attempt.model_tag, attempt.model_digest, attempt.endpoint,
                 attempt.runtime_version),
            )
    if len(identities) != 1:
        return None
    model_tag, model_digest, endpoint, runtime_version = next(iter(identities))
    try:
        return RuntimeProfileIdentity(
            model_tag=model_tag, model_digest=model_digest, endpoint=endpoint,
            runtime_version=runtime_version,
        )
    except ValueError:
        return None


def _evidence_fingerprint(results: tuple[QualificationAttemptResult, ...]) -> str:
    """A deterministic, leak-free reference to exactly this evidence --
    see the module docstring's "Evidence reference" section. Every value
    folded in here is already either a fixed enum value or a bounded
    sha256 fingerprint `planning.qualification` itself produced; no raw
    prompt/model text is ever included."""
    payload = {
        "instances": [
            {
                "qualification_class": result.qualification_class,
                "outcome": result.outcome.value,
                "attempt_outcomes": [attempt.outcome.value for attempt in result.attempts],
                "attempt_fingerprints": [
                    attempt.request_fingerprint for attempt in result.provenance
                ],
            }
            for result in results
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8"),
    ).hexdigest()


def _classify(
    results: tuple[QualificationAttemptResult, ...],
) -> tuple[RoleQualificationOutcome, str]:
    """The strict, no-partial-credit certification rule -- see the
    module docstring's "Certification policy" section."""
    failing = [result for result in results if result.outcome not in _PASS_OUTCOMES]
    if failing:
        return RoleQualificationOutcome.FAIL, failing[0].outcome.value
    if all(result.outcome == QualificationOutcome.PASS_FIRST_TRY for result in results):
        return RoleQualificationOutcome.PASS, QualificationOutcome.PASS_FIRST_TRY.value
    return RoleQualificationOutcome.PASS, QualificationOutcome.PASS_AFTER_FEEDBACK.value


def certify_planner_from_qualification(
    conn: sqlite3.Connection, *, worker_id: str,
    results: tuple[QualificationAttemptResult, ...], early_stopped: bool,
    now_fn=None,
) -> RoleCertificationResult:
    """Decide whether a completed Planner qualification run
    (`planning.qualification.run_corrected_planner_case()`'s own return
    shape: `(results, early_stopped)`) is sufficient to durably certify
    `worker_id` for `ProductionRole.PLANNER`, and record that decision
    through `workers.role_qualification.record_role_certificate()` if
    so. See the module docstring for the complete policy.

    Never mutates `planning`'s own state (there is none to mutate --
    qualification evidence is produced fresh by the caller and passed in
    here already complete) and never re-invokes the planner itself; this
    function performs no inference of its own."""
    if not isinstance(results, tuple) or not results:
        return _deny("empty_qualification_evidence")
    if not all(isinstance(result, QualificationAttemptResult) for result in results):
        return _deny("malformed_qualification_evidence")
    if early_stopped:
        return _deny("qualification_run_early_stopped_insufficient_evidence")

    runtime_profile = _agreed_runtime_profile(results)
    if runtime_profile is None:
        return _deny("ambiguous_or_unverified_runtime_profile_in_evidence")
    if not runtime_profile.is_fully_specified:
        return _deny("insufficient_runtime_profile_identity")

    outcome, classification = _classify(results)
    evidence_ref = _evidence_fingerprint(results)
    reason = f"planner_qualification_{len(results)}_instance(s)_{classification.lower()}"

    kwargs = {} if now_fn is None else {"now_fn": now_fn}
    return record_role_certificate(
        conn, worker_id=worker_id, role=ProductionRole.PLANNER,
        runtime_profile=runtime_profile, policy_version=PLANNER_CERTIFICATION_POLICY_VERSION,
        outcome=outcome, classification=classification, evidence_ref=evidence_ref, reason=reason,
        **kwargs,
    )
