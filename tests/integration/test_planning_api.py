"""Engineering planning over HTTP (Phase 8.2/8.2d, H.4 worker-bound
routing) — thin endpoints, no persistence/planning policy of their own;
strict field allowlists; explicit unconfigured state; read-only/no-
mutation guarantees end to end; durable background job execution
decoupled from HTTP request lifetime.

H.4 routing POLICY (candidate selection, zero/one/multiple, execution-
time revalidation, no-reroute, create/archive concurrency) has its own
dedicated coverage in `tests/unit/test_planner_routing.py` /
`test_planning_job_route_binding.py` / `test_planner_route_concurrency.py`.
This file seeds one fixed, already-eligible worker (`_seed_route()`)
purely so the HTTP contract itself (status codes, field allowlists,
durable job status shape, no-mutation guarantees) can be exercised
without every test re-deriving eligibility, and points every worker at
a real, reachable fake Ollama HTTP server (`ollama_server`) since H.4
execution-time route revalidation makes a genuine live-runtime probe
before ever constructing a Planner.
"""

from __future__ import annotations

import http.server
import json
import threading
import time

import pytest

from code_slayer.api import create_app
from code_slayer.api.service import RuntimeBindings, WorkerRegistration
from code_slayer.planning.fake_planner import FakePlanner
from code_slayer.planning.planner import PlannerOutcome, PlannerResponse, parse_planner_output
from code_slayer.runner import LocalWorkerRunner
from code_slayer.security.certification_service import (
    BaselineCertificationTarget,
    RoleEvaluationTarget,
)
from code_slayer.security.live_certification import LiveOllamaRuntimeExpectation
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
from tests.repo_helpers import git

REQUEST = "Add a read-only endpoint reporting snapshot age."
_FAST_POLL = 0.02
_created_apps: list = []

WORKER_ID = "w1"
POLICY_VERSION = "planner-certification-v1"
OUTPUT_TOKEN_BUDGET = 4096
TOOL_CHOICE_ENFORCEMENT = "ADVISORY_ONLY_UNVERIFIED"


@pytest.fixture(autouse=True)
def _stop_planning_executors():
    """Every `application()` call in this file starts a real background
    planning-job dispatcher thread (Phase 8.2d); without this, dozens of
    them would leak, live, for the rest of the test session."""
    yield
    while _created_apps:
        _created_apps.pop().extensions["codeslayer"].close()


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


def structured_response(**overrides):
    data = {
        "goal": "Add the endpoint", "requirements": [], "assumptions": [],
        "affected_files": [], "planned_changes": [], "dependencies": [], "risks": [],
        "verification_steps": [], "discovered_commands": [], "authority_requirements": [],
        "evidence_claims": [], "ambiguities": [],
    }
    data.update(overrides)
    output = parse_planner_output(data)
    assert output is not None
    return PlannerResponse(PlannerOutcome.STRUCTURED, output=output, raw="{}")


def _seed_eligible_worker(app, root: str, *, worker_id: str = WORKER_ID) -> None:
    """H.4: register `worker_id` (already done via `worker_registrations`
    when `application()` builds `RuntimeBindings` — this only adds the
    real Baseline Security PASS + Planner role PASS certificates so
    `planning.routing.select_planner_route()`'s canonical eligibility
    call actually finds it eligible) directly against the app's own
    production control-plane database."""
    service = app.extensions["codeslayer"]
    with service.planning() as planning:
        conn = planning.production_conn()
        profile = runtime_profile_identity_from_config(
            model_tag="devstral:24b", model_digest="sha256:abc", endpoint=f"{root}/v1",
            runtime_version="0.16.1", effective_context_tokens=16384, temperature=0.0,
            normalizer_id=None, normalizer_version=None,
        )
        security = record_baseline_certificate(
            conn, worker_id=worker_id, runtime_profile=profile,
            outcome=SecurityBaselineOutcome.PASS, evidence_ref="sec-ev", reason="ok",
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
            classification="PASS_FIRST_TRY", evidence_ref="role-ev", reason="ok",
            role_evaluation=role_evaluation,
        )
        assert role.ok, role.reason


def _baseline_target(root: str, *, worker_id: str = WORKER_ID) -> BaselineCertificationTarget:
    profile = runtime_profile_identity_from_config(
        model_tag="devstral:24b", model_digest="sha256:abc", endpoint=f"{root}/v1",
        runtime_version="0.16.1", effective_context_tokens=16384, temperature=0.0,
        normalizer_id=None, normalizer_version=None,
    )
    return BaselineCertificationTarget(
        worker_id=worker_id,
        expectation=LiveOllamaRuntimeExpectation(
            ollama_root=root, model_tag="devstral:24b", model_digest="sha256:abc",
            runtime_version="0.16.1", effective_context_tokens=16384, temperature=0.0,
            expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
            normalizer_id=None, normalizer_version=None,
        ),
    )


def _role_target(*, worker_id: str = WORKER_ID) -> RoleEvaluationTarget:
    return RoleEvaluationTarget(
        worker_id=worker_id, role=ProductionRole.PLANNER,
        output_token_budget=OUTPUT_TOKEN_BUDGET, tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        policy_version=POLICY_VERSION,
    )


def application(repo, ollama_server, *, planner_responses=None, poll_interval=_FAST_POLL):
    """A fully H.4-eligible app: one registered, certified worker
    (`WORKER_ID`) pointed at the real `ollama_server` fixture, and a
    `planner_factory_for_worker` that ignores the exact worker/job id
    and always returns the SAME scripted `FakePlanner` — this file
    tests the HTTP contract, not routing policy, so every test wants
    exactly one deterministic, always-eligible candidate."""
    planner = FakePlanner(planner_responses if planner_responses is not None else [])
    bindings = RuntimeBindings(
        planner_factory_for_worker=lambda worker_id, job_id: planner,
        planning_poll_interval_seconds=poll_interval,
        worker_registrations=(
            WorkerRegistration(
                worker_id=WORKER_ID, kind="openai_compatible", network_class="local",
            ),
        ),
        baseline_certification_targets=(_baseline_target(ollama_server),),
        role_evaluation_targets=(_role_target(),),
    )
    app = create_app(repo, bindings=bindings)
    _created_apps.append(app)
    _seed_eligible_worker(app, ollama_server)
    return app.test_client(), planner


def wait_for_job(client, job_id, *, timeout=5.0):
    """Poll durable job status until terminal -- the same thing a real
    WebUI client does; never assumes a specific number of polls."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/planning-jobs/{job_id}").get_json()
        if job["state"] in ("SUCCEEDED", "FAILED"):
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach a terminal state within {timeout}s")


def test_disconnected_unconfigured_planner_state_is_explicit(git_repo_with_commit):
    client = create_app(git_repo_with_commit, bindings=RuntimeBindings()).test_client()
    health = client.get("/api/health").json
    assert health["actions"]["planning_configured"] is False
    response = client.post("/api/plans", json={"request": REQUEST})
    assert response.status_code == 503
    assert response.json["error"]["code"] == "planner_not_configured"
    # No orphaned QUEUED job/plan is ever created when unconfigured.
    assert client.get("/api/planning-jobs").get_json()["jobs"] == []
    assert client.get("/api/plans").get_json()["plans"] == []


def test_create_returns_202_immediately_then_reaches_ready_over_http(
    git_repo_with_commit, ollama_server,
):
    client, _ = application(
        git_repo_with_commit, ollama_server, planner_responses=[structured_response()],
    )
    created = client.post("/api/plans", json={"request": REQUEST})
    assert created.status_code == 202
    job = created.json
    assert job["state"] == "QUEUED"
    assert job["worker_id"] == WORKER_ID
    assert job["output_token_budget"] == OUTPUT_TOKEN_BUDGET
    plan_id = job["plan_id"]
    assert created.headers["Location"] == job["status_url"] == f"/api/planning-jobs/{job['job_id']}"

    # The freshly created plan is already real and inspectable, even
    # before the background job has run at all.
    detail = client.get(f"/api/plans/{plan_id}")
    assert detail.status_code == 200 and detail.json["state"] == "DRAFT"

    finished = wait_for_job(client, job["job_id"])
    assert finished["state"] == "SUCCEEDED"
    assert finished["failure_category"] is None

    plan = client.get(f"/api/plans/{plan_id}").get_json()
    assert plan["state"] == "READY"
    assert plan["content"]["goal"] == "Add the endpoint"

    listed = client.get("/api/plans")
    assert any(p["plan_id"] == plan_id for p in listed.json["plans"])
    jobs_listed = client.get("/api/planning-jobs")
    assert any(j["job_id"] == job["job_id"] for j in jobs_listed.json["jobs"])

    assert client.get("/api/plans/does-not-exist").status_code == 404
    assert client.get("/api/planning-jobs/does-not-exist").status_code == 404


def test_needs_input_result_is_a_successful_job(git_repo_with_commit, ollama_server):
    """job = SUCCEEDED, plan = NEEDS_INPUT is a valid, distinct outcome."""
    blocked = structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])
    responses = [blocked, structured_response()]
    client, _ = application(git_repo_with_commit, ollama_server, planner_responses=responses)
    created = client.post("/api/plans", json={"request": REQUEST})
    job = created.json
    finished = wait_for_job(client, job["job_id"])
    assert finished["state"] == "SUCCEEDED"  # the turn itself succeeded
    plan_id = job["plan_id"]
    assert client.get(f"/api/plans/{plan_id}").json["state"] == "NEEDS_INPUT"


def test_blocked_plan_resolution_and_async_replan_over_http(git_repo_with_commit, ollama_server):
    blocked = structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])
    responses = [blocked, structured_response()]
    client, _ = application(git_repo_with_commit, ollama_server, planner_responses=responses)
    created = client.post("/api/plans", json={"request": REQUEST})
    job = created.json
    wait_for_job(client, job["job_id"])
    plan_id = job["plan_id"]
    assert client.get(f"/api/plans/{plan_id}").json["state"] == "NEEDS_INPUT"

    resolved = client.post(f"/api/plans/{plan_id}/resolutions", json={
        "ambiguity_id": "scope", "answer": "JSON", "resolution_kind": "FACT",
    })
    # Recording an answer alone never advances state -- resume
    # (synchronous, no inference) re-evaluates the Question Gate.
    assert resolved.status_code == 200 and resolved.json["state"] == "NEEDS_INPUT"
    resumed = client.post(f"/api/plans/{plan_id}/resume", json={})
    assert resumed.status_code == 200 and resumed.json["state"] == "READY"

    replanned = client.post(f"/api/plans/{plan_id}/replan", json={})
    assert replanned.status_code == 202
    replan_job = replanned.json
    assert replan_job["kind"] == "replan"
    assert replan_job["worker_id"] == WORKER_ID
    assert client.get(f"/api/plans/{plan_id}").json["state"] == "SUPERSEDED"
    finished = wait_for_job(client, replan_job["job_id"])
    assert finished["state"] == "SUCCEEDED"
    new_plan = client.get(f"/api/plans/{replan_job['plan_id']}").get_json()
    assert new_plan["predecessor_plan_id"] == plan_id
    assert new_plan["state"] == "READY"


@pytest.mark.parametrize("field", [
    "repo_path", "db_path", "worktree_path", "trust_level", "lease_generation",
    "fencing_token", "worker_session_id", "checkpoint_id", "worker_authority",
    "cloud_escalation", "allow_cloud", "command", "execute", "owner_pid",
    "owner_generation", "job_id",
    # H.4: no HTTP caller may select, name, or assert the worker/model/
    # certificate authority for a planning mutation -- the backend
    # always selects it.
    "worker_id", "model", "certificate_id", "runtime_identity_fingerprint",
])
def test_api_rejects_forbidden_authority_fields(git_repo_with_commit, ollama_server, field):
    client, _ = application(
        git_repo_with_commit, ollama_server, planner_responses=[structured_response()] * 2,
    )
    assert client.post(
        "/api/plans", json={"request": REQUEST, field: "forged"},
    ).status_code == 400
    created = client.post("/api/plans", json={"request": REQUEST})
    job = created.json
    wait_for_job(client, job["job_id"])
    plan_id = job["plan_id"]
    assert client.post(
        f"/api/plans/{plan_id}/resume", json={field: "forged"},
    ).status_code == 400
    assert client.post(
        f"/api/plans/{plan_id}/replan", json={field: "forged"},
    ).status_code == 400
    assert client.post(
        f"/api/plans/{plan_id}/resolutions",
        json={"ambiguity_id": "x", "answer": "y", "resolution_kind": "FACT", field: "forged"},
    ).status_code == 400


def test_post_plans_with_worker_id_creates_nothing(git_repo_with_commit, ollama_server):
    """H.4 mandatory regression: `POST /api/plans` with a client-supplied
    `worker_id` must fail closed with `invalid_fields` -- no plan, no
    job, ever created -- proving the closed-set body parser is the real
    authority boundary, not merely a route the client happened not to
    exercise."""
    client, _ = application(git_repo_with_commit, ollama_server)
    response = client.post("/api/plans", json={"request": REQUEST, "worker_id": WORKER_ID})
    assert response.status_code == 400
    assert response.json["error"]["code"] == "invalid_fields"
    assert client.get("/api/planning-jobs").get_json()["jobs"] == []
    assert client.get("/api/plans").get_json()["plans"] == []


def test_malformed_planner_output_surfaces_as_failed_job_and_draft_plan(
    git_repo_with_commit, ollama_server,
):
    client, _ = application(
        git_repo_with_commit, ollama_server,
        planner_responses=[PlannerResponse(PlannerOutcome.MALFORMED)],
    )
    created = client.post("/api/plans", json={"request": REQUEST})
    assert created.status_code == 202
    job = created.json
    finished = wait_for_job(client, job["job_id"])
    assert finished["state"] == "FAILED"
    assert finished["failure_category"]  # a safe, coarse code -- never None, never raw text
    plan = client.get(f"/api/plans/{job['plan_id']}").get_json()
    assert plan["state"] == "DRAFT"
    assert plan["reason"].startswith("malformed_planner_output:")


def test_planning_over_http_never_mutates_repository_or_executes(
    git_repo_with_commit, ollama_server,
):
    runner = LocalWorkerRunner(git_repo_with_commit)
    before_head = git(git_repo_with_commit, "rev-parse", "HEAD")
    before_status = git(git_repo_with_commit, "status", "--porcelain")
    client, _ = application(
        git_repo_with_commit, ollama_server,
        planner_responses=[structured_response(
            discovered_commands=[
                {"command": "rm -rf /", "purpose": "test", "evidence_source": "fabricated"},
            ],
        )],
    )
    created = client.post("/api/plans", json={"request": REQUEST})
    wait_for_job(client, created.json["job_id"])
    assert git(git_repo_with_commit, "rev-parse", "HEAD") == before_head
    assert git(git_repo_with_commit, "status", "--porcelain") == before_status
    runner.close()


# -- H.4 review finding: the executor must observe config changed AFTER --
# -- construction, never a stale snapshot captured at process start ------


def test_executor_observes_persistent_config_changed_after_construction(
    git_repo_with_commit, ollama_server, tmp_path,
):
    """No restart anywhere in this test, and no dependence on winning a
    scheduler race. Structure (H.4 review finding -- the original
    version of this test was race-prone: `ApplicationService.
    create_plan()` calls `executor.notify()` before the POST even
    returns, so the executor could consume the OLD config before
    `save_config()` below ever ran):

    1. `ApplicationService`/`PlanningJobExecutor` are constructed with a
       deliberately huge poll interval, so nothing EVER wakes the
       dispatcher's poll loop on its own within this test's lifetime --
       only an explicit `notify()` call can.
    2. `start()`'s own immediate startup discovery pass runs BEFORE any
       job exists at all, and finds nothing.
    3. The job is created directly and durably through
       `EngineeringPlanningService.create_job()` against the SAME
       underlying database the app's executor watches --
       deliberately bypassing `ApplicationService.create_plan()`, whose
       own `executor.notify()` is exactly the race this test must not
       depend on.
    4. Persistent config is changed (`output_token_budget=4096` ->
       `9999`, deliberately with NO matching new certificate) --
       nothing could possibly have consumed it yet; the executor has
       not been woken since `start()`'s own pre-job discovery pass.
    5. Only THEN is the executor explicitly `notify()`d.

    If the executor's `bindings_factory` resolved a stale snapshot from
    construction time, the job's own certified binding (4096) would
    still match current config and it would SUCCEED. It must instead
    FAIL -- a changed `output_token_budget` changes the role-evaluation
    fingerprint eligibility is computed against, so with no matching
    new certificate the worker becomes ineligible under the fresh
    profile -- externally observable proof the change was actually
    seen fresh."""
    from dataclasses import replace

    from code_slayer.config.schema import CSLRConfig, OllamaServerConfig, WorkerRuntimeConfig
    from code_slayer.config.store import save_config
    from code_slayer.planning.routing import RoutingOutcome, select_planner_route
    from code_slayer.planning.service import EngineeringPlanningService

    config_path = tmp_path / "config.toml"
    state_root = tmp_path / "_codeslayer_state"
    state_root.mkdir()
    server_config = OllamaServerConfig(server_id="ollama-1", origin=ollama_server)
    worker_config = WorkerRuntimeConfig(
        worker_id=WORKER_ID, kind="openai_compatible", network_class="local",
        ollama_server_id="ollama-1", model_tag="devstral:24b",
        approved_model_digest="sha256:abc", approved_runtime_version="0.16.1",
        effective_context_tokens=16384, temperature=0.0,
        normalizer_id=None, normalizer_version=None,
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        planner_policy_version=POLICY_VERSION,
    )
    save_config(
        CSLRConfig(ollama_servers=(server_config,), workers=(worker_config,)), path=config_path,
    )

    planner = FakePlanner([structured_response()])
    app = create_app(
        git_repo_with_commit, state_root=state_root,
        config_path=config_path, load_persistent_config=True,
        bindings=RuntimeBindings(
            planner_factory_for_worker=lambda worker_id, job_id: planner,
            # Deliberately enormous -- the poll loop must never fire on
            # its own within this test; only the explicit notify()
            # below may wake it.
            planning_poll_interval_seconds=3600.0,
        ),
    )
    _created_apps.append(app)
    _seed_eligible_worker(app, ollama_server)  # certifies AT output_token_budget=4096

    service = app.extensions["codeslayer"]
    direct = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        selection = select_planner_route(
            direct.production_conn(),
            baseline_targets=service.bindings.baseline_certification_targets,
            role_targets=service.bindings.role_evaluation_targets,
        )
        assert selection.outcome == RoutingOutcome.SELECTED
        job = direct.create_job(
            original_request=REQUEST, route_binding=selection.binding,
            baseline_targets=service.bindings.baseline_certification_targets,
            role_targets=service.bindings.role_evaluation_targets,
        )
    finally:
        direct.close()
    assert job.output_token_budget == OUTPUT_TOKEN_BUDGET
    assert job.state == "QUEUED"  # durably accepted; the executor has not been woken at all yet

    # Change persistent config -- nothing could possibly have consumed
    # it yet (no notify() has been sent since app construction's own
    # pre-job discovery pass, which found no job at all).
    changed_worker_config = replace(worker_config, output_token_budget=9999)
    save_config(
        CSLRConfig(ollama_servers=(server_config,), workers=(changed_worker_config,)),
        path=config_path,
    )

    # Only now does the dispatcher get any signal to act at all.
    service._planning_executor.notify()

    client = app.test_client()
    finished = wait_for_job(client, job.job_id)
    assert finished["state"] == "FAILED"
    assert finished["failure_category"] == "routing"
    assert finished["failure_reason"] == (
        "planner_worker_not_eligible:role_certificate_evaluation_profile_mismatch"
    )
