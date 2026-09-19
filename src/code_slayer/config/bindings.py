"""Map persistent config to RuntimeBindings. Not a security authority."""

from __future__ import annotations

from code_slayer.config.schema import CSLRConfig, WorkerRuntimeConfig
from code_slayer.security.certification_service import (
    BaselineCertificationTarget,
    RoleEvaluationTarget,
)
from code_slayer.security.live_certification import LiveOllamaRuntimeExpectation
from code_slayer.workers.role_qualification import ProductionRole
from code_slayer.workers.security_baseline import runtime_profile_identity_from_config


class PlannerWorkerNotConfiguredError(Exception):
    """H.4: raised by `planner_for_worker()` if `worker_id` (or its
    declared Ollama server) is not present in the `CSLRConfig` snapshot
    this factory closure was built from. Should never actually happen
    in production — execution-time route revalidation (`planning.
    routing.revalidate_route_binding()`) already proved the worker IS
    configured, against this SAME config snapshot, immediately before
    `planning.executor.PlanningJobExecutor` ever calls
    `planner_factory_for_worker(worker_id, job_id)` — but this stays
    defensive rather than assuming that invariant can never break."""

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        super().__init__(f"worker {worker_id!r} has no current persistent config")


def runtime_bindings_from_config(config: CSLRConfig):
    from code_slayer.api.service import RuntimeBindings, WorkerRegistration

    registrations = []
    targets = []
    role_targets = []
    for worker in config.workers:
        registrations.append(
            WorkerRegistration(
                worker_id=worker.worker_id,
                kind=worker.kind,
                network_class=worker.network_class,
            )
        )
        target = baseline_target_from_worker(config, worker)
        if target is not None:
            targets.append(target)
        role_targets.append(planner_role_evaluation_target_from_worker(worker))
    return RuntimeBindings(
        worker_registrations=tuple(registrations),
        baseline_certification_targets=tuple(targets),
        role_evaluation_targets=tuple(role_targets),
        # H.4: a closure over THIS EXACT `config` snapshot -- never a
        # second, separate config read inside the factory itself. This
        # is what makes `planning.executor.PlanningJobExecutor`'s own
        # `bindings_factory` freshness discipline actually work end to
        # end: the executor calls `bindings_factory()` (typically
        # `ApplicationService._compose_bindings`) fresh before every
        # job execution, which calls THIS function fresh with a newly
        # loaded `config`, which closes over that new snapshot here --
        # so a config change an operator saves while the process keeps
        # running is observed on the very next job execution, never
        # requiring a restart.
        planner_factory_for_worker=planner_factory_for_worker_from_config(config),
    )


def baseline_target_from_worker(
    config: CSLRConfig, worker: WorkerRuntimeConfig,
) -> BaselineCertificationTarget | None:
    if not worker.identity_approved:
        return None
    digest = worker.approved_model_digest
    version = worker.approved_runtime_version
    if digest is None or version is None:
        return None
    server = config.server_by_id(worker.ollama_server_id)
    if server is None:
        return None
    origin = server.origin.rstrip("/")
    identity = runtime_profile_identity_from_config(
        model_tag=worker.model_tag,
        model_digest=digest,
        endpoint=f"{origin}/v1",
        runtime_version=version,
        effective_context_tokens=worker.effective_context_tokens,
        temperature=float(worker.temperature),
        normalizer_id=worker.normalizer_id,
        normalizer_version=worker.normalizer_version,
    )
    fingerprint = identity.runtime_identity_fingerprint
    if fingerprint is None:
        return None
    return BaselineCertificationTarget(
        worker_id=worker.worker_id,
        expectation=LiveOllamaRuntimeExpectation(
            ollama_root=server.origin,
            model_tag=worker.model_tag,
            model_digest=digest,
            runtime_version=version,
            effective_context_tokens=worker.effective_context_tokens,
            temperature=float(worker.temperature),
            expected_runtime_identity_fingerprint=fingerprint,
            normalizer_id=worker.normalizer_id,
            normalizer_version=worker.normalizer_version,
        ),
    )


def planner_role_evaluation_target_from_worker(
    worker: WorkerRuntimeConfig,
) -> RoleEvaluationTarget:
    """Server-owned Planner role/evaluation diagnostics target, built
    from the worker's own persistent config fields
    (`output_token_budget`/`tool_choice_enforcement`/
    `planner_policy_version`) -- never a placeholder or an empty target.
    This is used only for eligibility diagnostics
    (`CertificationService._eligibility`); it never substitutes for
    Baseline Security and never itself grants trust, permission, or a
    role certificate. Unlike `baseline_target_from_worker`, this does not
    depend on `identity_approved`: a runtime identity is combined in
    separately, at evaluation time, from the worker's own
    `BaselineCertificationTarget` (which independently decides its own
    `None`-ness)."""
    return RoleEvaluationTarget(
        worker_id=worker.worker_id,
        role=ProductionRole.PLANNER,
        output_token_budget=worker.output_token_budget,
        tool_choice_enforcement=worker.tool_choice_enforcement,
        planner_timeout_seconds=worker.planner_timeout_seconds,
        policy_version=worker.planner_policy_version,
    )


def planner_for_worker(config: CSLRConfig, worker_id: str, job_id: str):
    """H.4: construct the actual worker-bound production
    `planning.planner.Planner` for `worker_id`, from `config` (a
    snapshot the caller already resolved fresh — this function performs
    no config I/O of its own) using the EXISTING OpenAI-compatible
    transport/`WorkerAdapterPlanner` bridge — never a second Ollama/HTTP
    implementation, matching `security.live_planner_certification.
    certify_live_planner_role()`'s own construction exactly. `job_id`
    becomes this turn's own protocol-layer task identity
    (`f"planning-job:{job_id}"`), the deterministic identity `planning.
    worker_planner.WorkerAdapterPlanner.task_id` needs (it already
    requires a `task_id`, and a plan's own `job_id` is the natural,
    already-unique choice — never reused across two different jobs).

    Native structured tool calls retain precedence exactly as
    `WorkerAdapterPlanner` already enforces; a configured normalizer
    (`worker.normalizer_id`/`.normalizer_version`) is passed through
    unchanged, using the same code-owned `build_default_normalizer_
    registry()` every other production Planner-turn caller uses — this
    function invents no second compatibility-decoder registry."""
    from code_slayer.planning.worker_planner import (
        WorkerAdapterPlanner,
        build_default_normalizer_registry,
    )
    from code_slayer.workers.openai_compatible_adapter import (
        OpenAICompatibleAdapter,
        OpenAICompatibleConfig,
    )

    worker = config.worker_by_id(worker_id)
    server = config.server_by_id(worker.ollama_server_id) if worker is not None else None
    if worker is None or server is None:
        raise PlannerWorkerNotConfiguredError(worker_id)
    adapter = OpenAICompatibleAdapter(OpenAICompatibleConfig(
        base_url=f"{server.origin.rstrip('/')}/v1", model=worker.model_tag,
        temperature=float(worker.temperature),
        timeout=float(worker.planner_timeout_seconds),
    ))
    normalizer_registry = (
        build_default_normalizer_registry() if worker.normalizer_id is not None else None
    )
    return WorkerAdapterPlanner(
        adapter, task_id=f"planning-job:{job_id}", role="planner",
        normalizer_registry=normalizer_registry,
        normalizer_id=worker.normalizer_id, normalizer_version=worker.normalizer_version,
    )


def planner_factory_for_worker_from_config(config: CSLRConfig):
    """`Callable[[worker_id, job_id], Planner]` closed over `config` —
    see `runtime_bindings_from_config()`'s own docstring for why this
    closure (never a second, separate config read inside it) is exactly
    what makes the executor's `bindings_factory` freshness discipline
    work."""
    def _factory(worker_id: str, job_id: str):
        return planner_for_worker(config, worker_id, job_id)
    return _factory
