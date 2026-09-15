"""Engineering planning over HTTP (Phase 8.2/8.2d) — thin endpoints, no
persistence/planning policy of their own; strict field allowlists;
explicit unconfigured state; read-only/no-mutation guarantees end to
end; durable background job execution decoupled from HTTP request
lifetime."""

from __future__ import annotations

import time

import pytest

from code_slayer.api import create_app
from code_slayer.api.service import RuntimeBindings
from code_slayer.planning.fake_planner import FakePlanner
from code_slayer.planning.planner import PlannerOutcome, PlannerResponse, parse_planner_output
from code_slayer.runner import LocalWorkerRunner
from tests.repo_helpers import git

REQUEST = "Add a read-only endpoint reporting snapshot age."
_FAST_POLL = 0.02
_created_apps: list = []


@pytest.fixture(autouse=True)
def _stop_planning_executors():
    """Every `application()` call in this file starts a real background
    planning-job dispatcher thread (Phase 8.2d); without this, dozens of
    them would leak, live, for the rest of the test session."""
    yield
    while _created_apps:
        _created_apps.pop().extensions["codeslayer"].close()


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


def application(repo, *, planner_responses=None, poll_interval=_FAST_POLL):
    planner = FakePlanner(planner_responses if planner_responses is not None else [])
    bindings = RuntimeBindings(
        planner_factory=lambda: planner, planning_poll_interval_seconds=poll_interval,
    )
    app = create_app(repo, bindings=bindings)
    _created_apps.append(app)
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


def test_create_returns_202_immediately_then_reaches_ready_over_http(git_repo_with_commit):
    client, _ = application(git_repo_with_commit, planner_responses=[structured_response()])
    created = client.post("/api/plans", json={"request": REQUEST})
    assert created.status_code == 202
    job = created.json
    assert job["state"] == "QUEUED"
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


def test_needs_input_result_is_a_successful_job(git_repo_with_commit):
    """job = SUCCEEDED, plan = NEEDS_INPUT is a valid, distinct outcome."""
    blocked = structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])
    responses = [blocked, structured_response()]
    client, _ = application(git_repo_with_commit, planner_responses=responses)
    created = client.post("/api/plans", json={"request": REQUEST})
    job = created.json
    finished = wait_for_job(client, job["job_id"])
    assert finished["state"] == "SUCCEEDED"  # the turn itself succeeded
    plan_id = job["plan_id"]
    assert client.get(f"/api/plans/{plan_id}").json["state"] == "NEEDS_INPUT"


def test_blocked_plan_resolution_and_async_replan_over_http(git_repo_with_commit):
    blocked = structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])
    responses = [blocked, structured_response()]
    client, _ = application(git_repo_with_commit, planner_responses=responses)
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
])
def test_api_rejects_forbidden_authority_fields(git_repo_with_commit, field):
    client, _ = application(git_repo_with_commit, planner_responses=[structured_response()] * 2)
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


def test_malformed_planner_output_surfaces_as_failed_job_and_draft_plan(git_repo_with_commit):
    client, _ = application(
        git_repo_with_commit, planner_responses=[PlannerResponse(PlannerOutcome.MALFORMED)],
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


def test_planning_over_http_never_mutates_repository_or_executes(git_repo_with_commit):
    runner = LocalWorkerRunner(git_repo_with_commit)
    before_head = git(git_repo_with_commit, "rev-parse", "HEAD")
    before_status = git(git_repo_with_commit, "status", "--porcelain")
    client, _ = application(git_repo_with_commit, planner_responses=[structured_response(
        discovered_commands=[
            {"command": "rm -rf /", "purpose": "test", "evidence_source": "fabricated"},
        ],
    )])
    created = client.post("/api/plans", json={"request": REQUEST})
    wait_for_job(client, created.json["job_id"])
    assert git(git_repo_with_commit, "rev-parse", "HEAD") == before_head
    assert git(git_repo_with_commit, "status", "--porcelain") == before_status
    runner.close()
