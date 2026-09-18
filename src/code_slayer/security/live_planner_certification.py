"""Production live Planner role certification runner (H.2).

This module is the production bridge, mirroring `security.
live_certification`'s own role for Baseline Security exactly:

    live Ollama runtime verification
            v
    construct exact RuntimeProfileIdentity + RoleEvaluationIdentity
            v
    verify a matching, current PRODUCTION Baseline Security certificate
    (via the existing `workers.production_eligibility` evaluator --
    never a third reimplementation of that matching logic)
            v
    run the fixed, code-owned qualification task suite through the
    EXISTING `planning.qualification.run_corrected_planner_case()`
            v
    `planning.planner_certification.certify_planner_from_qualification()`

It never accepts a caller-supplied `WorkerAdapter`, qualification
result, evidence_ref, outcome, classification, or fingerprint. Those
values are established here, from live-verified config and the
existing qualification/certification machinery, and nowhere else.

## Reuse, not a second harness

This module does not classify a single response, does not implement
retry/self-correction, does not validate structured-output schema or
scope/policy, and does not decide the PASS/FAIL certification rule.
Every one of those already exists and is reused unchanged:

- retry/self-correction, task-relevance, and per-attempt provenance:
  `planning.qualification.run_corrected_planner_case()`
- structured-output parsing and transport bridging:
  `planning.worker_planner.WorkerAdapterPlanner`
- the strict, no-partial-credit certification rule, evidence
  persistence, and the actual `worker_role_certificates` write:
  `planning.planner_certification.certify_planner_from_qualification()`
  (which itself calls `workers.role_qualification.
  record_role_certificate()` -- the ONLY function that ever inserts a
  role-certificate row)

This module's only real job is deciding what live-verified inputs to
hand those existing functions, and refusing to hand them anything at
all when the pre-conditions below are not met.

## Direct-to-PRODUCTION, unlike Baseline Security's VALIDATION/PRODUCTION
## split

`worker_role_certificates` has never had a VALIDATION/PRODUCTION split
-- that is specific to `worker_baseline_security_certificates` (added
by H.1, precisely because Baseline Security previously had no
production-issuance path at all). A role certificate has always been a
single, production-only authority table. This module therefore calls
`certify_planner_from_qualification()` directly against the PRODUCTION
connection and the PRODUCTION content store -- there is no separate
"promote" step for a Planner role certificate, and this module never
introduces one.

## Pre-conditions, all fail-closed, all before any model call

1. the worker exists in PRODUCTION.
2. the live Ollama runtime is re-probed right now
   (`security.live_certification.verify_ollama_runtime`) against the
   server-owned, config-derived `expected` -- a changed digest or
   runtime version fails closed here.
3. the CURRENT common runtime identity is rebuilt from that verified
   probe and must recompute to exactly
   `expected.expected_runtime_identity_fingerprint`.
4. a Planner `RoleEvaluationIdentity` is built from the caller-supplied
   `role_target` (server-owned persistent config; see `config.bindings.
   planner_role_evaluation_target_from_worker`) bound to that same
   verified runtime identity.
5. `workers.production_eligibility.evaluate_production_eligibility()`
   is consulted with that identity pair -- the SAME evaluator every
   other eligibility decision in this codebase uses, never a
   reimplementation of certificate matching. Before a role certificate
   exists, that call always denies (there is nothing to be eligible
   for yet); what matters here is WHICH reason it denies with. A
   baseline-layer reason (`no_baseline_security_certificate`,
   `baseline_security_certificate_profile_mismatch`,
   `baseline_security_certificate_policy_version_stale`,
   `security_hard_disqualifier`, `security_baseline_fail`, or any
   malformed/identity reason) fails this module closed -- a hard
   Security disqualifier in particular can never be outweighed by
   qualification evidence, exactly like `workers.
   production_eligibility`'s own "hard disqualifiers always win" rule.
   A role-layer reason (`no_role_certificate`,
   `role_certificate_profile_mismatch`,
   `role_certificate_evaluation_profile_mismatch`,
   `role_certificate_policy_version_stale`, `role_qualification_fail`)
   -- or `eligible=True` outright (re-certifying an already-qualified
   worker) -- is the expected, proceed-past state: that is precisely
   the gap this module exists to fill.
6. the live Ollama runtime is re-probed a SECOND time immediately
   before certifying, closing the gap between the qualification run
   (which can take a while -- real model calls) and the PRODUCTION
   write, mirroring `live_certification.
   certify_live_baseline_security`'s own pre-record re-probe.

Only once every one of those holds does this module ever call the
model.

## The security/runtime layer vs the role layer -- a semantic gate, not
## a reason-specific one

Pre-condition 5 above is deliberately interpreted, never reason-listed
by accident: `ROLE_LAYER_ELIGIBILITY_REASONS` names every reason
`evaluate_production_eligibility()` can return once Baseline Security
has ALREADY passed and only the role side remains unresolved
(`no_role_certificate`, `role_certificate_profile_mismatch`,
`role_certificate_evaluation_profile_mismatch`,
`role_certificate_policy_version_stale`, `role_qualification_fail`) --
every one of these, including a PRIOR Planner FAIL, is expected,
recertifiable state, never a reason to refuse a fresh attempt. Every
OTHER denial reason (`no_baseline_security_certificate`,
`baseline_security_certificate_profile_mismatch`,
`baseline_security_certificate_policy_version_stale`,
`security_hard_disqualifier`, `security_baseline_fail`, or any
malformed/identity reason) is a security/runtime-layer blocker and
fails this module closed. `decision.eligible is True` (an already-
valid PASS) also proceeds -- see "Recertification policy" below. This
distinction is checked by membership in that one set, not by comparing
against a single hardcoded reason string, specifically so a stale or
failed ROLE certificate can never be mistaken for -- or accidentally
coded as -- permission to skip the Baseline Security check.

## Recertification policy (deliberate choice)

When `evaluate_production_eligibility()` already reports
`eligible=True` for this exact worker/runtime/role-evaluation identity
(a currently-valid PASS role certificate already exists), this module
does NOT short-circuit to a no-op. It proceeds through the full live
qualification suite exactly as for any other request and records a
fresh, independent certificate row -- append-only, exactly like every
other certificate table in this codebase (`workers.role_qualification`'s
own module docstring). An operator-triggered "Start Planner
certification" is always a real recertification, never silently
downgraded to "already certified, nothing to do."

## Crash boundary: PRODUCTION certificate write vs VALIDATION run
## terminal update (inherited limitation, not introduced by H.2)

This module's own certificate write (via `certify_planner_from_
qualification()` -> `record_role_certificate()`) and the durable RUN
row's terminal state (`security.certification_service.
_execute_claimed_planner_run()`'s own `finish_in_transaction()` call,
against the SEPARATE VALIDATION `certification_runs` table/database)
are two INDEPENDENT commits, not one atomic transaction -- the same
two-commit shape `execute_claimed_run()` already has for Baseline
Security. If the process crashes between them, the PRODUCTION role
certificate is already durable and real (a worker can already become
eligible), but the VALIDATION run row is left permanently `RUNNING`.

This is a genuine gap, but NOT a duplicate-certificate risk:
`store.certification_runs_repo.CLAIMABLE_STATES = ("QUEUED",)` and
`claimable_ids()` only ever selects `state = 'QUEUED'` rows -- nothing
in this codebase re-discovers or re-claims a row stranded in
`RUNNING` (there is no lease/liveness sweep for `certification_runs`,
unlike `finalization.dispatcher.TaskLifecycleExecutor`'s restart
recovery for tasks). A stranded `RUNNING` row is therefore NEVER
automatically re-executed, so a restart can never silently mint a
second certificate "from the same run." The actual, disclosed cost is
availability, not authority: `active_for_worker()` treats `RUNNING` as
active, so `CertificationConflict` blocks BOTH a new preflight and a
new start for that `(worker_id, kind)` pair indefinitely, until an
operator manually repairs the stranded row -- no such repair tool
exists today for `certification_runs`. The worker's actual current
certificate/eligibility state remains correct and queryable throughout
(`GET /api/certification/workers/{id}` reads `roles.PLANNER`/
`production_eligibility` directly from the certificate tables, never
from the stuck run row) -- only the specific run's own progress display
and the ability to start a NEW run are affected.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from code_slayer.planning.planner import PlannerRequest
from code_slayer.planning.planner_certification import (
    RoleCertificationResult,
    certify_planner_from_qualification,
)
from code_slayer.planning.qualification import (
    QualificationAttemptResult,
    QualificationOutcome,
    RuntimeContextProfile,
    run_corrected_planner_case,
)
from code_slayer.planning.worker_planner import (
    WorkerAdapterPlanner,
    build_default_normalizer_registry,
)
from code_slayer.security.live_certification import (
    LiveOllamaRuntimeExpectation,
    verify_ollama_runtime,
)
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.openai_compatible_adapter import (
    OpenAICompatibleAdapter,
    OpenAICompatibleConfig,
)
from code_slayer.workers.production_eligibility import evaluate_production_eligibility
from code_slayer.workers.protocol_normalization import ToolProtocolNormalizerRegistry
from code_slayer.workers.role_qualification import (
    ProductionRole,
    RoleEvaluationIdentity,
    RoleQualificationOutcome,
    role_evaluation_identity_from_config,
)
from code_slayer.workers.security_baseline import (
    RuntimeProfileIdentity,
    runtime_profile_identity_from_config,
)

# The fixed, code-owned identifier of the live qualification task suite
# this module currently runs -- mirrors `security.evaluation.
# EVALUATION_SUITE_VERSION`/`workers.conformance.SUITE_VERSION`'s own
# role: a later change to what "the Planner live suite" even means
# bumps this identifier rather than silently reinterpreting old runs.
# v1 is deliberately small (two self-contained, low-ambiguity,
# read-only-flavored tasks, one repetition each) to keep real model-call
# volume bounded; widening it never requires touching the certification
# or classification logic above.
PLANNER_LIVE_QUALIFICATION_SUITE_VERSION = "planner-live-qualification-v1"

ROLE_LAYER_ELIGIBILITY_REASONS = frozenset(
    {
        "no_role_certificate",
        "role_certificate_profile_mismatch",
        "role_certificate_evaluation_profile_mismatch",
        "role_certificate_policy_version_stale",
        "role_qualification_fail",
    }
)


@dataclass(frozen=True)
class PlannerLiveQualificationTask:
    """One fixed, code-owned qualification task instance. Never a
    caller/client-supplied prompt."""

    qualification_class: str
    request: PlannerRequest
    repetitions: int = 1
    allowed_scope: tuple[str, ...] | None = None


def _live_qualification_suite() -> tuple[PlannerLiveQualificationTask, ...]:
    return (
        PlannerLiveQualificationTask(
            qualification_class="LIVE-PLANNER-001",
            request=PlannerRequest(
                original_request=(
                    "Add a read-only endpoint that returns the current server time."
                ),
            ),
        ),
        PlannerLiveQualificationTask(
            qualification_class="LIVE-PLANNER-002",
            request=PlannerRequest(
                original_request="Add a read-only health-check endpoint.",
            ),
        ),
    )


@dataclass(frozen=True)
class LivePlannerCertificationResult:
    """Bounded result of one live Planner certification attempt. Never
    carries raw model text, secrets, or HTTP bodies -- mirrors
    `LiveSecurityCertificationResult`."""

    ok: bool
    reason: str
    runtime_identity_fingerprint: str | None = None
    role_evaluation_fingerprint: str | None = None
    outcome: RoleQualificationOutcome | None = None
    classification: str | None = None
    certificate_id: str | None = None
    evidence_ref: str | None = None


def _deny(reason: str, **kwargs) -> LivePlannerCertificationResult:
    return LivePlannerCertificationResult(False, reason, **kwargs)


def _verify_live_identity(
    expected: LiveOllamaRuntimeExpectation,
) -> tuple[RuntimeProfileIdentity, str] | tuple[None, str]:
    """Re-probe live Ollama and rebuild the CURRENT common runtime
    identity. Returns `(profile, "")` on success, or `(None, reason)`
    on any failure -- never a partial/best-effort identity."""
    try:
        runtime_version, model_digest = verify_ollama_runtime(expected)
    except ValueError as exc:
        return None, (str(exc) if str(exc) else "runtime_probe_unavailable")
    try:
        profile = runtime_profile_identity_from_config(
            model_tag=expected.model_tag,
            model_digest=model_digest,
            endpoint=expected.openai_base_url,
            runtime_version=runtime_version,
            effective_context_tokens=expected.effective_context_tokens,
            temperature=float(expected.temperature),
            normalizer_id=expected.normalizer_id,
            normalizer_version=expected.normalizer_version,
        )
    except (TypeError, ValueError):
        return None, "insufficient_runtime_profile_identity"
    if profile.runtime_identity_fingerprint != expected.expected_runtime_identity_fingerprint:
        return None, "runtime_identity_fingerprint_mismatch"
    return profile, ""


def check_planner_certification_eligible(
    production_conn: sqlite3.Connection,
    *,
    worker_id: str,
    runtime_profile: RuntimeProfileIdentity,
    role_evaluation: RoleEvaluationIdentity,
    expected_role_policy_version: str,
):
    """Consult the EXISTING `evaluate_production_eligibility()` evaluator
    and classify its denial as a baseline-layer block (fails this
    module closed) or a role-layer/expected-gap state (proceed). Never
    reimplements certificate matching -- see the module docstring's
    pre-condition 5. Returns the raw `EligibilityDecision` so a caller
    can also surface `security_certificate_id` for provenance."""
    return evaluate_production_eligibility(
        production_conn,
        worker_id=worker_id,
        role=ProductionRole.PLANNER,
        runtime_profile=runtime_profile,
        role_evaluation=role_evaluation,
        expected_role_policy_version=expected_role_policy_version,
    )


def certify_live_planner_role(
    production_conn: sqlite3.Connection,
    *,
    worker_id: str,
    blobs_dir: Path | str,
    expected: LiveOllamaRuntimeExpectation,
    output_token_budget: int,
    tool_choice_enforcement: str,
    policy_version: str,
) -> LivePlannerCertificationResult:
    """Verify live runtime + PRODUCTION Baseline Security eligibility,
    run the fixed live qualification suite, and record exactly one
    Planner role certificate from the reread evidence -- directly
    against PRODUCTION. See the module docstring for the complete,
    ordered fail-closed check list. Never mutates `worker_trust_events`
    /`permission_grants`/any Baseline Security certificate table.
    """
    if not isinstance(worker_id, str) or not worker_id:
        return _deny("malformed_planner_certification_request")
    if not isinstance(expected, LiveOllamaRuntimeExpectation):
        return _deny("malformed_planner_certification_request")
    if not isinstance(blobs_dir, (str, Path)) or not str(blobs_dir).strip():
        return _deny("missing_durable_qualification_evidence")
    if WorkersRepo(production_conn).get(worker_id) is None:
        return _deny("unknown_worker")

    runtime_profile, reason = _verify_live_identity(expected)
    if runtime_profile is None:
        return _deny(reason)
    fingerprint = runtime_profile.runtime_identity_fingerprint

    try:
        role_evaluation = role_evaluation_identity_from_config(
            role=ProductionRole.PLANNER,
            runtime_identity_fingerprint=fingerprint,
            output_token_budget=output_token_budget,
            tool_choice_enforcement=tool_choice_enforcement,
            policy_version=policy_version,
        )
    except (TypeError, ValueError):
        return _deny(
            "insufficient_role_evaluation_identity",
            runtime_identity_fingerprint=fingerprint,
        )

    decision = check_planner_certification_eligible(
        production_conn,
        worker_id=worker_id,
        runtime_profile=runtime_profile,
        role_evaluation=role_evaluation,
        expected_role_policy_version=policy_version,
    )
    if not decision.eligible and decision.reason not in ROLE_LAYER_ELIGIBILITY_REASONS:
        return _deny(
            decision.reason,
            runtime_identity_fingerprint=fingerprint,
            role_evaluation_fingerprint=role_evaluation.role_evaluation_fingerprint,
        )

    normalizer_registry: ToolProtocolNormalizerRegistry | None = None
    if expected.normalizer_id is not None:
        normalizer_registry = build_default_normalizer_registry()

    config = OpenAICompatibleConfig(
        base_url=expected.openai_base_url,
        model=expected.model_tag,
        timeout=expected.timeout,
        api_key=expected.api_key,
        max_response_bytes=expected.max_response_bytes,
        temperature=float(expected.temperature),
    )
    adapter = OpenAICompatibleAdapter(config)
    planner = WorkerAdapterPlanner(
        adapter,
        task_id=f"planner-certification:{worker_id}",
        normalizer_registry=normalizer_registry,
        normalizer_id=expected.normalizer_id,
        normalizer_version=expected.normalizer_version,
    )

    context_profile = RuntimeContextProfile(
        model_tag=expected.model_tag,
        effective_context_tokens=expected.effective_context_tokens,
        output_token_budget=output_token_budget,
        model_digest=expected.model_digest,
        endpoint=expected.openai_base_url,
        runtime_version=expected.runtime_version,
        tool_choice_enforcement=tool_choice_enforcement,
        output_budget_enforcement_verified=False,
        normalizer_id=expected.normalizer_id,
        normalizer_version=expected.normalizer_version,
        temperature=float(expected.temperature),
    )

    combined_results: list[QualificationAttemptResult] = []
    any_early_stopped = False
    for task in _live_qualification_suite():
        results, early_stopped = run_corrected_planner_case(
            planner,
            task.request,
            qualification_class=task.qualification_class,
            repetitions=task.repetitions,
            context_profile=context_profile,
            allowed_scope=task.allowed_scope,
        )
        combined_results.extend(results)
        if early_stopped:
            any_early_stopped = True
            break

    # `run_corrected_planner_case()`'s own early-stop threshold is
    # scoped to consecutive attempts WITHIN one task's own repetitions
    # (default 3) -- with this suite's per-task `repetitions=1`, no
    # single task call can ever reach it on its own, which would
    # otherwise let a fully-down runtime silently accumulate spurious
    # FAIL_RUNTIME/FAIL_TRANSPORT_TIMEOUT "evidence" across tasks and
    # get recorded as a real FAIL certificate. This is aggregation, not
    # a second classification: it only reads the `.outcome` each
    # instance was ALREADY classified with by the existing harness.
    # When EVERY instance in the whole suite was a transport failure
    # (nothing genuinely evaluated the worker's capability at all),
    # this is treated exactly like `run_corrected_planner_case()`'s own
    # early-stop -- a refusal to judge, never a certificate.
    transport_outcomes = frozenset(
        {QualificationOutcome.FAIL_TRANSPORT_TIMEOUT, QualificationOutcome.FAIL_RUNTIME},
    )
    if combined_results and all(r.outcome in transport_outcomes for r in combined_results):
        any_early_stopped = True

    # Close the gap between the (potentially slow, real-model-call)
    # qualification run and the PRODUCTION write: re-probe live Ollama
    # once more, mirroring `live_certification.
    # certify_live_baseline_security`'s own pre-record re-probe.
    _, reprobe_reason = _verify_live_identity(expected)
    if reprobe_reason:
        return _deny(
            "runtime_changed_during_qualification",
            runtime_identity_fingerprint=fingerprint,
            role_evaluation_fingerprint=role_evaluation.role_evaluation_fingerprint,
        )

    recorded: RoleCertificationResult = certify_planner_from_qualification(
        production_conn,
        worker_id=worker_id,
        results=tuple(combined_results),
        early_stopped=any_early_stopped,
        blobs_dir=blobs_dir,
    )
    if not recorded.ok or recorded.certificate is None:
        return _deny(
            recorded.reason,
            runtime_identity_fingerprint=fingerprint,
            role_evaluation_fingerprint=role_evaluation.role_evaluation_fingerprint,
        )

    return LivePlannerCertificationResult(
        True,
        recorded.reason,
        runtime_identity_fingerprint=fingerprint,
        role_evaluation_fingerprint=role_evaluation.role_evaluation_fingerprint,
        outcome=RoleQualificationOutcome(recorded.certificate.outcome),
        classification=recorded.certificate.classification,
        certificate_id=recorded.certificate.certificate_id,
        evidence_ref=recorded.certificate.evidence_ref,
    )
