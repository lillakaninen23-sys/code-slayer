"""Compare approved config identity to live-attested Ollama inventory."""

from __future__ import annotations

from dataclasses import dataclass

from code_slayer.config.schema import CSLRConfig, WorkerRuntimeConfig
from code_slayer.security.live_certification import (
    OllamaInventory,
    canonical_model_digest,
    probe_ollama_inventory,
)


@dataclass(frozen=True)
class RuntimeAttestation:
    worker_id: str
    status: str
    reason: str
    configured_digest: str | None
    observed_digest: str | None
    configured_version: str | None
    observed_version: str | None
    configured_model_tag: str
    observed_model_tag: str | None
    ollama_origin: str | None
    fingerprint_source: str


def attest_worker(
    config: CSLRConfig,
    worker: WorkerRuntimeConfig,
    *,
    inventory: OllamaInventory | None = None,
    probe=probe_ollama_inventory,
) -> RuntimeAttestation:
    server = config.server_by_id(worker.ollama_server_id)
    if server is None:
        return RuntimeAttestation(
            worker_id=worker.worker_id, status="UNVERIFIED",
            reason="ollama_server_missing", configured_digest=worker.approved_model_digest,
            observed_digest=None, configured_version=worker.approved_runtime_version,
            observed_version=None, configured_model_tag=worker.model_tag,
            observed_model_tag=None, ollama_origin=None,
            fingerprint_source="CONFIG_BOUND",
        )
    try:
        live = inventory or probe(server.origin)
    except ValueError as exc:
        reason = str(exc) or "runtime_probe_unavailable"
        status = "UNREACHABLE" if "unavailable" in reason else "UNVERIFIED"
        return RuntimeAttestation(
            worker_id=worker.worker_id, status=status, reason=reason,
            configured_digest=worker.approved_model_digest, observed_digest=None,
            configured_version=worker.approved_runtime_version, observed_version=None,
            configured_model_tag=worker.model_tag, observed_model_tag=None,
            ollama_origin=server.origin, fingerprint_source="CONFIG_BOUND",
        )
    observed = next((item for item in live.models if item.name == worker.model_tag), None)
    observed_digest = observed.digest if observed is not None else None
    if not worker.identity_approved:
        return RuntimeAttestation(
            worker_id=worker.worker_id, status="OBSERVED",
            reason="runtime_identity_not_approved",
            configured_digest=None, observed_digest=observed_digest,
            configured_version=None, observed_version=live.runtime_version,
            configured_model_tag=worker.model_tag,
            observed_model_tag=observed.name if observed else None,
            ollama_origin=server.origin, fingerprint_source="LIVE_ATTESTED",
        )
    if observed is None:
        return RuntimeAttestation(
            worker_id=worker.worker_id, status="MISMATCH",
            reason="runtime_model_missing",
            configured_digest=worker.approved_model_digest, observed_digest=None,
            configured_version=worker.approved_runtime_version,
            observed_version=live.runtime_version,
            configured_model_tag=worker.model_tag, observed_model_tag=None,
            ollama_origin=server.origin, fingerprint_source="LIVE_ATTESTED",
        )
    digest_ok = canonical_model_digest(observed.digest) == canonical_model_digest(
        worker.approved_model_digest or "",
    )
    version_ok = live.runtime_version == worker.approved_runtime_version
    if digest_ok and version_ok:
        return RuntimeAttestation(
            worker_id=worker.worker_id, status="VERIFIED",
            reason="approved_identity_live_attested",
            configured_digest=worker.approved_model_digest,
            observed_digest=observed.digest,
            configured_version=worker.approved_runtime_version,
            observed_version=live.runtime_version,
            configured_model_tag=worker.model_tag, observed_model_tag=observed.name,
            ollama_origin=server.origin, fingerprint_source="LIVE_ATTESTED",
        )
    return RuntimeAttestation(
        worker_id=worker.worker_id, status="MISMATCH",
        reason="runtime_identity_mismatch",
        configured_digest=worker.approved_model_digest,
        observed_digest=observed.digest,
        configured_version=worker.approved_runtime_version,
        observed_version=live.runtime_version,
        configured_model_tag=worker.model_tag, observed_model_tag=observed.name,
        ollama_origin=server.origin, fingerprint_source="LIVE_ATTESTED",
    )
