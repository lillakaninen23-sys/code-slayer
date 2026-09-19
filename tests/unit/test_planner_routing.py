"""H.4: durable, production-eligible, worker-bound Planner routing
(`planning.routing`).

Covers `select_planner_route()`'s zero/one/multiple candidate policy,
candidate enumeration strictly from CURRENT config (never stale DB
history), and `revalidate_route_binding()`'s exact-match execution-time
recheck (worker archived/not-configured/not-eligible/stale, legacy
unbound, and live-runtime attestation).
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from code_slayer.planning.routing import (
    PlannerRouteBinding,
    RevalidationOutcome,
    RoutingOutcome,
    revalidate_route_binding,
    route_binding_from_job,
    select_planner_route,
)
from code_slayer.security.certification_service import (
    BaselineCertificationTarget,
    RoleEvaluationTarget,
)
from code_slayer.security.live_certification import LiveOllamaRuntimeExpectation
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.lifecycle import archive_worker
from code_slayer.workers.role_qualification import (
    ProductionRole,
    RoleQualificationOutcome,
    record_role_certificate,
    role_evaluation_identity_from_config,
)
from code_slayer.workers.security_baseline import (
    SecurityBaselineOutcome,
    record_baseline_certificate,
    runtime_profile_identity_from_config,
)

POLICY_VERSION = "planner-certification-v1"
OUTPUT_TOKEN_BUDGET = 4096
TOOL_CHOICE_ENFORCEMENT = "ADVISORY_ONLY_UNVERIFIED"
OLLAMA_ROOT = "http://local:11434"  # never live-probed unless a test opts in


class _OllamaScript:
    def __init__(self) -> None:
        self.version = "0.16.1"
        self.models = [{"name": "devstral:24b", "digest": "sha256:abc"}]


def _make_ollama_handler(script: _OllamaScript) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _write(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/")
            if path == "/api/version":
                self._write(200, json.dumps({"version": script.version}).encode())
                return
            if path == "/api/tags":
                self._write(200, json.dumps({"models": script.models}).encode())
                return
            self._write(404, b"{}")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return Handler


@pytest.fixture
def ollama_server():
    script = _OllamaScript()
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _make_ollama_handler(script))
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True,
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _profile(root: str = OLLAMA_ROOT, model_tag: str = "devstral:24b"):
    return runtime_profile_identity_from_config(
        model_tag=model_tag, model_digest="sha256:abc", endpoint=f"{root}/v1",
        runtime_version="0.16.1", effective_context_tokens=16384, temperature=0.0,
        normalizer_id=None, normalizer_version=None,
    )


def _target(worker_id: str, *, root: str = OLLAMA_ROOT, model_tag: str = "devstral:24b"):
    profile = _profile(root, model_tag)
    return BaselineCertificationTarget(
        worker_id=worker_id,
        expectation=LiveOllamaRuntimeExpectation(
            ollama_root=root, model_tag=model_tag, model_digest="sha256:abc",
            runtime_version="0.16.1", effective_context_tokens=16384, temperature=0.0,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
            normalizer_id=None, normalizer_version=None,
        ),
    )


def _role_target(worker_id: str) -> RoleEvaluationTarget:
    return RoleEvaluationTarget(
        worker_id=worker_id, role=ProductionRole.PLANNER,
        output_token_budget=OUTPUT_TOKEN_BUDGET, tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        policy_version=POLICY_VERSION,
    )


def _certify_eligible(
    conn, worker_id: str, *, root: str = OLLAMA_ROOT, model_tag: str = "devstral:24b",
):
    WorkersRepo(conn).register(worker_id=worker_id, kind="fake", network_class="local")
    profile = _profile(root, model_tag)
    security = record_baseline_certificate(
        conn, worker_id=worker_id, runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS, evidence_ref=f"sec-ev-{worker_id}", reason="ok",
    )
    assert security.ok, security.reason
    role_evaluation = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        policy_version=POLICY_VERSION,
    )
    role = record_role_certificate(
        conn, worker_id=worker_id, role=ProductionRole.PLANNER, runtime_profile=profile,
        policy_version=POLICY_VERSION, outcome=RoleQualificationOutcome.PASS,
        classification="PASS_FIRST_TRY", evidence_ref=f"role-ev-{worker_id}", reason="ok",
        role_evaluation=role_evaluation,
    )
    assert role.ok, role.reason
    return PlannerRouteBinding(
        worker_id=worker_id,
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        role_evaluation_fingerprint=role_evaluation.role_evaluation_fingerprint,
        security_certificate_id=security.certificate.certificate_id,
        role_certificate_id=role.certificate.certificate_id,
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        planner_policy_version=POLICY_VERSION,
    )


# -- select_planner_route(): zero/one/multiple -------------------------------


def test_zero_configured_candidates_is_no_eligible_worker(db_conn):
    result = select_planner_route(db_conn, baseline_targets=(), role_targets=())
    assert result.outcome == RoutingOutcome.NO_ELIGIBLE_PLANNER_WORKER
    assert result.binding is None


def test_configured_but_uncertified_worker_is_excluded(db_conn):
    WorkersRepo(db_conn).register(worker_id="w1", kind="fake", network_class="local")
    result = select_planner_route(
        db_conn, baseline_targets=(_target("w1"),), role_targets=(_role_target("w1"),),
    )
    assert result.outcome == RoutingOutcome.NO_ELIGIBLE_PLANNER_WORKER


def test_exactly_one_eligible_candidate_is_selected(db_conn):
    binding = _certify_eligible(db_conn, "w1")
    result = select_planner_route(
        db_conn, baseline_targets=(_target("w1"),), role_targets=(_role_target("w1"),),
    )
    assert result.outcome == RoutingOutcome.SELECTED
    assert result.binding == binding


def test_multiple_eligible_candidates_fails_closed_never_picks_one(db_conn):
    _certify_eligible(db_conn, "w1", model_tag="devstral:24b")
    _certify_eligible(db_conn, "w2", model_tag="qwen3-coder:30b")
    result = select_planner_route(
        db_conn,
        baseline_targets=(
            _target("w1", model_tag="devstral:24b"), _target("w2", model_tag="qwen3-coder:30b"),
        ),
        role_targets=(_role_target("w1"), _role_target("w2")),
    )
    assert result.outcome == RoutingOutcome.MULTIPLE_ELIGIBLE_PLANNER_WORKERS
    assert result.binding is None
    assert set(result.candidate_worker_ids) == {"w1", "w2"}


def test_archived_configured_worker_is_excluded(db_conn):
    _certify_eligible(db_conn, "w1")
    archived = archive_worker(db_conn, worker_id="w1")
    assert archived.ok and archived.changed
    result = select_planner_route(
        db_conn, baseline_targets=(_target("w1"),), role_targets=(_role_target("w1"),),
    )
    assert result.outcome == RoutingOutcome.NO_ELIGIBLE_PLANNER_WORKER


def test_historical_db_worker_absent_from_current_config_is_never_a_candidate(db_conn):
    """A worker fully certified and eligible in the DB, but simply not
    present in the CURRENT `baseline_targets`/`role_targets` snapshot
    (H.3's own precedent: archived/historical workers are preserved in
    the DB forever, never auto-purged), must never become a routing
    candidate -- enumeration is driven by config, never a DB scan."""
    _certify_eligible(db_conn, "legacy-worker")
    result = select_planner_route(db_conn, baseline_targets=(), role_targets=())
    assert result.outcome == RoutingOutcome.NO_ELIGIBLE_PLANNER_WORKER


def test_no_role_target_for_planner_excludes_worker_even_with_baseline_target(db_conn):
    _certify_eligible(db_conn, "w1")
    result = select_planner_route(
        db_conn, baseline_targets=(_target("w1"),), role_targets=(),
    )
    assert result.outcome == RoutingOutcome.NO_ELIGIBLE_PLANNER_WORKER


# -- revalidate_route_binding(): execution-time exact-match recheck ---------


def test_revalidate_legacy_unbound_job_refuses_first(db_conn):
    result = revalidate_route_binding(
        db_conn, None, baseline_targets=(), role_targets=(), verify_live_runtime=False,
    )
    assert not result.ok
    assert result.outcome == RevalidationOutcome.WORKER_UNBOUND
    assert result.failure_reason == "planner_worker_unbound"


def test_revalidate_matching_binding_ok_without_live_probe(db_conn):
    binding = _certify_eligible(db_conn, "w1")
    result = revalidate_route_binding(
        db_conn, binding, baseline_targets=(_target("w1"),), role_targets=(_role_target("w1"),),
        verify_live_runtime=False,
    )
    assert result.ok
    assert result.outcome == RevalidationOutcome.OK


def test_revalidate_refuses_when_worker_archived(db_conn):
    binding = _certify_eligible(db_conn, "w1")
    archive_worker(db_conn, worker_id="w1")
    result = revalidate_route_binding(
        db_conn, binding, baseline_targets=(_target("w1"),), role_targets=(_role_target("w1"),),
        verify_live_runtime=False,
    )
    assert not result.ok
    assert result.outcome == RevalidationOutcome.WORKER_ARCHIVED


def test_revalidate_refuses_when_worker_no_longer_in_config(db_conn):
    binding = _certify_eligible(db_conn, "w1")
    result = revalidate_route_binding(
        db_conn, binding, baseline_targets=(), role_targets=(), verify_live_runtime=False,
    )
    assert not result.ok
    assert result.outcome == RevalidationOutcome.WORKER_NOT_CONFIGURED


def test_revalidate_refuses_when_security_certificate_changed(db_conn):
    binding = _certify_eligible(db_conn, "w1")
    # A NEW certificate replaces the authority this binding was issued
    # under -- even though the worker is still eligible overall, it is
    # eligible under a DIFFERENT certificate_id now.
    profile = _profile()
    replacement = record_baseline_certificate(
        db_conn, worker_id="w1", runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS, evidence_ref="sec-ev-2", reason="re-certified",
    )
    assert replacement.ok
    assert replacement.certificate.certificate_id != binding.security_certificate_id
    result = revalidate_route_binding(
        db_conn, binding, baseline_targets=(_target("w1"),), role_targets=(_role_target("w1"),),
        verify_live_runtime=False,
    )
    assert not result.ok
    assert result.outcome == RevalidationOutcome.ROUTE_BINDING_STALE
    assert result.failure_reason == "planner_route_binding_stale"


def test_revalidate_refuses_when_output_token_budget_changed_in_config(db_conn):
    binding = _certify_eligible(db_conn, "w1")
    changed_role_target = RoleEvaluationTarget(
        worker_id="w1", role=ProductionRole.PLANNER,
        output_token_budget=8192,  # different from the certified 4096
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT, policy_version=POLICY_VERSION,
    )
    result = revalidate_route_binding(
        db_conn, binding, baseline_targets=(_target("w1"),), role_targets=(changed_role_target,),
        verify_live_runtime=False,
    )
    assert not result.ok
    # A different output_token_budget changes the role-evaluation
    # fingerprint itself, so this worker is no longer eligible under
    # that (now-unmatched) role-evaluation profile at all.
    assert result.outcome in (
        RevalidationOutcome.ROUTE_BINDING_STALE, RevalidationOutcome.WORKER_NOT_ELIGIBLE,
    )


def test_revalidate_ok_then_live_probe_reachable(db_conn, ollama_server):
    binding = _certify_eligible(db_conn, "w1", root=ollama_server)
    result = revalidate_route_binding(
        db_conn, binding, baseline_targets=(_target("w1", root=ollama_server),),
        role_targets=(_role_target("w1"),), verify_live_runtime=True,
    )
    assert result.ok


def test_revalidate_refuses_when_live_runtime_unreachable(db_conn):
    """`OLLAMA_ROOT` (module default) is never a real, listening
    server -- proves the live-probe step actually runs and actually
    refuses, rather than merely being wired up but never exercised."""
    binding = _certify_eligible(db_conn, "w1")
    result = revalidate_route_binding(
        db_conn, binding, baseline_targets=(_target("w1"),), role_targets=(_role_target("w1"),),
        verify_live_runtime=True,
    )
    assert not result.ok
    assert result.outcome == RevalidationOutcome.RUNTIME_UNREACHABLE


# -- route_binding_from_job() -------------------------------------------------


class _FakeJobRow:
    def __init__(self, **kwargs):
        for key in (
            "worker_id", "runtime_identity_fingerprint", "role_evaluation_fingerprint",
            "security_certificate_id", "role_certificate_id", "output_token_budget",
            "tool_choice_enforcement", "planner_policy_version",
        ):
            setattr(self, key, kwargs.get(key))


def test_route_binding_from_job_reconstructs_a_bound_job():
    row = _FakeJobRow(
        worker_id="w1", runtime_identity_fingerprint="rf", role_evaluation_fingerprint="ef",
        security_certificate_id="sec-1", role_certificate_id="role-1",
        output_token_budget=4096, tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        planner_policy_version=POLICY_VERSION,
    )
    binding = route_binding_from_job(row)
    assert binding == PlannerRouteBinding(
        worker_id="w1", runtime_identity_fingerprint="rf", role_evaluation_fingerprint="ef",
        security_certificate_id="sec-1", role_certificate_id="role-1",
        output_token_budget=4096, tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        planner_policy_version=POLICY_VERSION,
    )


def test_route_binding_from_job_is_none_for_legacy_unbound_job():
    row = _FakeJobRow(worker_id=None)
    assert route_binding_from_job(row) is None
