"""Code-owned engineering routes from current approved local worker config.

Like Planner routing, ambiguous equally qualified candidates fail closed. No
model name or caller-supplied adapter can choose a production route. Runtime
attestation and inference reuse the existing configured Ollama transport.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from code_slayer.store.db import utcnow_iso
from code_slayer.workers.engineering_roles import ENGINEERING_ROLES, POLICY_VERSION
from code_slayer.workers.openai_compatible_adapter import (
    OpenAICompatibleAdapter,
    OpenAICompatibleConfig,
)
from code_slayer.workers.production_eligibility import evaluate_production_eligibility
from code_slayer.workers.protocol import WorkerAdapterError
from code_slayer.workers.role_qualification import (
    ProductionRole,
    role_evaluation_identity_from_config,
)
from code_slayer.workers.security_baseline import runtime_profile_identity_from_config

OUTPUT_TOKEN_BUDGET = 4096
EXECUTION_TIMEOUT_SECONDS = 120.0
TOOL_CHOICE = "OPTIONAL_NATIVE_VALIDATED"


class RoleRoutingError(WorkerAdapterError):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class EngineeringTarget:
    worker_id: str
    role: ProductionRole
    expectation: object

    @property
    def profile(self):
        e = self.expectation
        return runtime_profile_identity_from_config(
            model_tag=e.model_tag,
            model_digest=e.model_digest,
            endpoint=e.openai_base_url,
            runtime_version=e.runtime_version,
            effective_context_tokens=e.effective_context_tokens,
            temperature=e.temperature,
            normalizer_id=e.normalizer_id,
            normalizer_version=e.normalizer_version,
        )

    @property
    def evaluation(self):
        return role_evaluation_identity_from_config(
            role=self.role,
            runtime_identity_fingerprint=self.profile.runtime_identity_fingerprint,
            output_token_budget=OUTPUT_TOKEN_BUDGET,
            tool_choice_enforcement=TOOL_CHOICE,
            execution_timeout_seconds=EXECUTION_TIMEOUT_SECONDS,
            policy_version=POLICY_VERSION,
        )


def targets_from_config(config, role):
    from code_slayer.config.bindings import baseline_target_from_worker

    if role not in ENGINEERING_ROLES:
        raise RoleRoutingError("unsupported_engineering_role")
    targets = []
    for worker in config.workers:
        # No widening into cloud, compatibility decoders, or unapproved identity.
        if worker.network_class != "local" or worker.normalizer_id is not None:
            continue
        target = baseline_target_from_worker(config, worker)
        if target is not None:
            targets.append(EngineeringTarget(worker.worker_id, role, target.expectation))
    return tuple(targets)


def attest(target):
    from code_slayer.security.live_certification import verify_ollama_runtime

    try:
        verify_ollama_runtime(target.expectation)
    except (OSError, ValueError) as exc:
        raise RoleRoutingError("role_runtime_attestation_failed") from exc


def configured_adapter(target):
    e = target.expectation
    return OpenAICompatibleAdapter(
        OpenAICompatibleConfig(
            base_url=e.openai_base_url,
            model=e.model_tag,
            temperature=e.temperature,
            timeout=target.evaluation.execution_timeout_seconds,
            max_response_bytes=e.max_response_bytes,
            api_key=e.api_key,
        )
    )


@dataclass(frozen=True)
class RoleBinding:
    worker_id: str
    role: ProductionRole
    runtime_identity_fingerprint: str
    role_evaluation_fingerprint: str
    security_certificate_id: str
    role_certificate_id: str


def eligible_binding(conn, blobs_dir, target, *, now_fn=utcnow_iso):
    decision = evaluate_production_eligibility(
        conn,
        worker_id=target.worker_id,
        role=target.role,
        runtime_profile=target.profile,
        role_evaluation=target.evaluation,
        expected_role_policy_version=POLICY_VERSION,
        blobs_dir=blobs_dir,
        now_fn=now_fn,
    )
    if not decision.eligible:
        raise RoleRoutingError(decision.reason)
    return RoleBinding(
        target.worker_id,
        target.role,
        target.profile.runtime_identity_fingerprint,
        target.evaluation.role_evaluation_fingerprint,
        decision.security_certificate_id,
        decision.role_certificate_id,
    )


class EngineeringRoleRouter:
    """Backend service, never HTTP/model input. Reload config at every inference.

    No adapter injection or strength assertions. V1 qualifies pass/fail only;
    multiple eligible candidates have no measured ordering and are refused.
    """

    def __init__(self, conn, blobs_dir, config_loader, *, now_fn=utcnow_iso):
        self.conn, self.blobs_dir = conn, blobs_dir
        self.config_loader, self.now_fn = config_loader, now_fn

    def select(self, role, *, excluded=()):
        candidates, denials = [], []
        for target in targets_from_config(self.config_loader(), role):
            if any(
                target.worker_id == prior.worker_id
                or target.profile.model_digest.removeprefix("sha256:")
                == prior.expectation.model_digest.removeprefix("sha256:")
                for prior in excluded
            ):
                continue
            try:
                binding = eligible_binding(
                    self.conn,
                    self.blobs_dir,
                    target,
                    now_fn=self.now_fn,
                )
                candidates.append((target, binding))
            except RoleRoutingError as exc:
                denials.append(exc.reason)
        if not candidates:
            detail = denials[0] if denials and len(set(denials)) == 1 else "none"
            raise RoleRoutingError(f"no_eligible_{role.value.lower()}_worker:{detail}")
        if len(candidates) != 1:
            raise RoleRoutingError(f"multiple_eligible_{role.value.lower()}_workers")
        target, binding = candidates[0]
        return CertifiedRoleAdapter(self, target, binding)

    def revalidate(self, target, binding):
        current = next(
            (
                t
                for t in targets_from_config(self.config_loader(), binding.role)
                if t.worker_id == binding.worker_id
            ),
            None,
        )
        if current is None:
            raise RoleRoutingError("role_worker_not_configured")
        if (
            current != target
            or eligible_binding(
                self.conn,
                self.blobs_dir,
                current,
                now_fn=self.now_fn,
            )
            != binding
        ):
            raise RoleRoutingError("role_route_binding_stale")
        attest(current)
        # Attestation is outside a write lock; recheck durable eligibility after it.
        if eligible_binding(self.conn, self.blobs_dir, current, now_fn=self.now_fn) != binding:
            raise RoleRoutingError("role_route_binding_stale")

    def resolve_pipeline(self):
        coder = self.select(ProductionRole.CODER)
        repairer = self.select(ProductionRole.REPAIRER)
        reviewer = self.select(ProductionRole.REVIEWER, excluded=(coder.target, repairer.target))
        security = self.select(
            ProductionRole.SECURITY, excluded=(coder.target, repairer.target, reviewer.target)
        )
        return {
            ProductionRole.CODER: coder,
            ProductionRole.REPAIRER: repairer,
            ProductionRole.REVIEWER: reviewer,
            ProductionRole.SECURITY: security,
        }


class CertifiedRoleAdapter:
    """Rechecks bound identity/certificates before EVERY inference, no fallback."""

    def __init__(self, router, target, binding):
        self.router, self.target, self.binding = router, target, binding

    def infer(self, request):
        if request.role != self.binding.role.value.lower():
            raise RoleRoutingError("execution_role_mismatch")
        allowed = (
            ("read_file", "create_file", "write_file", "apply_patch")
            if self.binding.role in (ProductionRole.CODER, ProductionRole.REPAIRER)
            else ()
        )
        if request.allowed_tools is None or not set(request.allowed_tools).issubset(allowed):
            raise RoleRoutingError("execution_role_tool_mismatch")
        self.router.revalidate(self.target, self.binding)
        # The exact certified role evaluation budget is applied in qualification and execution.
        response = configured_adapter(self.target).infer(
            replace(
                request,
                max_output_tokens=self.target.evaluation.output_token_budget,
            )
        )
        # A change while inference was in flight cannot authorize the returned tool call.
        self.router.revalidate(self.target, self.binding)
        return response

    def provenance(self):
        return asdict(self.binding)
