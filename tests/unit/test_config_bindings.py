"""config.bindings: mapping persistent config to server-owned targets.

H.1 item 2: Planner `RoleEvaluationTarget` values must come from real,
persistent worker config -- never an empty placeholder tuple.
"""

from __future__ import annotations

from code_slayer.config.bindings import (
    planner_role_evaluation_target_from_worker,
    runtime_bindings_from_config,
)
from code_slayer.config.schema import CSLRConfig, OllamaServerConfig, WorkerRuntimeConfig
from code_slayer.security.certification_service import RoleEvaluationTarget
from code_slayer.workers.role_qualification import ProductionRole


def _worker(**overrides) -> WorkerRuntimeConfig:
    kwargs = dict(
        worker_id="w1",
        kind="openai_compatible",
        network_class="local",
        ollama_server_id="local",
        model_tag="demo:1",
        approved_model_digest=None,
        approved_runtime_version=None,
        effective_context_tokens=4096,
        temperature=0.0,
        normalizer_id=None,
        normalizer_version=None,
    )
    kwargs.update(overrides)
    return WorkerRuntimeConfig(**kwargs)


def test_planner_role_target_uses_worker_config_fields():
    worker = _worker(
        output_token_budget=2048,
        tool_choice_enforcement="REQUIRED",
        planner_policy_version="planner-certification-v2",
    )
    target = planner_role_evaluation_target_from_worker(worker)
    assert target == RoleEvaluationTarget(
        worker_id="w1",
        role=ProductionRole.PLANNER,
        output_token_budget=2048,
        tool_choice_enforcement="REQUIRED",
        policy_version="planner-certification-v2",
    )


def test_planner_role_target_uses_schema_defaults_when_unset():
    worker = _worker()
    target = planner_role_evaluation_target_from_worker(worker)
    assert target.output_token_budget == 4096
    assert target.tool_choice_enforcement == "ADVISORY_ONLY_UNVERIFIED"
    assert target.policy_version == "planner-certification-v1"


def test_planner_role_target_is_independent_of_baseline_identity_approval():
    """Unlike `baseline_target_from_worker`, a role evaluation target does
    not require `identity_approved` -- the common runtime identity is
    combined in separately, at evaluation time, from the worker's own
    `BaselineCertificationTarget`."""
    not_approved = _worker(approved_model_digest=None, approved_runtime_version=None)
    target = planner_role_evaluation_target_from_worker(not_approved)
    assert target is not None
    assert target.worker_id == "w1"


def test_runtime_bindings_from_config_populates_role_evaluation_targets():
    config = CSLRConfig(
        ollama_servers=(OllamaServerConfig(server_id="local", origin="http://127.0.0.1:11434"),),
        workers=(
            _worker(worker_id="w1"),
            _worker(
                worker_id="w2",
                approved_model_digest="sha256:" + "a" * 64,
                approved_runtime_version="0.16.1",
                output_token_budget=8192,
            ),
        ),
    )
    bindings = runtime_bindings_from_config(config)
    assert len(bindings.role_evaluation_targets) == 2
    by_worker = {t.worker_id: t for t in bindings.role_evaluation_targets}
    assert by_worker["w1"].role == ProductionRole.PLANNER
    assert by_worker["w2"].output_token_budget == 8192
    # Baseline targets remain gated on identity approval -- unrelated to
    # role evaluation targets, and unaffected by this change: only w2
    # (approved digest/version) gets one, w1 does not.
    assert [t.worker_id for t in bindings.baseline_certification_targets] == ["w2"]
