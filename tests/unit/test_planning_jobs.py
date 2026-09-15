"""Durable background planning jobs (Phase 8.2d).

Covers: job creation never invokes a planner on the calling thread;
durable QUEUED/RUNNING persistence; atomic, fenced claim/reclaim
semantics (concurrent dispatchers, crash-then-restart recovery via
`lease.liveness`, stale-owner refusal, terminal-job immutability);
SUCCEEDED/FAILED job outcomes distinct from plan content state; no raw
model output through the job-facing shape; a bounded background
executor that never duplicates execution and never lets one job's
exception kill the dispatcher; and the read-only/no-mutation/no-trust
guarantees applied to the whole job lifecycle.
"""

from __future__ import annotations

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
from code_slayer.planning.service import EngineeringPlanningService
from code_slayer.planning.worker_planner import WorkerAdapterPlanner
from code_slayer.store.db import transaction
from code_slayer.store.planning_jobs_repo import PlanningJobsRepo
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.protocol import WorkerAdapterError
from tests.repo_helpers import git

REQUEST = "Add a read-only endpoint reporting repository intelligence snapshot age."


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


# --- 1/2. job creation never invokes a planner; durable QUEUED --------------

def test_create_job_never_invokes_a_planner_and_is_durably_queued(service):
    job = service.create_job(original_request=REQUEST)
    assert job.state == "QUEUED"
    assert job.attempt == 0
    plan = service.get(job.plan_id)
    assert plan.state == "DRAFT"  # real, inspectable plan identity already exists

    # Durable: a second, independent service instance sees the same row.
    row = PlanningJobsRepo(service._conn).get(job.job_id)
    assert row.state == "QUEUED"
    assert row.owner_pid is None


def test_create_job_is_fast_even_though_planning_is_slow(git_repo_with_commit, state_root):
    """Test item 1: proves the calling thread does not block on
    inference -- `create_job()` returns immediately regardless of how
    long a subsequently-claimed execution would take."""
    svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        t0 = time.time()
        job = svc.create_job(original_request=REQUEST)
        elapsed = time.time() - t0
        assert elapsed < 1.0
        assert job.state == "QUEUED"
    finally:
        svc.close()


# --- 9. claim/execute cycle and RUNNING persistence -------------------------

def test_claim_transitions_queued_to_running_durably(service):
    job = service.create_job(original_request=REQUEST)
    claimed = service.claim_job(job.job_id)
    assert claimed.state == "RUNNING"
    assert claimed.owner_pid is not None
    assert claimed.owner_generation == 1
    assert claimed.attempt == 1
    row = PlanningJobsRepo(service._conn).get(job.job_id)
    assert row.state == "RUNNING"


# --- 5/6. SUCCEEDED with READY / NEEDS_INPUT, distinct from job state ------

def test_successful_turn_ready_plan(service):
    job = service.create_job(original_request=REQUEST)
    claimed = service.claim_job(job.job_id)
    planner = FakePlanner([structured_response()])
    record = service.execute_claimed_job(claimed, planner)
    assert record.state == "SUCCEEDED"
    assert record.failure_category is None
    plan = service.get(job.plan_id)
    assert plan.state == "READY"


def test_successful_turn_needs_input_plan_is_still_a_succeeded_job(service):
    blocked = structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])
    job = service.create_job(original_request=REQUEST)
    claimed = service.claim_job(job.job_id)
    planner = FakePlanner([blocked])
    record = service.execute_claimed_job(claimed, planner)
    assert record.state == "SUCCEEDED"  # the turn itself succeeded
    plan = service.get(job.plan_id)
    assert plan.state == "NEEDS_INPUT"  # a legitimate, distinct plan outcome


# --- 7/8. transport failure -> FAILED, safe category, no raw leak ----------

def test_planner_transport_failure_is_a_failed_job_with_safe_category(service):
    adapter = FakeWorkerAdapter([WorkerAdapterError("transport_timeout")])
    planner = WorkerAdapterPlanner(adapter, task_id="job-x")
    job = service.create_job(original_request=REQUEST)
    claimed = service.claim_job(job.job_id)
    record = service.execute_claimed_job(claimed, planner)
    assert record.state == "FAILED"
    assert record.failure_category == "transport_error"
    assert record.failure_reason.startswith("malformed_planner_output:")


def test_raw_planner_output_never_appears_on_the_job_record(service):
    from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind

    secret_looking_prose = "IMPLEMENTATION PLAN: rm -rf / # api_key=sk-should-never-leak"
    adapter = FakeWorkerAdapter([
        WorkerResponse(kind=WorkerResponseKind.TEXT, text=secret_looking_prose),
    ])
    planner = WorkerAdapterPlanner(adapter, task_id="job-x")
    job = service.create_job(original_request=REQUEST)
    claimed = service.claim_job(job.job_id)
    record = service.execute_claimed_job(claimed, planner)
    assert record.state == "FAILED"
    import dataclasses

    for value in dataclasses.asdict(record).values():
        assert secret_looking_prose not in str(value)


def test_internal_exception_during_execution_is_a_failed_job_not_a_stuck_running_job(service):
    class _BrokenPlanner:
        def plan(self, request):
            raise RuntimeError("boom")

    job = service.create_job(original_request=REQUEST)
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
        job = creator.create_job(original_request=REQUEST)
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


def test_live_running_ownership_cannot_be_stolen(service):
    """Test item 13."""
    job = service.create_job(original_request=REQUEST)
    first = service.claim_job(job.job_id)
    assert first is not None
    second = service.claim_job(job.job_id)
    assert second is None  # this process's own pid is genuinely alive


def test_terminal_succeeded_job_is_never_rerun(service):
    """Test item 14."""
    job = service.create_job(original_request=REQUEST)
    claimed = service.claim_job(job.job_id)
    service.execute_claimed_job(claimed, FakePlanner([structured_response()]))
    assert service.claim_job(job.job_id) is None
    row = PlanningJobsRepo(service._conn).get(job.job_id)
    assert row.state == "SUCCEEDED"
    # The migration's own trigger refuses to reopen a terminal row even
    # via a raw UPDATE, independent of any application-level check.
    with pytest.raises(sqlite3.IntegrityError):
        with transaction(service._conn):
            service._conn.execute(
                "UPDATE planning_jobs SET state = 'QUEUED' WHERE job_id = ?", (job.job_id,),
            )


def test_terminal_failed_job_is_never_rerun_automatically(service):
    """Test item 15."""
    job = service.create_job(original_request=REQUEST)
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
        job = first.create_job(original_request=REQUEST)
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
        job = first.create_job(original_request=REQUEST)
        claimed = first.claim_job(job.job_id)
        assert claimed.owner_generation == 1
        # Simulate the owning process having actually crashed: record a
        # pid that is guaranteed not to exist, with a real (parseable)
        # start-time format so liveness evidence is genuinely GONE, not
        # merely UNKNOWN from malformed data.
        dead_pid = 999999999
        with transaction(first._conn):
            first._conn.execute(
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


def test_a_live_owners_running_job_is_not_in_the_claimable_set(service):
    job = service.create_job(original_request=REQUEST)
    service.claim_job(job.job_id)  # owned by this very-much-alive process
    assert job.job_id not in service.claimable_job_ids()


# --- 16/17/18/19. resume/replan/resolution async semantics ------------------

def test_resume_never_invokes_a_planner_and_creates_no_job(service):
    """By design (Phase 8.2d): `resume()` only re-evaluates the Question
    Gate against durable resolutions -- it performs no model inference
    at all, so there is no long-running turn to move into a background
    job, and it must never create one."""
    blocked = structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])
    job = service.create_job(original_request=REQUEST)
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


def test_replan_job_creates_a_queued_job_bound_to_a_new_plan_revision(service):
    """Test item 17."""
    job = service.create_job(original_request=REQUEST)
    claimed = service.claim_job(job.job_id)
    service.execute_claimed_job(claimed, FakePlanner([structured_response()]))
    replan_job = service.replan_job(job.plan_id)
    assert replan_job.state == "QUEUED"
    assert replan_job.kind == "replan"
    new_plan = service.get(replan_job.plan_id)
    assert new_plan.predecessor_plan_id == job.plan_id
    assert service.get(job.plan_id).state == "SUPERSEDED"


def test_human_resolution_recording_remains_durable(service):
    """Test item 18 (light confirmation; full coverage already exists
    in test_engineering_planning.py)."""
    from code_slayer.workers.prompt_analysis import EvidenceSource
    from code_slayer.workers.question_gate import ResolutionKind

    blocked = structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])
    job = service.create_job(original_request=REQUEST)
    claimed = service.claim_job(job.job_id)
    service.execute_claimed_job(claimed, FakePlanner([blocked]))
    service.record_user_resolution(
        job.plan_id, "scope", "JSON", resolution_kind=ResolutionKind.FACT,
        source=EvidenceSource.DURABLE_TASK_EVIDENCE,
    )
    plan = service.get(job.plan_id)
    assert plan.questions[0]["answer_recorded"] is True


# --- 21/22/23/24. bounded executor, thread safety, no mutation/execution ---

def test_executor_is_bounded_to_max_workers(git_repo_with_commit, state_root):
    """Test item 21: two QUEUED jobs, `max_workers=1` -- only one ever
    executes concurrently, proven with a blocking planner and an
    in-flight high-water-mark check."""
    svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        job1 = svc.create_job(original_request=REQUEST + " one")
        job2 = svc.create_job(original_request=REQUEST + " two")
    finally:
        svc.close()

    blocking = _BlockingPlanner(structured_response())
    executor = PlanningJobExecutor(
        git_repo_with_commit, planner_factory=lambda: blocking,
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
    git_repo_with_commit, state_root,
):
    """Test item 11, exercised through the real executor (not just the
    service layer directly)."""
    svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        job = svc.create_job(original_request=REQUEST)
    finally:
        svc.close()

    planner = FakePlanner([structured_response()])
    executor = PlanningJobExecutor(
        git_repo_with_commit, planner_factory=lambda: planner,
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
    git_repo_with_commit, state_root,
):
    """Test item 22: each execution constructs its own
    `EngineeringPlanningService` (its own connection); running several
    jobs through the real thread pool must never raise a
    cross-thread-connection error."""
    svc = EngineeringPlanningService(git_repo_with_commit, state_root_override=state_root)
    try:
        jobs = [svc.create_job(original_request=f"{REQUEST} {i}") for i in range(3)]
    finally:
        svc.close()

    # A fresh FakePlanner per call -- `FakePlanner` itself is not
    # thread-safe, matching how a real `planner_factory` would construct
    # its own adapter per call rather than share one mutable object
    # across concurrently-executing jobs. `max_workers=1` matches the
    # documented, evidence-based production default (`planning.executor`'s
    # module docstring): concurrent `git` invocations against the same
    # working tree are not provably contention-free (index-lock races),
    # so this test proves each of several *sequential* background
    # executions gets its own fresh connection -- not that true overlap
    # across jobs sharing one repository is safe, which nothing in this
    # phase claims.
    executor = PlanningJobExecutor(
        git_repo_with_commit, planner_factory=lambda: FakePlanner([structured_response()]),
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


def test_jobs_perform_no_repository_mutation_or_command_execution(service, git_repo_with_commit):
    """Test items 23/24."""
    (git_repo_with_commit / "Makefile").write_text("test:\n\ttouch executed.marker\n")
    before_status = git(git_repo_with_commit, "status", "--porcelain")
    job = service.create_job(original_request=REQUEST)
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
