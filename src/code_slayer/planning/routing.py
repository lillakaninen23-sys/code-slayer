"""H.4: durable, production-eligible, worker-bound Planner routing.

## What this module is, and is not

Before this module existed, `POST /api/plans` had no durable proof of
which worker/model actually supplied a planning turn's Planner, whether
that worker was `ACTIVE`, Planner-certified, or still eligible when
inference actually began — see `docs/ENGINEERING_PLANNING.md`'s own
"Known limitations" section (H.3's documented, deliberate gap). This
module is the ONE place that turns "current server-owned worker
configuration" into "the exact worker a new planning job is durably
bound to" (`select_planner_route()`), and the ONE place that later
proves a durably-bound job's route binding is still exactly valid
immediately before a Planner is ever constructed for it
(`revalidate_route_binding()`).

This module decides NO eligibility rules of its own — every PASS/FAIL/
certificate-matching decision is `workers.production_eligibility.
evaluate_production_eligibility()`, the same canonical evaluator
Certification Center already uses (`security.certification_service.
CertificationService._eligibility()`). This module only builds that
call's inputs from server-owned data (current persistent config,
`workers.security_baseline.runtime_profile_identity_from_config()`,
`workers.role_qualification.role_evaluation_identity_from_config()`)
and turns its output into a `PlannerRouteBinding` — never a second,
parallel eligibility implementation.

This module never talks Ollama/HTTP directly for SELECTION (candidate
enumeration is pure/local: current config + one canonical eligibility
call per candidate) — only `revalidate_route_binding()`'s live-runtime-
attestation step calls `security.live_certification.
verify_ollama_runtime()`, the same probe Certification Center already
uses.

## No client authority, ever

`PlannerRouteBinding` is derived exclusively from: current server-owned
persistent worker configuration, code-owned `RuntimeProfileIdentity`/
`RoleEvaluationIdentity` construction, the canonical eligibility
evaluator, and the durable certificate IDs that evaluator returns. No
model output, and no HTTP request field, ever contributes to routing
authority — `POST /api/plans`'s closed-set body parser
(`api.routes.body()`) already has no `worker_id`/`model`/
`certificate_id`/`runtime_identity_fingerprint` key in its spec, so a
client that supplies one fails closed with `invalid_fields` before this
module is ever reached.

## No ranking yet — fail closed on ambiguity

There is currently no code-owned Planner strength score or routing
priority. `select_planner_route()` collects every ELIGIBLE candidate
from current config; zero is `NO_ELIGIBLE_PLANNER_WORKER`, exactly one
is selected, and MORE than one is `MULTIPLE_ELIGIBLE_PLANNER_WORKERS` —
never a silent first-match/alphabetical/newest-certificate/arbitrary-
DB-order pick. A later routing-policy phase may introduce deliberate
ranking; this module deliberately does not guess at one now.

## Candidates come from CURRENT config, never stale DB history

Candidate enumeration is driven by `baseline_targets`/`role_targets`
(`api.service.RuntimeBindings.baseline_certification_targets`/
`.role_evaluation_targets`, themselves rebuilt fresh from
`config.bindings.runtime_bindings_from_config()` on every call) — never
a `SELECT * FROM workers` scan. H.3 deliberately preserves archived and
historical worker rows in the database forever; a historical worker no
longer present in current config can never become a routing candidate
here, regardless of what certificates it still holds on file.

## No reroute, ever

`select_planner_route()` is called exactly once per NEW job
(`planning.service.EngineeringPlanningService.create_job()`/
`.replan_job()`), before that job's durable row is even created.
`revalidate_route_binding()` is called again later, at claim/execution
time, but it only ever CHECKS the exact `worker_id` a job's own durable
`PlannerRouteBinding` already names — it never selects a different
candidate, and nothing in `planning.executor.PlanningJobExecutor` ever
calls `select_planner_route()` again for an already-queued job. If the
bound worker later becomes ineligible, the job fails closed; it is
never silently reassigned to a different, currently-eligible worker. A
newly created (or replanned) job may receive its own, independently
selected binding — see `store.migrations.
0019_planner_worker_routing`'s `planning_jobs_no_mutate_identity`
trigger for the structural (not merely conventional) enforcement of
this invariant.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from code_slayer.security.live_certification import verify_ollama_runtime
from code_slayer.store.workers_repo import WorkerLifecycleState, WorkersRepo
from code_slayer.workers.production_eligibility import evaluate_production_eligibility
from code_slayer.workers.role_qualification import (
    ProductionRole,
    role_evaluation_identity_from_config,
)
from code_slayer.workers.security_baseline import runtime_profile_identity_from_config

if TYPE_CHECKING:
    # Deferred: `security.certification_service` imports `planning.
    # planner_certification`/`planning.qualification_evidence`, and
    # `code_slayer.planning.__init__` eagerly imports `planning.service`
    # (which imports this module) -- a top-level import here would be a
    # genuine circular import through `config.bindings` -> `security`
    # (package init) -> `certification_service` -> `planning.
    # planner_certification` -> `planning` (package init) -> `planning.
    # service` -> this module. These two names are used only as type
    # annotations (see `from __future__ import annotations` above, which
    # already makes every annotation in this module a lazily-evaluated
    # string), so a `TYPE_CHECKING`-only import carries zero runtime
    # cost and breaks the cycle completely -- the same deferred-import
    # discipline `api.service.ApplicationService._compose_bindings()`/
    # `security.certification_service.CertificationService.
    # _promotion_availability()` already use for their own real
    # (non-type-only) cross-package imports.
    from code_slayer.security.certification_service import (
        BaselineCertificationTarget,
        RoleEvaluationTarget,
    )


@dataclass(frozen=True)
class PlannerRouteBinding:
    """The exact, durable, backend-selected Planner routing authority
    for one planning job — see the module docstring. Every field is a
    bounded, non-secret identifier (never an API key or raw runtime
    credential) safe to durably record and to expose over HTTP/WebUI as
    provenance."""

    worker_id: str
    runtime_identity_fingerprint: str
    role_evaluation_fingerprint: str
    security_certificate_id: str
    role_certificate_id: str
    output_token_budget: int
    tool_choice_enforcement: str
    planner_policy_version: str


class RoutingOutcome(StrEnum):
    SELECTED = "selected"
    NO_ELIGIBLE_PLANNER_WORKER = "no_eligible_planner_worker"
    MULTIPLE_ELIGIBLE_PLANNER_WORKERS = "multiple_eligible_planner_workers"


@dataclass(frozen=True)
class RouteSelectionResult:
    """`binding` is set only when `outcome is RoutingOutcome.SELECTED`.
    `candidate_worker_ids` is populated only for `MULTIPLE_ELIGIBLE_
    PLANNER_WORKERS` — diagnostic only, never itself routing authority,
    and never returned to an HTTP caller as anything more than an
    opaque `503` reason code."""

    outcome: RoutingOutcome
    binding: PlannerRouteBinding | None = None
    candidate_worker_ids: tuple[str, ...] = ()


def _planner_role_target(
    role_targets: tuple[RoleEvaluationTarget, ...], worker_id: str,
) -> RoleEvaluationTarget | None:
    for target in role_targets:
        if target.worker_id == worker_id and target.role == ProductionRole.PLANNER:
            return target
    return None


def _binding_for_target(
    conn: sqlite3.Connection,
    target: BaselineCertificationTarget,
    role_target: RoleEvaluationTarget,
) -> PlannerRouteBinding | None:
    """`None` if `target.worker_id` is not currently eligible for
    `ProductionRole.PLANNER` — see the module docstring for why this
    never reimplements `evaluate_production_eligibility()`'s own
    rules, only builds its inputs from server-owned config (mirroring
    `security.certification_service.CertificationService._eligibility()`'s
    own, already-established construction pattern — never imported
    directly, since that method is private to that module)."""
    expectation = target.expectation
    profile = runtime_profile_identity_from_config(
        model_tag=expectation.model_tag,
        model_digest=expectation.model_digest,
        endpoint=expectation.openai_base_url,
        runtime_version=expectation.runtime_version,
        effective_context_tokens=expectation.effective_context_tokens,
        temperature=float(expectation.temperature),
        normalizer_id=expectation.normalizer_id,
        normalizer_version=expectation.normalizer_version,
    )
    role_evaluation = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        output_token_budget=role_target.output_token_budget,
        tool_choice_enforcement=role_target.tool_choice_enforcement,
        policy_version=role_target.policy_version,
    )
    decision = evaluate_production_eligibility(
        conn,
        worker_id=target.worker_id,
        role=ProductionRole.PLANNER,
        runtime_profile=profile,
        role_evaluation=role_evaluation,
        expected_role_policy_version=role_target.policy_version,
    )
    if not decision.eligible:
        return None
    assert decision.security_certificate_id is not None
    assert decision.role_certificate_id is not None
    return PlannerRouteBinding(
        worker_id=target.worker_id,
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        role_evaluation_fingerprint=role_evaluation.role_evaluation_fingerprint,
        security_certificate_id=decision.security_certificate_id,
        role_certificate_id=decision.role_certificate_id,
        output_token_budget=role_target.output_token_budget,
        tool_choice_enforcement=role_target.tool_choice_enforcement,
        planner_policy_version=role_target.policy_version,
    )


def select_planner_route(
    conn: sqlite3.Connection,
    *,
    baseline_targets: tuple[BaselineCertificationTarget, ...],
    role_targets: tuple[RoleEvaluationTarget, ...],
) -> RouteSelectionResult:
    """The ONE place a NEW planning job's Planner worker is chosen. See
    the module docstring for the complete zero/one/multiple policy and
    the "no ranking yet" / "candidates come from current config"
    invariants. Read-only: never mutates `workers`/any certificate
    table/`planning_jobs`; the caller (`planning.service.
    EngineeringPlanningService.create_job()`/`.replan_job()`) durably
    records the returned `binding`, inside its own atomic transaction,
    only after this call already returned `SELECTED`."""
    candidates: list[PlannerRouteBinding] = []
    for target in baseline_targets:
        role_target = _planner_role_target(role_targets, target.worker_id)
        if role_target is None:
            continue
        binding = _binding_for_target(conn, target, role_target)
        if binding is not None:
            candidates.append(binding)
    if not candidates:
        return RouteSelectionResult(RoutingOutcome.NO_ELIGIBLE_PLANNER_WORKER)
    if len(candidates) > 1:
        return RouteSelectionResult(
            RoutingOutcome.MULTIPLE_ELIGIBLE_PLANNER_WORKERS,
            candidate_worker_ids=tuple(b.worker_id for b in candidates),
        )
    return RouteSelectionResult(RoutingOutcome.SELECTED, binding=candidates[0])


# -- execution-time revalidation --------------------------------------------


class RevalidationOutcome(StrEnum):
    """Stable, code-owned reasons `planning.executor.
    PlanningJobExecutor` uses as a claimed job's `failure_category=
    "routing"`/`failure_reason` when revalidation refuses — never raw
    exception/model/network text (see `RevalidationResult.detail`,
    which is diagnostic-only and never HTTP-visible)."""

    OK = "ok"
    WORKER_UNBOUND = "planner_worker_unbound"
    WORKER_NOT_CONFIGURED = "planner_worker_not_configured"
    WORKER_ARCHIVED = "planner_worker_archived"
    WORKER_NOT_ELIGIBLE = "planner_worker_not_eligible"
    ROUTE_BINDING_STALE = "planner_route_binding_stale"
    RUNTIME_UNREACHABLE = "planner_runtime_unreachable"
    RUNTIME_IDENTITY_MISMATCH = "planner_runtime_identity_mismatch"


@dataclass(frozen=True)
class RevalidationResult:
    ok: bool
    outcome: RevalidationOutcome
    detail: str = ""

    @property
    def failure_reason(self) -> str:
        """The exact, stable, HTTP/durable-safe reason code — `detail`
        (when present) is appended only for the two outcomes whose
        `detail` is itself already a stable code, never raw text: a
        `not_eligible` reason from `evaluate_production_eligibility()`
        (its own reason vocabulary is already code-owned and stable),
        or which specific field went stale."""
        if self.detail:
            return f"{self.outcome.value}:{self.detail}"
        return self.outcome.value


def _ok() -> RevalidationResult:
    return RevalidationResult(True, RevalidationOutcome.OK)


def _refuse(outcome: RevalidationOutcome, detail: str = "") -> RevalidationResult:
    return RevalidationResult(False, outcome, detail)


def revalidate_route_binding(
    conn: sqlite3.Connection,
    binding: PlannerRouteBinding | None,
    *,
    baseline_targets: tuple[BaselineCertificationTarget, ...],
    role_targets: tuple[RoleEvaluationTarget, ...],
    verify_live_runtime: bool = True,
) -> RevalidationResult:
    """Prove a `PlannerRouteBinding` is STILL exactly valid. Two
    distinct callers, one shared invariant:

    - `planning.executor.PlanningJobExecutor`, immediately after claim
      and BEFORE any Planner factory/model call (`verify_live_runtime=
      True`, the default) — this is where the final `verify_ollama_
      runtime()` live-network step belongs; called from the same
      background-thread execution context Certification Center's own
      live preflight/execution steps already run from, never from an
      HTTP request thread.
    - `planning.service.EngineeringPlanningService.create_job()`/
      `.replan_job()`, INSIDE the same production `BEGIN IMMEDIATE`
      transaction that creates the new plan/job row
      (`verify_live_runtime=False`) — a genuine no-network-under-a-
      held-write-lock recheck of only the DB-backed facts (worker
      lifecycle, certificates), the same discipline `security.
      production_promotion`'s own docstring already documents
      ("no database write lock is ever held across an Ollama network
      probe"). This does NOT make config-file acceptance atomic with
      the SQLite transaction -- it cannot be; `baseline_targets`/
      `role_targets` must already be a config snapshot resolved fresh,
      immediately before this call, by the caller. What this closes is
      the narrower, genuinely-atomic race: a certificate/lifecycle
      change landing between route SELECTION and the CREATE
      transaction's own commit.

    `binding is None` means a legacy, pre-H.4 job with no worker
    identity at all (`store.migrations.0019_planner_worker_routing`) —
    refused first, before any config/eligibility/network work, exactly
    like every other case here: zero factory calls, zero model calls,
    on any refusal. (Never actually reached from `create_job()`/
    `replan_job()` — a freshly selected binding is never `None` — only
    from the executor, for a legacy or already-durable job.)

    Exact-match required on every one of `binding`'s own fields against
    what CURRENT config + CURRENT canonical eligibility would produce
    right now — an eligible-but-DIFFERENT authority (a new certificate
    replaced the one this job was queued under, even for a
    superficially similar profile) is `ROUTE_BINDING_STALE`, never
    silently upgraded to the new authority. See the module docstring's
    "no reroute" section — this function only ever checks the ONE
    worker `binding` already names; it never selects a different one."""
    if binding is None:
        return _refuse(RevalidationOutcome.WORKER_UNBOUND)

    worker = WorkersRepo(conn).get(binding.worker_id)
    if worker is None or worker.lifecycle_state != WorkerLifecycleState.ACTIVE:
        return _refuse(RevalidationOutcome.WORKER_ARCHIVED)

    target = next(
        (t for t in baseline_targets if t.worker_id == binding.worker_id), None,
    )
    role_target = _planner_role_target(role_targets, binding.worker_id)
    if target is None or role_target is None:
        return _refuse(RevalidationOutcome.WORKER_NOT_CONFIGURED)

    expectation = target.expectation
    profile = runtime_profile_identity_from_config(
        model_tag=expectation.model_tag,
        model_digest=expectation.model_digest,
        endpoint=expectation.openai_base_url,
        runtime_version=expectation.runtime_version,
        effective_context_tokens=expectation.effective_context_tokens,
        temperature=float(expectation.temperature),
        normalizer_id=expectation.normalizer_id,
        normalizer_version=expectation.normalizer_version,
    )
    role_evaluation = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        output_token_budget=role_target.output_token_budget,
        tool_choice_enforcement=role_target.tool_choice_enforcement,
        policy_version=role_target.policy_version,
    )
    decision = evaluate_production_eligibility(
        conn,
        worker_id=binding.worker_id,
        role=ProductionRole.PLANNER,
        runtime_profile=profile,
        role_evaluation=role_evaluation,
        expected_role_policy_version=role_target.policy_version,
    )
    if not decision.eligible:
        return _refuse(RevalidationOutcome.WORKER_NOT_ELIGIBLE, decision.reason)
    assert decision.security_certificate_id is not None
    assert decision.role_certificate_id is not None

    current = PlannerRouteBinding(
        worker_id=binding.worker_id,
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        role_evaluation_fingerprint=role_evaluation.role_evaluation_fingerprint,
        security_certificate_id=decision.security_certificate_id,
        role_certificate_id=decision.role_certificate_id,
        output_token_budget=role_target.output_token_budget,
        tool_choice_enforcement=role_target.tool_choice_enforcement,
        planner_policy_version=role_target.policy_version,
    )
    if current != binding:
        return _refuse(RevalidationOutcome.ROUTE_BINDING_STALE)

    if not verify_live_runtime:
        # DB-only recheck (create/replan acceptance, inside an open
        # write transaction) -- never probes Ollama while holding a
        # SQLite write lock. See this function's own docstring.
        return _ok()

    try:
        verify_ollama_runtime(expectation)
    except ValueError as exc:
        reason = str(exc) or "runtime_probe_unavailable"
        if reason in {
            "runtime_probe_unavailable", "runtime_probe_redirect",
            "runtime_probe_response_too_large", "runtime_probe_malformed_json",
        }:
            return _refuse(RevalidationOutcome.RUNTIME_UNREACHABLE, reason)
        return _refuse(RevalidationOutcome.RUNTIME_IDENTITY_MISMATCH, reason)

    return _ok()


def route_binding_from_job(job) -> PlannerRouteBinding | None:
    """Reconstruct a `PlannerRouteBinding` from any object exposing the
    eight H.4 route-binding attributes by name — `store.models.
    PlanningJobRow` (`planning.executor.PlanningJobExecutor`'s own
    claimed-row shape) and `planning.models.PlanningJobRecord` both
    qualify, structurally. `None` when `job.worker_id is None` — a
    legacy, pre-H.4 job with no binding to reconstruct at all (never a
    `KeyError`/`AttributeError` on the other seven fields, which are
    written together or not at all — see `store.migrations.
    0019_planner_worker_routing`'s own INSERT-completeness trigger)."""
    if job.worker_id is None:
        return None
    return PlannerRouteBinding(
        worker_id=job.worker_id,
        runtime_identity_fingerprint=job.runtime_identity_fingerprint,
        role_evaluation_fingerprint=job.role_evaluation_fingerprint,
        security_certificate_id=job.security_certificate_id,
        role_certificate_id=job.role_certificate_id,
        output_token_budget=job.output_token_budget,
        tool_choice_enforcement=job.tool_choice_enforcement,
        planner_policy_version=job.planner_policy_version,
    )
