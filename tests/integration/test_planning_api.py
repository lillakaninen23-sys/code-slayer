"""Engineering planning over HTTP (Phase 8.2) — thin endpoints, no
persistence/planning policy of their own; strict field allowlists;
explicit unconfigured state; read-only/no-mutation guarantees end to
end."""

from __future__ import annotations

import pytest

from code_slayer.api import create_app
from code_slayer.api.service import RuntimeBindings
from code_slayer.planning.fake_planner import FakePlanner
from code_slayer.planning.planner import PlannerOutcome, PlannerResponse, parse_planner_output
from code_slayer.runner import LocalWorkerRunner
from tests.repo_helpers import git

REQUEST = "Add a read-only endpoint reporting snapshot age."


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


def application(repo, *, planner_responses=None):
    planner = FakePlanner(planner_responses if planner_responses is not None else [])
    bindings = RuntimeBindings(planner_factory=lambda: planner)
    app = create_app(repo, bindings=bindings)
    return app.test_client(), planner


def test_disconnected_unconfigured_planner_state_is_explicit(git_repo_with_commit):
    client = create_app(git_repo_with_commit, bindings=RuntimeBindings()).test_client()
    health = client.get("/api/health").json
    assert health["actions"]["planning_configured"] is False
    response = client.post("/api/plans", json={"request": REQUEST})
    assert response.status_code == 503
    assert response.json["error"]["code"] == "planner_not_configured"


def test_create_get_list_and_resume_over_http(git_repo_with_commit):
    client, _ = application(git_repo_with_commit, planner_responses=[structured_response()])
    created = client.post("/api/plans", json={"request": REQUEST})
    assert created.status_code == 201
    plan_id = created.json["plan_id"]
    assert created.headers["Location"] == f"/api/plans/{plan_id}"
    assert created.json["state"] == "READY"
    assert created.json["content"]["goal"] == "Add the endpoint"

    detail = client.get(f"/api/plans/{plan_id}")
    assert detail.status_code == 200 and detail.json["plan_id"] == plan_id

    listed = client.get("/api/plans")
    assert listed.status_code == 200
    assert any(p["plan_id"] == plan_id for p in listed.json["plans"])

    resumed = client.post(f"/api/plans/{plan_id}/resume", json={})
    assert resumed.status_code == 200 and resumed.json["state"] == "READY"

    assert client.get("/api/plans/does-not-exist").status_code == 404


def test_blocked_plan_resolution_and_replan_over_http(git_repo_with_commit):
    blocked = structured_response(ambiguities=[{
        "id": "scope", "question": "Which format?", "rationale": "unclear",
        "risk_class": "MATERIAL",
    }])
    responses = [blocked, structured_response()]
    client, _ = application(git_repo_with_commit, planner_responses=responses)
    created = client.post("/api/plans", json={"request": REQUEST})
    assert created.json["state"] == "NEEDS_INPUT"
    plan_id = created.json["plan_id"]

    resolved = client.post(f"/api/plans/{plan_id}/resolutions", json={
        "ambiguity_id": "scope", "answer": "JSON", "resolution_kind": "FACT",
    })
    # Recording an answer alone never advances state (mirrors the runs
    # resolutions endpoint) -- an explicit resume() call re-evaluates
    # the Question Gate.
    assert resolved.status_code == 200 and resolved.json["state"] == "NEEDS_INPUT"
    resumed = client.post(f"/api/plans/{plan_id}/resume", json={})
    assert resumed.status_code == 200 and resumed.json["state"] == "READY"

    replanned = client.post(f"/api/plans/{plan_id}/replan", json={})
    assert replanned.status_code == 200
    assert replanned.json["predecessor_plan_id"] == plan_id
    assert client.get(f"/api/plans/{plan_id}").json["state"] == "SUPERSEDED"


@pytest.mark.parametrize("field", [
    "repo_path", "db_path", "worktree_path", "trust_level", "lease_generation",
    "fencing_token", "worker_session_id", "checkpoint_id", "worker_authority",
    "cloud_escalation", "allow_cloud", "command", "execute",
])
def test_api_rejects_forbidden_authority_fields(git_repo_with_commit, field):
    client, _ = application(git_repo_with_commit, planner_responses=[structured_response()])
    assert client.post(
        "/api/plans", json={"request": REQUEST, field: "forged"},
    ).status_code == 400
    created = client.post("/api/plans", json={"request": REQUEST})
    plan_id = created.json["plan_id"]
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


def test_malformed_planner_output_surfaces_as_draft_over_http(git_repo_with_commit):
    client, _ = application(
        git_repo_with_commit, planner_responses=[PlannerResponse(PlannerOutcome.MALFORMED)],
    )
    created = client.post("/api/plans", json={"request": REQUEST})
    assert created.status_code == 201
    assert created.json["state"] == "DRAFT"
    assert created.json["reason"] == "malformed_planner_output"


def test_planning_over_http_never_mutates_repository_or_executes(git_repo_with_commit):
    runner = LocalWorkerRunner(git_repo_with_commit)
    before_head = git(git_repo_with_commit, "rev-parse", "HEAD")
    before_status = git(git_repo_with_commit, "status", "--porcelain")
    client, _ = application(git_repo_with_commit, planner_responses=[structured_response(
        discovered_commands=[
            {"command": "rm -rf /", "purpose": "test", "evidence_source": "fabricated"},
        ],
    )])
    client.post("/api/plans", json={"request": REQUEST})
    assert git(git_repo_with_commit, "rev-parse", "HEAD") == before_head
    assert git(git_repo_with_commit, "status", "--porcelain") == before_status
    runner.close()
