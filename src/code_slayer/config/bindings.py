"""Map persistent config to RuntimeBindings. Not a security authority."""

from __future__ import annotations

from code_slayer.config.schema import CSLRConfig, WorkerRuntimeConfig
from code_slayer.security.certification_service import (
    BaselineCertificationTarget,
)
from code_slayer.security.live_certification import LiveOllamaRuntimeExpectation
from code_slayer.workers.security_baseline import runtime_profile_identity_from_config


def runtime_bindings_from_config(config: CSLRConfig):
    from code_slayer.api.service import RuntimeBindings, WorkerRegistration

    registrations = []
    targets = []
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
    return RuntimeBindings(
        worker_registrations=tuple(registrations),
        baseline_certification_targets=tuple(targets),
        role_evaluation_targets=(),
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
