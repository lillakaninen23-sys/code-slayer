"""H.2: Certification Center v1 -- durable live Planner role
certification over the HTTP API.

Complements `test_live_planner_certification.py` (module-level gating)
and the existing `test_certification_center.py` (Baseline Security
routes, unaffected by this file). This file drives the new
`/planner/preflight` and `/planner/runs` routes end to end, including
the durable dispatcher actually executing a queued run.
"""

from __future__ import annotations

import http.server
import json
import threading
import time

import pytest

from code_slayer.api import create_app
from code_slayer.api.service import RuntimeBindings, WorkerRegistration
from code_slayer.repo import identity as repo_identity
from code_slayer.security.certification_service import (
    BaselineCertificationTarget,
    RoleEvaluationTarget,
)
from code_slayer.security.live_certification import LiveOllamaRuntimeExpectation
from code_slayer.store.db import connect, migrate
from code_slayer.store.location import db_path
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.workers.role_qualification import ProductionRole
from code_slayer.workers.security_baseline import (
    SecurityBaselineOutcome,
    record_baseline_certificate,
    runtime_profile_identity_from_config,
)

WORKER = "local-ollama-qwen3-coder-30b"


class _Script:
    def __init__(self) -> None:
        self.version = "0.16.1"
        self.models = [{"name": "qwen3-coder:30b", "digest": "sha256:abc"}]
        self.completion_posts = 0
        self.completion_status = 200
        self.completions: list[bytes] | None = None
        self.default_completion = _plan_body()


def _plan_body(goal: str = "Add the requested read-only endpoint") -> bytes:
    return json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "type": "function",
                                "function": {
                                    "name": "emit_engineering_plan",
                                    "arguments": json.dumps({"goal": goal}),
                                },
                            },
                        ],
                    },
                },
            ],
        },
    ).encode()


def _handler(script: _Script):
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

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            if script.completions is not None:
                index = min(script.completion_posts, max(len(script.completions) - 1, 0))
                body = script.completions[index]
            else:
                body = script.default_completion
            script.completion_posts += 1
            self._write(script.completion_status, body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return Handler


@pytest.fixture
def runtime_server():
    script = _Script()
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _handler(script))
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True,
    )
    thread.start()
    try:
        yield script, f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _target(root: str) -> BaselineCertificationTarget:
    identity = runtime_profile_identity_from_config(
        model_tag="qwen3-coder:30b",
        model_digest="sha256:abc",
        endpoint=f"{root}/v1",
        runtime_version="0.16.1",
        effective_context_tokens=16384,
        temperature=0.0,
        normalizer_id=None,
        normalizer_version=None,
    )
    return BaselineCertificationTarget(
        worker_id=WORKER,
        expectation=LiveOllamaRuntimeExpectation(
            ollama_root=root,
            model_tag="qwen3-coder:30b",
            model_digest="sha256:abc",
            runtime_version="0.16.1",
            effective_context_tokens=16384,
            temperature=0.0,
            expected_runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
            normalizer_id=None,
            normalizer_version=None,
            timeout=5.0,
        ),
    )


def _role_target() -> RoleEvaluationTarget:
    return RoleEvaluationTarget(
        worker_id=WORKER,
        role=ProductionRole.PLANNER,
        output_token_budget=1024,
        tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
        policy_version="planner-certification-v1",
    )


@pytest.fixture
def app_client(git_repo_with_commit, runtime_server):
    _script, root = runtime_server
    target = _target(root)
    bindings = RuntimeBindings(
        worker_registrations=(
            WorkerRegistration(worker_id=WORKER, kind="openai_compatible", network_class="local"),
        ),
        baseline_certification_targets=(target,),
        role_evaluation_targets=(_role_target(),),
        certification_poll_interval_seconds=0.05,
    )
    application = create_app(git_repo_with_commit, bindings=bindings)
    client = application.test_client()
    identity = runtime_profile_identity_from_config(
        model_tag=target.expectation.model_tag,
        model_digest=target.expectation.model_digest,
        endpoint=target.expectation.openai_base_url,
        runtime_version=target.expectation.runtime_version,
        effective_context_tokens=target.expectation.effective_context_tokens,
        temperature=target.expectation.temperature,
        normalizer_id=target.expectation.normalizer_id,
        normalizer_version=target.expectation.normalizer_version,
    )
    resolved = repo_identity.resolve(git_repo_with_commit, create=False)
    production = connect(db_path(resolved.repo_id, resolved.worktree_id))
    migrate(production)
    yield client, application, git_repo_with_commit, identity, production
    application.extensions["codeslayer"].close()
    production.close()


def _seed_production_baseline_pass(production_conn, identity):
    result = record_baseline_certificate(
        production_conn,
        worker_id=WORKER,
        runtime_profile=identity,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="baseline-ev",
        reason="ok",
    )
    assert result.ok, result.reason
    return result.certificate


def _wait_for_run(client, run_id: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    data = None
    while time.time() < deadline:
        data = client.get(f"/api/certification/runs/{run_id}").get_json()
        if data["state"] in {"PASS", "FAIL", "HARD_DISQUALIFIED", "INCOMPLETE"}:
            return data
        time.sleep(0.05)
    assert data is not None
    return data


def _start_after_preflight(client) -> dict:
    preflight = client.post(
        f"/api/certification/workers/{WORKER}/planner/preflight", json={},
    ).get_json()
    assert preflight["ready"] is True, preflight
    start = client.post(f"/api/certification/workers/{WORKER}/planner/runs", json={})
    assert start.status_code == 202
    return start.get_json()


# -- end-to-end PASS ----------------------------------------------------


def test_planner_certification_end_to_end_records_production_role_certificate(app_client):
    client, _app, _repo, identity, production = app_client
    _seed_production_baseline_pass(production, identity)

    worker = client.get(f"/api/certification/workers/{WORKER}").get_json()
    assert worker["planner_ready_for_certification"] is False
    planner_action = next(a for a in worker["future_actions"] if a["role"] == "PLANNER")
    assert planner_action["available"] is False

    preflight = client.post(
        f"/api/certification/workers/{WORKER}/planner/preflight", json={},
    ).get_json()
    assert preflight["ready"] is True, preflight
    assert preflight["kind"] == "planner_role"

    worker = client.get(f"/api/certification/workers/{WORKER}").get_json()
    assert worker["planner_ready_for_certification"] is True
    planner_action = next(a for a in worker["future_actions"] if a["role"] == "PLANNER")
    assert planner_action["available"] is True

    start = client.post(f"/api/certification/workers/{WORKER}/planner/runs", json={})
    assert start.status_code == 202
    run_id = start.get_json()["run_id"]

    data = _wait_for_run(client, run_id)
    assert data["state"] == "PASS"
    assert data["has_certificate"] is True
    assert data["kind"] == "planner_role"

    evidence = client.get(f"/api/certification/runs/{run_id}/evidence").get_json()
    assert evidence["environment"] == "PRODUCTION"
    assert evidence["document"]["final_classification"] == "PASS_FIRST_TRY"

    detail = client.get(f"/api/certification/workers/{WORKER}").get_json()
    assert detail["roles"]["PLANNER"]["status"] == "CERTIFIED"
    assert detail["production_eligibility"]["eligible"] is True
    assert detail["production_eligibility"]["source"] == "evaluate_production_eligibility"

    rows = RoleCertificatesRepo(production).list_for_worker_role(WORKER, "PLANNER")
    assert len(rows) == 1
    assert rows[0].outcome == "PASS"
    assert production.execute("SELECT count(*) AS c FROM worker_trust_events").fetchone()["c"] == 0
    assert production.execute("SELECT count(*) AS c FROM permission_grants").fetchone()["c"] == 0


# -- fail closed ----------------------------------------------------------


def test_planner_preflight_blocked_without_production_baseline(app_client):
    client, _app, _repo, _identity, _production = app_client
    preflight = client.post(
        f"/api/certification/workers/{WORKER}/planner/preflight", json={},
    ).get_json()
    assert preflight["ready"] is False
    named = {c["name"]: c for c in preflight["checks"]}
    assert named["production_baseline_security"]["ok"] is False

    start = client.post(f"/api/certification/workers/{WORKER}/planner/runs", json={})
    assert start.status_code == 409
    assert start.get_json()["error"]["code"] == "preflight_required"


def test_planner_certification_blocked_without_role_evaluation_target(
    git_repo_with_commit, runtime_server,
):
    _script, root = runtime_server
    target = _target(root)
    bindings = RuntimeBindings(
        worker_registrations=(
            WorkerRegistration(worker_id=WORKER, kind="openai_compatible", network_class="local"),
        ),
        baseline_certification_targets=(target,),
        role_evaluation_targets=(),  # deliberately missing
        certification_poll_interval_seconds=0.05,
    )
    application = create_app(git_repo_with_commit, bindings=bindings)
    client = application.test_client()
    try:
        preflight = client.post(
            f"/api/certification/workers/{WORKER}/planner/preflight", json={},
        ).get_json()
        assert preflight["ready"] is False
        named = {c["name"]: c for c in preflight["checks"]}
        assert named["role_target_configured"]["ok"] is False
    finally:
        application.extensions["codeslayer"].close()


def test_repeated_start_cannot_mint_duplicate_planner_authority(app_client):
    client, _app, _repo, identity, production = app_client
    _seed_production_baseline_pass(production, identity)
    started = _start_after_preflight(client)
    duplicate = client.post(f"/api/certification/workers/{WORKER}/planner/runs", json={})
    assert duplicate.status_code == 409
    assert duplicate.get_json()["error"]["code"] == "certification_already_in_progress"
    _wait_for_run(client, started["run_id"])


def test_planner_routes_reject_client_supplied_authority_fields(app_client):
    client, _app, _repo, _identity, _production = app_client
    for body in (
        {"outcome": "PASS"},
        {"score": 1.0},
        {"pass": True},
        {"digest": "sha256:x"},
        {"runtime_fingerprint": "x"},
        {"evaluation_fingerprint": "x"},
        {"evidence_ref": "abc"},
        {"certificate_id": "abc"},
        {"policy_version": "x"},
        {"test_results": []},
    ):
        preflight = client.post(
            f"/api/certification/workers/{WORKER}/planner/preflight", json=body,
        )
        assert preflight.status_code == 400
        assert preflight.get_json()["error"]["code"] == "invalid_fields"
        start = client.post(f"/api/certification/workers/{WORKER}/planner/runs", json=body)
        assert start.status_code == 400
        assert start.get_json()["error"]["code"] == "invalid_fields"


def test_planner_qualification_fail_never_flips_eligibility(git_repo_with_commit, runtime_server):
    script, root = runtime_server
    target = _target(root)
    bindings = RuntimeBindings(
        worker_registrations=(
            WorkerRegistration(worker_id=WORKER, kind="openai_compatible", network_class="local"),
        ),
        baseline_certification_targets=(target,),
        role_evaluation_targets=(_role_target(),),
        certification_poll_interval_seconds=0.05,
    )
    application = create_app(git_repo_with_commit, bindings=bindings)
    client = application.test_client()
    try:
        identity = runtime_profile_identity_from_config(
            model_tag=target.expectation.model_tag,
            model_digest=target.expectation.model_digest,
            endpoint=target.expectation.openai_base_url,
            runtime_version=target.expectation.runtime_version,
            effective_context_tokens=target.expectation.effective_context_tokens,
            temperature=target.expectation.temperature,
            normalizer_id=target.expectation.normalizer_id,
            normalizer_version=target.expectation.normalizer_version,
        )
        resolved = repo_identity.resolve(git_repo_with_commit, create=False)
        production = connect(db_path(resolved.repo_id, resolved.worktree_id))
        migrate(production)
        try:
            _seed_production_baseline_pass(production, identity)
            script.default_completion = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "I refuse."}}]},
            ).encode()

            started = _start_after_preflight(client)
            data = _wait_for_run(client, started["run_id"])
            assert data["state"] == "FAIL"
            assert data["has_certificate"] is True

            detail = client.get(f"/api/certification/workers/{WORKER}").get_json()
            assert detail["roles"]["PLANNER"]["status"] == "NOT_CERTIFIED"
            assert detail["production_eligibility"]["eligible"] is False
            assert detail["production_eligibility"]["reason"] == "role_qualification_fail"

            rows = RoleCertificatesRepo(production).list_for_worker_role(WORKER, "PLANNER")
            assert len(rows) == 1
            assert rows[0].outcome == "FAIL"

            # H.2 follow-up: the prior FAIL must not permanently block a
            # new attempt at the HTTP/service layer either (not just the
            # live-runner module) -- preflight and start must both
            # succeed again once the (simulated) underlying problem is
            # fixed, reusing the SAME worker/runtime.
            script.default_completion = _plan_body()
            preflight2 = client.post(
                f"/api/certification/workers/{WORKER}/planner/preflight", json={},
            ).get_json()
            assert preflight2["ready"] is True, preflight2
            named = {c["name"]: c for c in preflight2["checks"]}
            assert named["production_baseline_security"]["ok"] is True

            second_started = _start_after_preflight(client)
            second_data = _wait_for_run(client, second_started["run_id"])
            assert second_data["state"] == "PASS"

            rows_after = RoleCertificatesRepo(production).list_for_worker_role(WORKER, "PLANNER")
            assert len(rows_after) == 2
            assert {r.outcome for r in rows_after} == {"FAIL", "PASS"}

            detail_after = client.get(f"/api/certification/workers/{WORKER}").get_json()
            assert detail_after["roles"]["PLANNER"]["status"] == "CERTIFIED"
            assert detail_after["production_eligibility"]["eligible"] is True
        finally:
            production.close()
    finally:
        application.extensions["codeslayer"].close()
