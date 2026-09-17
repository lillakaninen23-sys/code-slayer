"""Host wiring for Certification Center v1 against a local Qwen coder.

This is not a security authority. It only declares the worker identity
and the operator-supplied expected Ollama runtime. Digest, version, and
fingerprint are never accepted over HTTP. The live runner still probes
Ollama and records certificates only into isolated validation state.

Required environment:

  CODESLAYER_CERT_OLLAMA_ROOT        Ollama origin, e.g. http://127.0.0.1:11434
  CODESLAYER_CERT_MODEL_DIGEST       exact digest the live probe must match
  CODESLAYER_CERT_RUNTIME_VERSION    exact /api/version string

Optional:

  CODESLAYER_CERT_WORKER_ID          default local-ollama-qwen3-coder-30b
  CODESLAYER_CERT_MODEL_TAG          default qwen3-coder:30b
  CODESLAYER_CERT_CONTEXT_TOKENS     default 16384 (config-bound, not measured)
  CODESLAYER_CERT_TEMPERATURE        default 0.0
  CODESLAYER_CERT_NORMALIZER_ID      default qwen_textual_tool_v1
  CODESLAYER_CERT_NORMALIZER_VERSION default 1

If the required env is missing, the worker is still registered so the
Certification Center can list it; preflight then fails closed with
runtime_profile_not_configured. No default host or digest is baked in.
"""

from __future__ import annotations

import os

from code_slayer.api.service import RuntimeBindings, WorkerRegistration
from code_slayer.security.certification_service import (
    BaselineCertificationTarget,
    RoleEvaluationTarget,
)
from code_slayer.security.live_certification import LiveOllamaRuntimeExpectation
from code_slayer.workers.role_qualification import ProductionRole
from code_slayer.workers.security_baseline import runtime_profile_identity_from_config

DEFAULT_WORKER_ID = "local-ollama-qwen3-coder-30b"
DEFAULT_MODEL_TAG = "qwen3-coder:30b"


def create_runtime() -> RuntimeBindings:
    worker_id = os.environ.get("CODESLAYER_CERT_WORKER_ID", DEFAULT_WORKER_ID).strip()
    registrations = (
        WorkerRegistration(
            worker_id=worker_id, kind="openai_compatible", network_class="local",
        ),
    )
    root = os.environ.get("CODESLAYER_CERT_OLLAMA_ROOT", "").strip()
    digest = os.environ.get("CODESLAYER_CERT_MODEL_DIGEST", "").strip()
    version = os.environ.get("CODESLAYER_CERT_RUNTIME_VERSION", "").strip()
    if not root or not digest or not version:
        return RuntimeBindings(worker_registrations=registrations)

    tag = os.environ.get("CODESLAYER_CERT_MODEL_TAG", DEFAULT_MODEL_TAG).strip()
    context = int(os.environ.get("CODESLAYER_CERT_CONTEXT_TOKENS", "16384"))
    temperature = float(os.environ.get("CODESLAYER_CERT_TEMPERATURE", "0.0"))
    normalizer_id = os.environ.get("CODESLAYER_CERT_NORMALIZER_ID", "qwen_textual_tool_v1").strip()
    normalizer_version = int(os.environ.get("CODESLAYER_CERT_NORMALIZER_VERSION", "1"))
    identity = runtime_profile_identity_from_config(
        model_tag=tag,
        model_digest=digest,
        endpoint=f"{root.rstrip('/')}/v1",
        runtime_version=version,
        effective_context_tokens=context,
        temperature=temperature,
        normalizer_id=normalizer_id or None,
        normalizer_version=normalizer_version if normalizer_id else None,
    )
    target = BaselineCertificationTarget(
        worker_id=worker_id,
        expectation=LiveOllamaRuntimeExpectation(
            ollama_root=root,
            model_tag=tag,
            model_digest=digest,
            runtime_version=version,
            effective_context_tokens=context,
            temperature=temperature,
            expected_runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
            normalizer_id=normalizer_id or None,
            normalizer_version=normalizer_version if normalizer_id else None,
        ),
    )
    role_target = RoleEvaluationTarget(
        worker_id=worker_id,
        role=ProductionRole.PLANNER,
        output_token_budget=int(os.environ.get("CODESLAYER_CERT_OUTPUT_TOKEN_BUDGET", "4096")),
        tool_choice_enforcement=os.environ.get(
            "CODESLAYER_CERT_TOOL_CHOICE", "ADVISORY_ONLY_UNVERIFIED",
        ),
        policy_version=os.environ.get(
            "CODESLAYER_CERT_PLANNER_POLICY", "planner-certification-v1",
        ),
    )
    return RuntimeBindings(
        worker_registrations=registrations,
        baseline_certification_targets=(target,),
        role_evaluation_targets=(role_target,),
    )
