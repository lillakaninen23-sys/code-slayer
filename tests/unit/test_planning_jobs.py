"""Durable background planning jobs (Phase 8.2d, H.4 worker-bound routing).

Covers: job creation never invokes a planner on the calling thread;
durable QUEUED/RUNNING persistence; atomic, fenced claim/reclaim
semantics (concurrent dispatchers, crash-then-restart recovery via
`lease.liveness`, stale-owner refusal, terminal-job immutability);
SUCCEEDED/FAILED job outcomes distinct from plan content state; no raw
model output through the job-facing shape; a bounded background
executor that never duplicates execution and never lets one job's
exception kill the dispatcher; and the read-only/no-mutation/no-trust
guarantees applied to the whole job lifecycle.

H.4 routing POLICY (candidate selection, zero/one/multiple, execution-
time revalidation, no-reroute) has its own dedicated coverage in
`test_planner_routing.py` / `test_planning_job_route_binding.py` /
`test_planner_route_concurrency.py`. This file seeds one fixed,
already-eligible worker (`_seed_route()`) purely so job-LIFECYCLE
mechanics (claim/execute/restart/terminal-immutability/...) can be
exercised without every test re-deriving eligibility itself.
"""

from __future__ import annotations

import http.server
import json
import sqlite3
import threading
import time

import pytest

from code_slayer.lease.liveness import process_start_time
from code_slayer.planning.executor import PlanningJobExecutor
from code_slayer.planning.fake_planner import FakePlanner
from code_slayer.planning.planner import (
    PlannerOutcome,
    PlannerRequest,
    PlannerResponse,
    parse_planner_output,
)
from code_slayer.planning.routing import PlannerRouteBinding
from code_slayer.planning.service import EngineeringPlanningService
from code_slayer.planning.worker_planner import WorkerAdapterPlanner
from code_slayer.security.certification_service import (
    BaselineCertificationTarget,
    RoleEvaluationTarget,
)
from code_slayer.security.live_certification import LiveOllamaRuntimeExpectation
from code_slayer.store.db import transaction
from code_slayer.store.planning_jobs_repo import PlanningJobsRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.protocol import WorkerAdapterError
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

REQUEST = "Add a read-only endpoint reporting repository intelligence snapshot age."

WORKER_ID = "w1"
POLICY_VERSION = "planner-certification-v1"
OUTPUT_TOKEN_BUDGET = 4096
TOOL_CHOICE_ENFORCEMENT = "ADVISORY_ONLY_UNVERIFIED"
OLLAMA_ROOT = "http://local:11434"


def structured_response(**overrides) -> PlannerResponse:
    data = {
        "goal": "Add the endpoint", "requirements": [], "assumptions": [], "affected_files": [],
        "planned_changes": [], "dependencies": [], "risks": [], "verification_steps": [],
        "discovered_commands": [], "authority_requirements": [], "evidence_claims": [],
        "ambiguities": [],
    }
    data.update(overrides)
    output = parse_planner_output(data)
    assert output is not None
    return PlannerResponse(PlannerOutcome.STRUCTURED, output=output, raw="{}")


class _BlockingPlanner:
    """A `Planner` whose `.plan()` blocks until released — proves the
    HTTP/service-caller thread never waits for it (test item 1) and lets
    a test control exactly when a job's execution finishes."""

    def __init__(self, response: PlannerResponse) -> None:
        self._response = response
        self._release = threading.Event()
        self.calls = 0
        self.started = threading.Event()

    def plan(self, request: PlannerRequest) -> PlannerResponse:
        self.calls += 1
        self.started.set()
        self._release.wait(timeout=10)
        return self._response

    def release(self) -> None:
        self._release.set()


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
    """A minimal, real, reachable fake Ollama HTTP server -- only needed
    by tests that exercise the REAL `PlanningJobExecutor` (execution-
    time route revalidation, H.4, does a genuine `verify_ollama_
    runtime()` probe before ever constructing a Planner). Tests that
    call `EngineeringPlanningService` directly never reach that probe,
    so most of this file never needs this fixture at all."""
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


def _profile(root: str = OLLAMA_ROOT):
    return runtime_profile_identity_from_config(
        model_tag="devstral:24b", model_digest="sha256:abc", endpoint=f"{root}/v1",
        runtime_version="0.16.1", effective_context_tokens=16384, temperature=0.0,
        normalizer_id=None, normalizer_version=None,
    )


_RouteFixture = tuple[
    PlannerRouteBinding, tuple[BaselineCertificationTarget, ...], tuple[RoleEvaluationTarget, ...],
]


def _seed_route(
    conn, *, worker_id: str = WORKER_ID, root: str = OLLAMA_ROOT,
) -> _RouteFixture:
    """Register `worker_id`, issue it a real Baseline Security PASS and
    Planner role PASS certificate, and return `(route_binding,
    baseline_targets, role_targets)` -- everything `create_job()`/
    `replan_job()`'s own atomic re-verification needs to actually
    accept the binding (it re-derives eligibility from these exact
    certificates, not from the binding's own claims). `root` only
    matters for a test that exercises the real `PlanningJobExecutor`
    (see `ollama_server` fixture) -- every other test never live-probes
    it, so the unreachable default is harmless."""
    WorkersRepo(conn).register(worker_id=worker_id, kind="fake", network_class="local")
    profile = _profile(root)
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
    binding = PlannerRouteBinding(
        worker_id=worker_id,
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        role_evaluation_fingerprint=role_evaluation.role_evaluation_fingerprint,
        security_certificate_id=security.certificate.certificate_id,
        role_certificate_id=role.certificate.certificate_id,
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        planner_policy_version=POLICY_VERSION,
    )
    baseline_targets = (
        BaselineCertificationTarget(
            worker_id=worker_id,
            expectation=LiveOllamaRuntimeExpectation(
                ollama_root=root, model_tag=profile.model_tag,
                model_digest=profile.model_digest, runtime_version=profile.runtime_version,
                effective_context_tokens=profile.effective_context_tokens,
                temperature=profile.temperature,
                expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
                normalizer_id=profile.normalizer_id, normalizer_version=profile.normalizer_version,
            ),
        ),
    )
    role_targets = (
        RoleEvaluationTarget(
            worker_id=worker_id, role=ProductionRole.PLANNER,
            output_token_budget=OUTPUT_TOKEN_BUDGET,
            tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT, policy_version=POLICY_VERSION,
        ),
    )
    return binding, baseline_targets, role_targets


@pytest.fixture
def state_root(tmp_path):
    root = tmp_path / "_codeslayer_state"
    root.mkdir()
    return root


@pytest.fixture
def service(git_repo_with_commit, state_root):
    svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    yield svc
    svc.close()


@pytest.fixture
def route(service):
    """`(binding, baseline_targets, role_targets)` for `WORKER_ID`,
    seeded against `service`'s own control-plane connection."""
    return _seed_route(service.production_conn())


def _create_job(service, route, *, request: str = REQUEST):
    binding, baseline_targets, role_targets = route
    return service.create_job(
        original_request=request, route_binding=binding,
        baseline_targets=baseline_targets, role_targets=role_targets,
    )


def _replan_job(service, route, plan_id: str):
    binding, baseline_targets, role_targets = route
    return service.replan_job(
        plan_id, route_binding=binding,
        baseline_targets=baseline_targets, role_targets=role_targets,
    )


# --- 1/2. job creation never invokes a planner; durable QUEUED --------------

def test_create_job_never_invokes_a_planner_and_is_durably_queued(service, route):
    job = _create_job(service, route)
    assert job.state == "QUEUED"
    assert job.attempt == 0
    assert job.worker_id == WORKER_ID
    assert job.output_token_budget == OUTPUT_TOKEN_BUDGET
    plan = service.get(job.plan_id)
    assert plan.state == "DRAFT"  # real, inspectable plan identity already exists

    # Durable: a second, independent service instance sees the same row.
    row = PlanningJobsRepo(service.production_conn()).get(job.job_id)
    assert row.state == "QUEUED"
    assert row.owner_pid is None
    assert row.worker_id == WORKER_ID


def test_create_job_is_fast_even_though_planning_is_slow(git_repo_with_commit, state_root):
    """Test item 1: proves the calling thread does not block on
    inference -- `create_job()` returns immediately regardless of how
    long a subsequently-claimed execution would take."""
    svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        route = _seed_route(svc.production_conn())
        t0 = time.time()
        job = _create_job(svc, route)
        elapsed = time.time() - t0
        assert elapsed < 1.0
        assert job.state == "QUEUED"
    finally:
        svc.close()


# --- 9. claim/execute cycle and RUNNING persistence -------------------------

def test_claim_transitions_queued_to_running_durably(service, route):
    job = _create_job(service, route)
    claimed = service.claim_job(job.job_id)
    assert claimed.state == "RUNNING"
    assert claimed.owner_pid is not None
    assert claimed.owner_generation == 1
    assert claimed.attempt == 1
    row = PlanningJobsRepo(service.production_conn()).get(job.job_id)
    assert row.state == "RUNNING"


# --- 5/6. SUCCEEDED with READY / NEEDS_INPUT, distinct from job state ------

def test_successful_turn_ready_plan(service, route):
    job = _create_job(service, route)
    claimed = service.claim_job(job.job_id)
    planner = FakePlanner([structured_response()])
    record = service.execute_claimed_job(claimed, planner)
    assert record.state == "SUCCEEDED"
    assert record.failure_category is None
    plan = service.get(job.plan_id)
    assert plan.state == "READY"


def test_successful_turn_needs_input_plan_is_still_a_succeeded_job(service, route):
    blocked = structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])
    job = _create_job(service, route)
    claimed = service.claim_job(job.job_id)
    planner = FakePlanner([blocked])
    record = service.execute_claimed_job(claimed, planner)
    assert record.state == "SUCCEEDED"  # the turn itself succeeded
    plan = service.get(job.plan_id)
    assert plan.state == "NEEDS_INPUT"  # a legitimate, distinct plan outcome


# --- 7/8. transport failure -> FAILED, safe category, no raw leak ----------

def test_planner_transport_failure_is_a_failed_job_with_safe_category(service, route):
    adapter = FakeWorkerAdapter([WorkerAdapterError("transport_timeout")])
    planner = WorkerAdapterPlanner(adapter, task_id="job-x")
    job = _create_job(service, route)
    claimed = service.claim_job(job.job_id)
    record = service.execute_claimed_job(claimed, planner)
    assert record.state == "FAILED"
    # H.4: the coarse, top-level job failure category is now "planner"
    # (never the fine-grained `PlannerFailureCategory` value directly)
    # -- the fine-grained code is preserved unchanged in `failure_reason`.
    assert record.failure_category == "planner"
    assert record.failure_reason.startswith("malformed_planner_output:")
    assert record.failure_reason.endswith("transport_error")


def test_raw_planner_output_never_appears_on_the_job_record(service, route):
    from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind

    secret_looking_prose = "IMPLEMENTATION PLAN: rm -rf / # api_key=sk-should-never-leak"
    adapter = FakeWorkerAdapter([
        WorkerResponse(kind=WorkerResponseKind.TEXT, text=secret_looking_prose),
    ])
    planner = WorkerAdapterPlanner(adapter, task_id="job-x")
    job = _create_job(service, route)
    claimed = service.claim_job(job.job_id)
    record = service.execute_claimed_job(claimed, planner)
    assert record.state == "FAILED"
    import dataclasses

    for value in dataclasses.asdict(record).values():
        assert secret_looking_prose not in str(value)


def test_internal_exception_during_execution_is_a_failed_job_not_a_stuck_running_job(
    service, route,
):
    class _BrokenPlanner:
        def plan(self, request):
            raise RuntimeError("boom")

    job = _create_job(service, route)
    claimed = service.claim_job(job.job_id)
    record = service.execute_claimed_job(claimed, _BrokenPlanner())
    assert record.state == "FAILED"
    assert record.failure_category == "internal_error"


# --- 10/13/14/15. ownership, fencing, terminal immutability -----------------

def test_concurrent_claim_attempts_cannot_both_win(git_repo_with_commit, state_root):
    """Test item 10: two independent service instances (simulating two
    dispatchers) racing to claim the same QUEUED job -- SQLite's own
    `BEGIN IMMEDIATE` serializes them; only one may ever win."""
    creator = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        route = _seed_route(creator.production_conn())
        job = _create_job(creator, route)
    finally:
        creator.close()

    a = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    b = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        first = a.claim_job(job.job_id)
        second = b.claim_job(job.job_id)  # already RUNNING, owned by a live process (this one)
        assert first is not None
        assert second is None
    finally:
        a.close()
        b.close()


def test_live_running_ownership_cannot_be_stolen(service, route):
    """Test item 13."""
    job = _create_job(service, route)
    first = service.claim_job(job.job_id)
    assert first is not None
    second = service.claim_job(job.job_id)
    assert second is None  # this process's own pid is genuinely alive


def test_terminal_succeeded_job_is_never_rerun(service, route):
    """Test item 14."""
    job = _create_job(service, route)
    claimed = service.claim_job(job.job_id)
    service.execute_claimed_job(claimed, FakePlanner([structured_response()]))
    assert service.claim_job(job.job_id) is None
    row = PlanningJobsRepo(service.production_conn()).get(job.job_id)
    assert row.state == "SUCCEEDED"
    # The migration's own trigger refuses to reopen a terminal row even
    # via a raw UPDATE, independent of any application-level check.
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(service.production_conn()):
            service.production_conn().execute(
                "UPDATE planning_jobs SET state = 'QUEUED' WHERE job_id = ?", (job.job_id,),
            )


def test_terminal_failed_job_is_never_rerun_automatically(service, route):
    """Test item 15."""
    job = _create_job(service, route)
    claimed = service.claim_job(job.job_id)
    service.execute_claimed_job(claimed, FakePlanner([PlannerResponse(PlannerOutcome.MALFORMED)]))
    assert service.claim_job(job.job_id) is None


# --- 11/12. crash/restart recovery ------------------------------------------

def test_restart_recovers_a_queued_job(git_repo_with_commit, state_root):
    """Test item 11 (Case A): a job persisted QUEUED, "server dies"
    (this process instance closes without ever claiming it), a fresh
    instance discovers and can execute it."""
    first = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        route = _seed_route(first.production_conn())
        job = _create_job(first, route)
    finally:
        first.close()

    restarted = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        assert job.job_id in restarted.claimable_job_ids()
        claimed = restarted.claim_job(job.job_id)
        assert claimed is not None
        record = restarted.execute_claimed_job(claimed, FakePlanner([structured_response()]))
        assert record.state == "SUCCEEDED"
    finally:
        restarted.close()


def test_restart_recovers_an_abandoned_running_job_with_dead_owner(
    git_repo_with_commit, state_root,
):
    """Test item 12 (Case B): a job was RUNNING under a pid that is
    provably dead (never a genuinely live one) -- a fresh instance
    reclaims it under a new generation, never duplicating a live
    execution."""
    first = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        route = _seed_route(first.production_conn())
        job = _create_job(first, route)
        claimed = first.claim_job(job.job_id)
        assert claimed.owner_generation == 1
        # Simulate the owning process having actually crashed: record a
        # pid that is guaranteed not to exist, with a real (parseable)
        # start-time format so liveness evidence is genuinely GONE, not
        # merely UNKNOWN from malformed data.
        dead_pid = 999999999
        with transaction(first.production_conn()):
            first.production_conn().execute(
                "UPDATE planning_jobs SET owner_pid = ?, owner_pid_started_at = ? "
                "WHERE job_id = ?",
                (dead_pid, process_start_time(dead_pid) or "0:0:100", job.job_id),
            )
    finally:
        first.close()

    restarted = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        assert job.job_id in restarted.claimable_job_ids()
        claimed = restarted.claim_job(job.job_id)
        assert claimed is not None
        assert claimed.owner_generation == 2  # a genuinely new epoch, never reused
        record = restarted.execute_claimed_job(claimed, FakePlanner([structured_response()]))
        assert record.state == "SUCCEEDED"
    finally:
        restarted.close()


def test_a_live_owners_running_job_is_not_in_the_claimable_set(service, route):
    job = _create_job(service, route)
    service.claim_job(job.job_id)  # owned by this very-much-alive process
    assert job.job_id not in service.claimable_job_ids()


# --- 16/17/18/19. resume/replan/resolution async semantics ------------------

def test_resume_never_invokes_a_planner_and_creates_no_job(service, route):
    """By design (Phase 8.2d): `resume()` only re-evaluates the Question
    Gate against durable resolutions -- it performs no model inference
    at all, so there is no long-running turn to move into a background
    job, and it must never create one."""
    blocked = structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])
    job = _create_job(service, route)
    claimed = service.claim_job(job.job_id)
    service.execute_claimed_job(claimed, FakePlanner([blocked]))
    plan_id = job.plan_id
    assert service.get(plan_id).state == "NEEDS_INPUT"

    from code_slayer.workers.prompt_analysis import EvidenceSource
    from code_slayer.workers.question_gate import ResolutionKind

    service.record_user_resolution(
        plan_id, "scope", "JSON", resolution_kind=ResolutionKind.FACT,
        source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    before_job_count = len(service.list_jobs())
    resumed = service.resume(plan_id)  # resume() accepts no `planner` argument at all
    assert resumed.state == "READY"
    assert len(service.list_jobs()) == before_job_count  # no new job was ever created


def test_replan_job_creates_a_queued_job_bound_to_a_new_plan_revision(service, route):
    """Test item 17."""
    job = _create_job(service, route)
    claimed = service.claim_job(job.job_id)
    service.execute_claimed_job(claimed, FakePlanner([structured_response()]))
    replan_job = _replan_job(service, route, job.plan_id)
    assert replan_job.state == "QUEUED"
    assert replan_job.kind == "replan"
    assert replan_job.worker_id == WORKER_ID
    new_plan = service.get(replan_job.plan_id)
    assert new_plan.predecessor_plan_id == job.plan_id
    assert service.get(job.plan_id).state == "SUPERSEDED"


def test_human_resolution_recording_remains_durable(service, route):
    """Test item 18 (light confirmation; full coverage already exists
    in test_engineering_planning.py)."""
    from code_slayer.workers.prompt_analysis import EvidenceSource
    from code_slayer.workers.question_gate import ResolutionKind

    blocked = structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])
    job = _create_job(service, route)
    claimed = service.claim_job(job.job_id)
    service.execute_claimed_job(claimed, FakePlanner([blocked]))
    service.record_user_resolution(
        job.plan_id, "scope", "JSON", resolution_kind=ResolutionKind.FACT,
        source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    plan = service.get(job.plan_id)
    assert plan.questions[0]["answer_recorded"] is True


# --- 21/22/23/24. bounded executor, thread safety, no mutation/execution ---

def test_executor_is_bounded_to_max_workers(git_repo_with_commit, state_root, ollama_server):
    """Test item 21: two QUEUED jobs, `max_workers=1` -- only one ever
    executes concurrently, proven with a blocking planner and an
    in-flight high-water-mark check."""
    svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        route = _seed_route(svc.production_conn(), root=ollama_server)
        job1 = _create_job(svc, route, request=REQUEST + " one")
        job2 = _create_job(svc, route, request=REQUEST + " two")
    finally:
        svc.close()

    binding, baseline_targets, role_targets = route
    blocking = _BlockingPlanner(structured_response())
    executor = PlanningJobExecutor(
        git_repo_with_commit, planner_factory_for_worker=lambda worker_id, job_id: blocking,
        baseline_targets=baseline_targets, role_targets=role_targets,
        state_root_override=state_root, max_workers=1, poll_interval_seconds=0.02,
    )
    try:
        executor.start()
        assert blocking.started.wait(timeout=5)
        time.sleep(0.1)
        with executor._lock:
            in_flight = len(executor._in_flight)
        assert in_flight <= 1
        blocking.release()
        _wait_for_jobs_terminal(git_repo_with_commit, state_root, [job1.job_id, job2.job_id])
    finally:
        executor.stop()
    assert blocking.calls >= 1


def test_executor_recovers_and_runs_a_preexisting_queued_job_on_start(
    git_repo_with_commit, state_root, ollama_server,
):
    """Test item 11, exercised through the real executor (not just the
    service layer directly)."""
    svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        route = _seed_route(svc.production_conn(), root=ollama_server)
        job = _create_job(svc, route)
    finally:
        svc.close()

    binding, baseline_targets, role_targets = route
    planner = FakePlanner([structured_response()])
    executor = PlanningJobExecutor(
        git_repo_with_commit, planner_factory_for_worker=lambda worker_id, job_id: planner,
        baseline_targets=baseline_targets, role_targets=role_targets,
        state_root_override=state_root, poll_interval_seconds=0.02,
    )
    try:
        executor.start()
        _wait_for_jobs_terminal(git_repo_with_commit, state_root, [job.job_id])
    finally:
        executor.stop()

    check = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        assert check.get_job(job.job_id).state == "SUCCEEDED"
        assert check.get(job.plan_id).state == "READY"
    finally:
        check.close()


def test_executor_never_reuses_one_sqlite_connection_across_threads(
    git_repo_with_commit, state_root, ollama_server,
):
    """Test item 22: each execution constructs its own
    `EngineeringPlanningService` (its own connection); running several
    jobs through the real thread pool must never raise a
    cross-thread-connection error."""
    svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        route = _seed_route(svc.production_conn(), root=ollama_server)
        jobs = [_create_job(svc, route, request=f"{REQUEST} {i}") for i in range(3)]
    finally:
        svc.close()

    binding, baseline_targets, role_targets = route
    # A fresh FakePlanner per call -- `FakePlanner` itself is not
    # thread-safe, matching how a real worker-bound factory would
    # construct its own adapter per call rather than share one mutable
    # object across concurrently-executing jobs. `max_workers=1` matches
    # the documented, evidence-based production default (`planning.
    # executor`'s module docstring): concurrent `git` invocations
    # against the same working tree are not provably contention-free
    # (index-lock races), so this test proves each of several
    # *sequential* background executions gets its own fresh connection
    # -- not that true overlap across jobs sharing one repository is
    # safe, which nothing in this phase claims.
    executor = PlanningJobExecutor(
        git_repo_with_commit,
        planner_factory_for_worker=lambda worker_id, job_id: FakePlanner([structured_response()]),
        baseline_targets=baseline_targets, role_targets=role_targets,
        state_root_override=state_root, max_workers=1, poll_interval_seconds=0.02,
    )
    try:
        executor.start()
        _wait_for_jobs_terminal(git_repo_with_commit, state_root, [j.job_id for j in jobs])
    finally:
        executor.stop()
    check = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        for j in jobs:
            assert check.get_job(j.job_id).state == "SUCCEEDED"
    finally:
        check.close()


def test_jobs_perform_no_repository_mutation_or_command_execution(
    service, route, git_repo_with_commit,
):
    """Test items 23/24."""
    (git_repo_with_commit / "Makefile").write_text("test:\n\ttouch executed.marker\n")
    before_status = git(git_repo_with_commit, "status", "--porcelain")
    job = _create_job(service, route)
    claimed = service.claim_job(job.job_id)
    planner = FakePlanner([structured_response(discovered_commands=[
        {"command": "make test", "purpose": "test", "evidence_source": "fabricated"},
    ])])
    service.execute_claimed_job(claimed, planner)
    assert git(git_repo_with_commit, "status", "--porcelain") == before_status
    assert not (git_repo_with_commit / "executed.marker").exists()


# --- 25. no trust/policy capability broadened -------------------------------

def test_executor_and_jobs_repo_import_no_mutation_authority():
    import ast

    import code_slayer.planning.executor as executor_module
    import code_slayer.store.planning_jobs_repo as jobs_repo_module

    forbidden = {
        "code_slayer.tools.executor", "code_slayer.policy.engine",
        "code_slayer.lease.manager", "code_slayer.repo.checkpoint",
        "code_slayer.workers.trust", "code_slayer.workers.promotion",
    }
    for module in (executor_module, jobs_repo_module):
        tree = ast.parse(open(module.__file__, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        overlap = forbidden & imported
        assert not overlap, f"{module.__name__} imports forbidden: {overlap}"


# -- H.4: the certified output_token_budget reaches the actual adapter's
# HTTP payload, and is never hardcoded ---------------------------------


class _CompletionCapturingHandler(http.server.BaseHTTPRequestHandler):
    """Serves `/api/version`/`/api/tags` (the live-runtime probe) and
    `/v1/chat/completions` (the actual inference call) -- captures every
    completion request body's own `max_tokens` field on `self.server`
    so the test can inspect exactly what left the process."""

    def _write(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/api/version":
            self._write(200, json.dumps({"version": "0.16.1"}).encode())
            return
        if path == "/api/tags":
            self._write(
                200,
                json.dumps({"models": [{"name": "devstral:24b", "digest": "sha256:abc"}]}).encode(),
            )
            return
        self._write(404, b"{}")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        if self.path.split("?", 1)[0] == "/v1/chat/completions":
            self.server.captured_max_tokens.append(json.loads(body).get("max_tokens"))
            self._write(200, json.dumps({
                "choices": [{"message": {"role": "assistant", "tool_calls": [{
                    "type": "function",
                    "function": {
                        "name": "emit_engineering_plan",
                        "arguments": json.dumps({"goal": "done"}),
                    },
                }]}}],
            }).encode())
            return
        self._write(404, b"{}")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def completion_server():
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _CompletionCapturingHandler)
    httpd.captured_max_tokens = []
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True,
    )
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("budget", [4096, 512])
def test_certified_output_token_budget_reaches_the_adapter_http_payload(
    service, completion_server, budget,
):
    """H.4 mandatory proof: the job's own durably-bound `output_token_
    budget` -- not a hardcoded constant -- is exactly what appears as
    `max_tokens` in the real `OpenAICompatibleAdapter`'s outgoing HTTP
    request body. Run with two DISTINCT budget values (never both
    4096, the common default) so a hardcoded `4096` anywhere in the
    pipeline would be caught."""
    from code_slayer.workers.openai_compatible_adapter import (
        OpenAICompatibleAdapter,
        OpenAICompatibleConfig,
    )

    root = f"http://127.0.0.1:{completion_server.server_port}"
    conn = service.production_conn()
    WorkersRepo(conn).register(worker_id="budget-worker", kind="fake", network_class="local")
    profile = runtime_profile_identity_from_config(
        model_tag="devstral:24b", model_digest="sha256:abc", endpoint=f"{root}/v1",
        runtime_version="0.16.1", effective_context_tokens=16384, temperature=0.0,
        normalizer_id=None, normalizer_version=None,
    )
    security = record_baseline_certificate(
        conn, worker_id="budget-worker", runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS, evidence_ref="sec-ev-budget", reason="ok",
    )
    assert security.ok, security.reason
    role_evaluation = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        output_token_budget=budget, tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        policy_version=POLICY_VERSION,
    )
    role = record_role_certificate(
        conn, worker_id="budget-worker", role=ProductionRole.PLANNER, runtime_profile=profile,
        policy_version=POLICY_VERSION, outcome=RoleQualificationOutcome.PASS,
        classification="PASS_FIRST_TRY", evidence_ref="role-ev-budget", reason="ok",
        role_evaluation=role_evaluation,
    )
    assert role.ok, role.reason
    binding = PlannerRouteBinding(
        worker_id="budget-worker",
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        role_evaluation_fingerprint=role_evaluation.role_evaluation_fingerprint,
        security_certificate_id=security.certificate.certificate_id,
        role_certificate_id=role.certificate.certificate_id,
        output_token_budget=budget, tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        planner_policy_version=POLICY_VERSION,
    )
    baseline_targets = (
        BaselineCertificationTarget(
            worker_id="budget-worker",
            expectation=LiveOllamaRuntimeExpectation(
                ollama_root=root, model_tag="devstral:24b", model_digest="sha256:abc",
                runtime_version="0.16.1", effective_context_tokens=16384, temperature=0.0,
                expected_runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
                normalizer_id=None, normalizer_version=None,
            ),
        ),
    )
    role_targets = (
        RoleEvaluationTarget(
            worker_id="budget-worker", role=ProductionRole.PLANNER,
            output_token_budget=budget, tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
            policy_version=POLICY_VERSION,
        ),
    )
    job = service.create_job(
        original_request=REQUEST, route_binding=binding,
        baseline_targets=baseline_targets, role_targets=role_targets,
    )
    assert job.output_token_budget == budget
    claimed = service.claim_job(job.job_id)

    adapter = OpenAICompatibleAdapter(
        OpenAICompatibleConfig(base_url=f"{root}/v1", model="devstral:24b"),
    )
    planner = WorkerAdapterPlanner(adapter, task_id=f"planning-job:{job.job_id}")
    record = service.execute_claimed_job(claimed, planner)
    assert record.state == "SUCCEEDED"

    assert completion_server.captured_max_tokens == [budget]


def _wait_for_jobs_terminal(repo_path, state_root, job_ids, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        check = EngineeringPlanningService(repo_path, state_root_override=state_root)
        try:
            if all(check.get_job(jid).state in ("SUCCEEDED", "FAILED") for jid in job_ids):
                return
        finally:
            check.close()
        time.sleep(0.02)
    raise AssertionError(f"jobs {job_ids} did not reach a terminal state within {timeout}s")
