"""H.4: real, coordinated two-connection/thread concurrency for durable
Planner route creation vs. worker archive.

Each racer opens its OWN connection to the SAME on-disk database in the
thread that uses it (Python's `sqlite3` connections are not safe to
share across threads) -- the same discipline `tests/unit/
test_worker_lifecycle_service.py`'s own H.3 concurrency tests already
established. Assertions branch on WHICH side actually won (never a
hardcoded order), so these tests are correct regardless of real OS
thread-scheduling nondeterminism; `threading.Event`/`threading.Barrier`
are used to force or maximize a specific interleaving where the
invariant under test needs one.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

import code_slayer.planning.routing as routing_module
from code_slayer.planning.executor import PlanningJobExecutor
from code_slayer.planning.fake_planner import FakePlanner
from code_slayer.planning.models import JobFailureCategory
from code_slayer.planning.planner import PlannerOutcome, PlannerResponse, parse_planner_output
from code_slayer.planning.routing import RoutingOutcome, select_planner_route
from code_slayer.planning.service import (
    EngineeringPlanningService,
    PlannerRouteBindingRejectedError,
)
from code_slayer.security.certification_service import (
    BaselineCertificationTarget,
    RoleEvaluationTarget,
)
from code_slayer.security.live_certification import LiveOllamaRuntimeExpectation
from code_slayer.store import db as db_module
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

REQUEST = "Add a read-only endpoint reporting repository intelligence snapshot age."
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


def _profile(worker_id: str, *, root: str = OLLAMA_ROOT):
    return runtime_profile_identity_from_config(
        model_tag=f"{worker_id}-model", model_digest="sha256:abc", endpoint=f"{root}/v1",
        runtime_version="0.16.1", effective_context_tokens=16384, temperature=0.0,
        normalizer_id=None, normalizer_version=None,
    )


def _target(worker_id: str, *, root: str = OLLAMA_ROOT) -> BaselineCertificationTarget:
    profile = _profile(worker_id, root=root)
    return BaselineCertificationTarget(
        worker_id=worker_id,
        expectation=LiveOllamaRuntimeExpectation(
            ollama_root=root, model_tag=f"{worker_id}-model", model_digest="sha256:abc",
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


def _certify_eligible(conn, worker_id: str, *, root: str = OLLAMA_ROOT):
    from code_slayer.store.workers_repo import WorkersRepo

    WorkersRepo(conn).register(worker_id=worker_id, kind="fake", network_class="local")
    profile = _profile(worker_id, root=root)
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


@pytest.fixture
def state_root(tmp_path):
    root = tmp_path / "_codeslayer_state"
    root.mkdir()
    return root


class _OllamaScript:
    def __init__(self) -> None:
        self.version = "0.16.1"
        self.models = [{"name": "worker-a-model", "digest": "sha256:abc"}]


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


# -- create vs archive: both winner orders -----------------------------------


def test_create_job_vs_archive_worker_concurrency_both_winner_orders(
    git_repo_with_commit, state_root,
):
    """H.4 review §10: inside the SAME production `BEGIN IMMEDIATE`
    transaction that creates the new plan/job, `create_job()` reloads
    and re-verifies the route binding's worker. Races that against a
    genuinely concurrent `archive_worker()` call: if archive wins,
    NOTHING is created; if create wins, archive subsequently sees the
    new QUEUED job as active work and refuses."""
    setup = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        _certify_eligible(setup.production_conn(), "w1")
        selection = select_planner_route(
            setup.production_conn(), baseline_targets=(_target("w1"),),
            role_targets=(_role_target("w1"),),
        )
        assert selection.outcome == RoutingOutcome.SELECTED
        binding = selection.binding
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def _create():
        svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
        try:
            barrier.wait(timeout=5)
            try:
                results["job"] = svc.create_job(
                    original_request=REQUEST, route_binding=binding,
                    baseline_targets=(_target("w1"),), role_targets=(_role_target("w1"),),
                )
            except PlannerRouteBindingRejectedError as exc:
                results["create_error"] = exc
        finally:
            svc.close()

    def _archive():
        conn = db_module.connect(setup._db_path)
        try:
            barrier.wait(timeout=5)
            results["archive"] = archive_worker(conn, worker_id="w1")
        finally:
            conn.close()

    threads = [threading.Thread(target=_create), threading.Thread(target=_archive)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads)

    archive_result = results["archive"]
    verify = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        jobs = verify.list_jobs()
        if "job" in results:
            # create() won first: a real QUEUED job exists, bound to w1,
            # and archive (racing against it) saw genuinely active work.
            assert len(jobs) == 1
            assert jobs[0].worker_id == "w1"
            assert jobs[0].state == "QUEUED"
            assert archive_result.ok is False
            assert archive_result.reason == "worker_has_active_work"
        else:
            # archive() won first: create's own atomic recheck saw
            # ARCHIVED and created NOTHING -- no plan, no job.
            assert "create_error" in results
            assert results["create_error"].reason == "planner_worker_archived"
            assert jobs == []
            assert verify.list(limit=50) == []
            assert archive_result.ok and archive_result.changed
    finally:
        verify.close()


# -- certificate authority changes between selection and the create
# transaction (H.4 review correction #2) -------------------------------------


def test_certificate_authority_change_between_selection_and_create_is_refused(
    git_repo_with_commit, state_root,
):
    """A deterministic DB race, forced via events rather than a free-
    running barrier (the exact interleaving under test -- a NEW
    certificate landing strictly between route SELECTION and the
    CREATE transaction's own atomic recheck -- must be guaranteed, not
    merely likely): route selection happens, then (before the create
    transaction ever opens) a genuinely concurrent re-certification
    commits a NEW Baseline Security certificate for the same worker,
    on a separate connection/thread. The stale binding must be refused
    -- `planner_route_binding_stale` -- and create NOTHING; a job must
    never be accepted already-stale merely because execution-time
    revalidation would eventually have caught it too."""
    setup = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        _certify_eligible(setup.production_conn(), "w1")
        selection = select_planner_route(
            setup.production_conn(), baseline_targets=(_target("w1"),),
            role_targets=(_role_target("w1"),),
        )
        assert selection.outcome == RoutingOutcome.SELECTED
        binding = selection.binding
    finally:
        setup.close()

    recertified = threading.Event()

    def _recertify():
        conn = db_module.connect(setup._db_path)
        try:
            profile = _profile("w1")
            result = record_baseline_certificate(
                conn, worker_id="w1", runtime_profile=profile,
                outcome=SecurityBaselineOutcome.PASS, evidence_ref="sec-ev-w1-NEW",
                reason="re-certified",
            )
            assert result.ok, result.reason
            assert result.certificate.certificate_id != binding.security_certificate_id
        finally:
            conn.close()
        recertified.set()

    thread = threading.Thread(target=_recertify)
    thread.start()
    thread.join(timeout=5)
    assert recertified.is_set()

    svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        with pytest.raises(PlannerRouteBindingRejectedError) as excinfo:
            svc.create_job(
                original_request=REQUEST, route_binding=binding,
                baseline_targets=(_target("w1"),), role_targets=(_role_target("w1"),),
            )
        assert excinfo.value.reason == "planner_route_binding_stale"
        assert svc.list_jobs() == []
        assert svc.list(limit=50) == []
    finally:
        svc.close()


# -- no-reroute: an ineligible bound worker never falls back to another ------


def test_bound_worker_ineligible_never_reroutes_to_a_different_eligible_worker(
    git_repo_with_commit, state_root,
):
    """H.4's central invariant: `job` is bound to worker-A at creation.
    worker-A later becomes ineligible (its Baseline Security certificate
    is superseded by a FAIL re-certification -- a realistic scenario
    that, unlike archiving, is never blocked by the job itself being
    QUEUED). worker-B becomes eligible in the meantime. The claimed job
    must FAIL -- never silently execute on worker-B. worker-B's own
    factory must never be called at all."""
    setup = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        _certify_eligible(setup.production_conn(), "worker-a")
        selection = select_planner_route(
            setup.production_conn(), baseline_targets=(_target("worker-a"),),
            role_targets=(_role_target("worker-a"),),
        )
        assert selection.outcome == RoutingOutcome.SELECTED
        binding_a = selection.binding
        job = setup.create_job(
            original_request=REQUEST, route_binding=binding_a,
            baseline_targets=(_target("worker-a"),), role_targets=(_role_target("worker-a"),),
        )
        assert job.worker_id == "worker-a"

        # worker-A becomes ineligible; worker-B becomes eligible.
        fail_cert = record_baseline_certificate(
            setup.production_conn(), worker_id="worker-a", runtime_profile=_profile("worker-a"),
            outcome=SecurityBaselineOutcome.FAIL, evidence_ref="sec-ev-worker-a-FAIL",
            reason="regressed",
        )
        assert fail_cert.ok, fail_cert.reason
        _certify_eligible(setup.production_conn(), "worker-b")
    finally:
        setup.close()

    worker_b_calls: list[tuple[str, str]] = []

    def _planner_factory_for_worker(worker_id: str, job_id: str):
        worker_b_calls.append((worker_id, job_id))
        return FakePlanner([structured_response()])

    executor = PlanningJobExecutor(
        git_repo_with_commit, planner_factory_for_worker=_planner_factory_for_worker,
        baseline_targets=(_target("worker-a"), _target("worker-b")),
        role_targets=(_role_target("worker-a"), _role_target("worker-b")),
        state_root_override=state_root, poll_interval_seconds=0.02,
    )
    try:
        executor.start()
        deadline_service = EngineeringPlanningService(
            git_repo_with_commit, state_root_override=state_root,
        )
        import time
        deadline = time.time() + 10
        try:
            while time.time() < deadline:
                if deadline_service.get_job(job.job_id).state in ("SUCCEEDED", "FAILED"):
                    break
                time.sleep(0.02)
            final = deadline_service.get_job(job.job_id)
        finally:
            deadline_service.close()
    finally:
        executor.stop()

    assert final.state == "FAILED"
    assert final.failure_category == JobFailureCategory.ROUTING.value
    assert final.failure_reason == "planner_worker_not_eligible:security_baseline_fail"
    # The factory was never called for ANY worker -- neither the
    # archived worker-a (revalidation refused before construction) nor
    # a substituted worker-b (this job was never re-routed to it).
    assert worker_b_calls == []


# -- certificate authority changes WHILE the live probe is in flight
# (H.4 review finding: the final authority check must occur immediately
# before Planner construction, not merely before the live probe) --------


def test_certificate_change_during_live_probe_is_caught_before_construction(
    git_repo_with_commit, state_root, ollama_server, monkeypatch,
):
    """Forces, via events (never a free-running race), the exact
    interleaving under test: the executor's FIRST DB-only revalidation
    pass already succeeded, its live probe is in flight, and a
    genuinely concurrent connection commits a NEW Baseline Security
    certificate for the same worker WHILE that probe is still running.
    The probe itself succeeds (the runtime is genuinely reachable) --
    but the SECOND, post-probe, DB-only revalidation pass
    (`PlanningJobExecutor._revalidate_before_construction()`) must
    still catch the now-stale authority and refuse before a Planner is
    ever constructed or a model is ever called."""
    svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        _certify_eligible(svc.production_conn(), "worker-a", root=ollama_server)
        selection = select_planner_route(
            svc.production_conn(), baseline_targets=(_target("worker-a", root=ollama_server),),
            role_targets=(_role_target("worker-a"),),
        )
        assert selection.outcome == RoutingOutcome.SELECTED
        binding = selection.binding
        job = svc.create_job(
            original_request=REQUEST, route_binding=binding,
            baseline_targets=(_target("worker-a", root=ollama_server),),
            role_targets=(_role_target("worker-a"),),
        )
    finally:
        svc.close()

    probe_started = threading.Event()
    cert_changed = threading.Event()
    real_verify = routing_module.verify_ollama_runtime

    def _synchronized_verify(expected):
        probe_started.set()
        assert cert_changed.wait(timeout=5)
        return real_verify(expected)

    monkeypatch.setattr(routing_module, "verify_ollama_runtime", _synchronized_verify)

    def _change_certificate():
        assert probe_started.wait(timeout=5)
        changer = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
        try:
            replacement = record_baseline_certificate(
                changer.production_conn(), worker_id="worker-a",
                runtime_profile=_profile("worker-a", root=ollama_server),
                outcome=SecurityBaselineOutcome.PASS, evidence_ref="sec-ev-worker-a-NEW",
                reason="re-certified-during-probe",
            )
            assert replacement.ok, replacement.reason
            assert replacement.certificate.certificate_id != binding.security_certificate_id
        finally:
            changer.close()
        cert_changed.set()

    changer_thread = threading.Thread(target=_change_certificate)

    factory_calls: list[tuple[str, str]] = []

    def _planner_factory_for_worker(worker_id: str, job_id: str):
        factory_calls.append((worker_id, job_id))
        return FakePlanner([structured_response()])

    executor = PlanningJobExecutor(
        git_repo_with_commit, planner_factory_for_worker=_planner_factory_for_worker,
        baseline_targets=(_target("worker-a", root=ollama_server),),
        role_targets=(_role_target("worker-a"),),
        state_root_override=state_root, poll_interval_seconds=0.02,
    )
    changer_thread.start()
    try:
        executor.start()
        _wait_for_terminal(git_repo_with_commit, state_root, job.job_id)
    finally:
        executor.stop()
        changer_thread.join(timeout=5)
    assert not changer_thread.is_alive()

    check = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        final = check.get_job(job.job_id)
    finally:
        check.close()

    assert final.state == "FAILED"
    assert final.failure_category == JobFailureCategory.ROUTING.value
    assert final.failure_reason == "planner_route_binding_stale"
    # The gap was closed before the Planner was ever constructed or
    # called -- zero factory calls, zero model calls.
    assert factory_calls == []


def _wait_for_terminal(repo_path, state_root, job_id, timeout=10.0):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        check = EngineeringPlanningService(repo_path, state_root_override=state_root)
        try:
            if check.get_job(job_id).state in ("SUCCEEDED", "FAILED"):
                return
        finally:
            check.close()
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach a terminal state within {timeout}s")
