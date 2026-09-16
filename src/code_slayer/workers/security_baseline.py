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
  **That harness does not exist yet in this codebase** and is not built
  here — this is a disclosed, intentional gap, not an oversight. This
  module accepts an already-computed `SecurityBaselineOutcome` plus its
  supporting evidence reference, exactly the same "evidence, never
  authority" boundary `planning.qualification`'s own module docstring
  draws for planner-turn evaluation ("it never writes durable state,
  never selects a production planner, and never grants trust,
  permission, or policy authority to anything"). A future evaluation
  harness is responsible for actually producing a real outcome from
  observed behavior; this module is responsible for making sure a
  produced outcome can never be recorded, bound, or trusted incorrectly.
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
`model_digest`/`endpoint`/`runtime_version` — deliberately the same
identity vocabulary `planning.qualification.RuntimeContextProfile`
already established for "what runtime was actually verified," reused
here rather than inventing a second, differently-named identity shape
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

`model_digest`/`endpoint`/`runtime_version` remain optional here
deliberately — `record_baseline_certificate()` still accepts a
certificate bound only to `model_tag`, since that is sometimes the only
fact genuinely available at *evaluation* time, and this module's job is
recording evidence honestly, not demanding more identity than was
actually established. `RuntimeProfileIdentity.is_fully_specified`
exists for the separate, stricter question a *production* decision must
ask: `workers.production_eligibility` refuses to treat ANY certificate
as authoritative for a real eligibility decision unless the CURRENT
profile it is asked to check against has every field populated — a
loosely-specified profile (e.g. `model_tag` alone) could otherwise let
two meaningfully different runtimes silently share a certificate. This
keeps recording lenient/honest and production consultation strict,
without changing what this module itself accepts.

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

import json
import sqlite3
import uuid
from dataclasses import dataclass
from enum import StrEnum

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
    """The exact runtime/model/provider configuration one Baseline
    Security evaluation was actually run against — deliberately the same
    identity vocabulary `planning.qualification.RuntimeContextProfile`
    already established (`model_tag`/`model_digest`/`endpoint`/
    `runtime_version`), reused here rather than invented afresh. A
    caller-*verified* value, never introspected or assumed by this
    module itself (same posture `RuntimeContextProfile` already takes)."""

    model_tag: str
    model_digest: str | None = None
    endpoint: str | None = None
    runtime_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.model_tag, str) or not self.model_tag.strip():
            raise ValueError("model_tag must be a non-empty string")
        for name in ("model_digest", "endpoint", "runtime_version"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be a non-empty string or None")

    def matches(self, other: RuntimeProfileIdentity) -> bool:
        """Exact match on every field — `None` only ever matches `None`,
        never treated as a wildcard. This is what stops a certificate
        issued against a partially-described profile (e.g. no digest
        recorded) from being silently treated as covering a DIFFERENT,
        now-current profile that also happens to omit a digest for an
        unrelated reason."""
        if not isinstance(other, RuntimeProfileIdentity):
            return False
        return (
            self.model_tag == other.model_tag
            and self.model_digest == other.model_digest
            and self.endpoint == other.endpoint
            and self.runtime_version == other.runtime_version
        )

    @property
    def is_fully_specified(self) -> bool:
        """`True` only when every identity field is populated —
        `model_tag` alone (or any strict subset) is a legitimate binding
        for a *recorded* certificate (`record_baseline_certificate()`
        never required more, and this property does not retroactively
        change that), but it is NOT a strong enough binding for
        `workers.production_eligibility` to treat as authoritative for a
        real production decision: two meaningfully different runtimes
        (different digest, different endpoint, different runtime
        version) could otherwise share the same loosely-specified
        profile and be silently confused for each other. `workers.
        production_eligibility` refuses to evaluate eligibility at all
        against a `runtime_profile` for which this is `False` — unknown
        identity fails closed rather than wildcard-matching (see that
        module's own docstring)."""
        return None not in (self.model_tag, self.model_digest, self.endpoint, self.runtime_version)


@dataclass(frozen=True)
class SecurityCertificationResult:
    ok: bool
    reason: str
    certificate: WorkerBaselineSecurityCertificate | None = None


def _deny(reason: str) -> SecurityCertificationResult:
    return SecurityCertificationResult(False, reason)


def record_baseline_certificate(
    conn: sqlite3.Connection, *, worker_id: str, runtime_profile: RuntimeProfileIdentity,
    outcome: SecurityBaselineOutcome, evidence_ref: str, reason: str,
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
            certificate_id=certificate_id, worker_id=worker_id,
            baseline_version=BASELINE_VERSION, model_tag=runtime_profile.model_tag,
            model_digest=runtime_profile.model_digest, endpoint=runtime_profile.endpoint,
            runtime_version=runtime_profile.runtime_version, outcome=outcome.value,
            hard_disqualifiers_json=json.dumps([d.value for d in hard_disqualifiers]),
            evidence_ref=evidence_ref, reason=reason, issued_at=issued_at,
        )
        AuditWriter(conn).append(
            task_id=None, event_type=EventType.SECURITY_BASELINE_CERTIFICATE_RECORDED,
            actor_type="system", actor_id=worker_id,
            payload={
                "certificate_id": certificate_id, "worker_id": worker_id,
                "baseline_version": BASELINE_VERSION, "model_tag": runtime_profile.model_tag,
                "model_digest": runtime_profile.model_digest,
                "endpoint": runtime_profile.endpoint,
                "runtime_version": runtime_profile.runtime_version,
                "outcome": outcome.value,
                "hard_disqualifiers": [d.value for d in hard_disqualifiers],
                "evidence_ref": evidence_ref, "reason": reason,
            },
        )
        return SecurityCertificationResult(True, "certificate_recorded", certificate=certificate)
