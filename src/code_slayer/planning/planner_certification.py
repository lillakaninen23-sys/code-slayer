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
- the `(model_tag, model_digest, endpoint, runtime_version,
  normalizer_id, normalizer_version, runtime_config_fingerprint)`
  recorded in every attempt's `AttemptProvenance`, across every
  instance, does not agree exactly — ambiguous evidence about which
  runtime was actually tested is never resolved by guessing one.
  Native-only (`normalizer_id is None`) is a different runtime identity
  from one that uses a compatibility normalizer; a certificate for one
  must never silently cover the other. A missing runtime-config
  fingerprint is a different identity from one that bound temperature,
  context capacity, and output-token budget; a pre-fingerprint
  certificate must never silently cover a fully-specified runtime.
- the agreed-upon runtime profile is not fully specified (`workers.
  security_baseline.RuntimeProfileIdentity.is_fully_specified`) — see
  that property's own docstring for why a loosely-specified profile is
  never treated as a strong enough production binding
- the canonical runtime-config spec reconstructed from provenance does
  not recompute to the agreed `runtime_config_fingerprint` (tampered or
  internally inconsistent evidence)
- durable qualification evidence cannot be persisted and re-read from
  `ContentStore` before issuance (missing store, kind mismatch, hash
  mismatch, or fingerprint mismatch on the persisted spec)

## Evidence reference

`planning.qualification` durably stores nothing of its own. This module
persists a bounded canonical JSON document via
`planning.qualification_evidence` *before* recording the certificate,
and stores that blob's content hash as `evidence_ref`. The document
contains the canonical `runtime-config-spec-v1` (so the fingerprint is
later recomputable from durable state), each instance outcome, and the
already-bounded `AttemptProvenance` records — never raw prompt or model
text. A one-way fingerprint without that document is not sufficient
forensic evidence; a document whose recomputed fingerprint does not
match the certificate's `runtime_config_fingerprint` is refused. Existing
certificates are never rewritten; their `evidence_ref` values stay as
originally recorded."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from code_slayer.planning.qualification import QualificationAttemptResult, QualificationOutcome
from code_slayer.planning.qualification_evidence import (
    QualificationEvidenceError,
    agreed_runtime_config_spec,
    build_planner_qualification_evidence_document,
    persist_planner_qualification_evidence,
    verify_runtime_config_fingerprint,
)
from code_slayer.store.content_store import ContentStore
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

_PASS_OUTCOMES = frozenset(
    {
        QualificationOutcome.PASS_FIRST_TRY,
        QualificationOutcome.PASS_AFTER_FEEDBACK,
    }
)


def _deny(reason: str) -> RoleCertificationResult:
    return RoleCertificationResult(False, reason)


def _agreed_runtime_profile(
    results: tuple[QualificationAttemptResult, ...],
) -> RuntimeProfileIdentity | None:
    """`None` if any instance has no provenance at all, or if the
    provenance recorded across every attempt in every instance does not
    agree on exactly one `(model_tag, model_digest, endpoint,
    runtime_version, normalizer_id, normalizer_version,
    runtime_config_fingerprint)` tuple -- see the module docstring."""
    identities: set[
        tuple[str, str | None, str | None, str | None, str | None, int | None, str | None]
    ] = set()
    for result in results:
        if not result.provenance:
            return None
        for attempt in result.provenance:
            identities.add(
                (
                    attempt.model_tag,
                    attempt.model_digest,
                    attempt.endpoint,
                    attempt.runtime_version,
                    attempt.normalizer_id,
                    attempt.normalizer_version,
                    attempt.runtime_config_fingerprint,
                ),
            )
    if len(identities) != 1:
        return None
    (
        model_tag,
        model_digest,
        endpoint,
        runtime_version,
        normalizer_id,
        normalizer_version,
        runtime_config_fingerprint,
    ) = next(iter(identities))
    try:
        return RuntimeProfileIdentity(
            model_tag=model_tag,
            model_digest=model_digest,
            endpoint=endpoint,
            runtime_version=runtime_version,
            normalizer_id=normalizer_id,
            normalizer_version=normalizer_version,
            runtime_config_fingerprint=runtime_config_fingerprint,
        )
    except ValueError:
        return None


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


def _persist_evidence(
    conn: sqlite3.Connection,
    blobs_dir: Path | str,
    *,
    results: tuple[QualificationAttemptResult, ...],
    runtime_profile: RuntimeProfileIdentity,
    classification: str,
) -> str:
    """Build, persist, and re-verify durable qualification evidence.
    Returns the content hash used as `evidence_ref`. Raises
    `QualificationEvidenceError` on any fail-closed condition."""
    if not isinstance(blobs_dir, (str, Path)) or not str(blobs_dir).strip():
        raise QualificationEvidenceError("missing_durable_qualification_evidence")
    fingerprint = runtime_profile.runtime_config_fingerprint
    if not isinstance(fingerprint, str) or not fingerprint:
        raise QualificationEvidenceError("runtime_config_fingerprint_mismatch")
    spec = agreed_runtime_config_spec(results)
    verify_runtime_config_fingerprint(spec, fingerprint)
    document = build_planner_qualification_evidence_document(
        results=results,
        runtime_config_spec=spec,
        runtime_config_fingerprint=fingerprint,
        policy_version=PLANNER_CERTIFICATION_POLICY_VERSION,
        classification=classification,
    )
    store = ContentStore(conn, blobs_dir)
    return persist_planner_qualification_evidence(
        store,
        document,
        expected_runtime_config_fingerprint=fingerprint,
    )


def certify_planner_from_qualification(
    conn: sqlite3.Connection,
    *,
    worker_id: str,
    results: tuple[QualificationAttemptResult, ...],
    early_stopped: bool,
    blobs_dir: Path | str,
    now_fn=None,
) -> RoleCertificationResult:
    """Decide whether a completed Planner qualification run
    (`planning.qualification.run_corrected_planner_case()`'s own return
    shape: `(results, early_stopped)`) is sufficient to durably certify
    `worker_id` for `ProductionRole.PLANNER`, and record that decision
    through `workers.role_qualification.record_role_certificate()` if
    so. See the module docstring for the complete policy.

    Durable qualification evidence is persisted to `blobs_dir` (the
    existing `ContentStore`) BEFORE the certificate row is written.
    Issuance refuses if that document cannot be stored and re-read with
    a recomputed runtime-config fingerprint matching the certificate.
    Never mutates `planning`'s own state and never re-invokes the
    planner itself; this function performs no inference of its own."""
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
    if runtime_profile.normalizer_id is None and any(
        attempt.tool_call_transport == "NORMALIZED"
        for result in results
        for attempt in result.provenance
    ):
        # A turn that actually used a compatibility decoder cannot be
        # certified as native-only -- the two identities must not be
        # silently confused.
        return _deny("normalized_transport_without_normalizer_identity")

    outcome, classification = _classify(results)
    try:
        evidence_ref = _persist_evidence(
            conn,
            blobs_dir,
            results=results,
            runtime_profile=runtime_profile,
            classification=classification,
        )
    except QualificationEvidenceError as exc:
        return _deny(exc.reason)

    reason = f"planner_qualification_{len(results)}_instance(s)_{classification.lower()}"

    kwargs = {} if now_fn is None else {"now_fn": now_fn}
    return record_role_certificate(
        conn,
        worker_id=worker_id,
        role=ProductionRole.PLANNER,
        runtime_profile=runtime_profile,
        policy_version=PLANNER_CERTIFICATION_POLICY_VERSION,
        outcome=outcome,
        classification=classification,
        evidence_ref=evidence_ref,
        reason=reason,
        **kwargs,
    )
