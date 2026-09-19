"""config.bindings: mapping persistent config to server-owned targets.

H.1 item 2: Planner `RoleEvaluationTarget` values must come from real,
persistent worker config -- never an empty placeholder tuple.
"""

from __future__ import annotations

import http.server
import threading
import time

from code_slayer.config.bindings import (
    planner_for_worker,
    planner_role_evaluation_target_from_worker,
    runtime_bindings_from_config,
)
from code_slayer.config.schema import CSLRConfig, OllamaServerConfig, WorkerRuntimeConfig
from code_slayer.planning.planner import PlannerFailureCategory, PlannerOutcome, PlannerRequest
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
        planner_timeout_seconds=120.0,
        planner_policy_version="planner-certification-v2",
    )
    target = planner_role_evaluation_target_from_worker(worker)
    assert target == RoleEvaluationTarget(
        worker_id="w1",
        role=ProductionRole.PLANNER,
        output_token_budget=2048,
        tool_choice_enforcement="REQUIRED",
        planner_timeout_seconds=120.0,
        policy_version="planner-certification-v2",
    )


def test_planner_role_target_uses_schema_defaults_when_unset():
    worker = _worker()
    target = planner_role_evaluation_target_from_worker(worker)
    assert target.output_token_budget == 4096
    assert target.tool_choice_enforcement == "ADVISORY_ONLY_UNVERIFIED"
    assert target.planner_timeout_seconds == 30.0
    assert target.policy_version == "planner-certification-v2"


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


# -- H.4.1: `planner_for_worker()` actually uses the worker's configured -----
# -- timeout -- a real bounded fake-HTTP-server timing proof, never merely --
# -- inspection of the constructed config object. ----------------------------


class _HangingHandler(http.server.BaseHTTPRequestHandler):
    """Accepts the connection but never writes a response within any
    ordinary test timeout -- forces a genuine client-side socket
    timeout, exactly the transport condition the H.4.1 production
    incident hit (`adapter_error:transport_timeout`)."""

    def do_POST(self) -> None:  # noqa: N802
        time.sleep(5.0)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


def _hanging_server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _HangingHandler)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True,
    )
    thread.start()
    return httpd, thread


def test_planner_for_worker_uses_the_workers_configured_timeout_not_the_adapter_default():
    """Real network timing, not config inspection: a worker configured
    with a short `planner_timeout_seconds` must have its actual Planner
    turn time out close to THAT value against a server that never
    responds -- never the adapter's own much-larger hardcoded default
    (30.0s), which is exactly the bug this field exists to close (see
    `config.bindings.planner_for_worker`'s own docstring)."""
    httpd, thread = _hanging_server()
    try:
        root = f"http://127.0.0.1:{httpd.server_port}"
        config = CSLRConfig(
            ollama_servers=(OllamaServerConfig(server_id="local", origin=root),),
            workers=(_worker(worker_id="w1", planner_timeout_seconds=0.3),),
        )
        planner = planner_for_worker(config, "w1", "job-1")
        start = time.monotonic()
        response = planner.plan(PlannerRequest(original_request="Add a read-only endpoint."))
        elapsed = time.monotonic() - start
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    # Bounded well under the adapter's own 30.0s default -- a mismatch
    # (the exact H.4.1 production bug) would make this take ~30s instead.
    assert elapsed < 5.0
    assert response.outcome == PlannerOutcome.MALFORMED
    assert response.failure_category == PlannerFailureCategory.TRANSPORT_ERROR
    assert response.error == "adapter_error:transport_timeout"


def test_planner_for_worker_timeout_scales_with_configured_value():
    """A second, longer-configured timeout against the SAME hanging
    server takes measurably longer to fail than the short one above --
    proof the value is actually threaded through per-worker, never a
    single hardcoded constant reused regardless of config."""
    httpd, thread = _hanging_server()
    try:
        root = f"http://127.0.0.1:{httpd.server_port}"
        config = CSLRConfig(
            ollama_servers=(OllamaServerConfig(server_id="local", origin=root),),
            workers=(_worker(worker_id="w1", planner_timeout_seconds=1.5),),
        )
        planner = planner_for_worker(config, "w1", "job-1")
        start = time.monotonic()
        response = planner.plan(PlannerRequest(original_request="Add a read-only endpoint."))
        elapsed = time.monotonic() - start
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    assert elapsed >= 1.4  # close to the configured 1.5s, never the short 0.3s above
    assert elapsed < 5.0  # and still nowhere near the adapter's 30.0s default
    assert response.outcome == PlannerOutcome.MALFORMED
    assert response.failure_category == PlannerFailureCategory.TRANSPORT_ERROR
