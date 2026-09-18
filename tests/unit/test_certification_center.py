"""Certification Center v1: WebUI API over canonical live certification."""

from __future__ import annotations

import ast
import http.server
import inspect
import json
import threading
import time
from pathlib import Path

import pytest

from code_slayer.api import create_app
from code_slayer.api.service import RuntimeBindings, WorkerRegistration
from code_slayer.repo import identity as repo_identity
from code_slayer.security.certification_service import (
    BaselineCertificationTarget,
    RoleEvaluationTarget,
)
from code_slayer.security.evaluation import TOOL_CANARY_DELETE, mandatory_cases
from code_slayer.security.live_certification import LiveOllamaRuntimeExpectation
from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.db import connect, migrate
from code_slayer.store.location import (
    db_path,
    validation_certification_blobs_dir,
    validation_certification_db_path,
)
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.workers.role_qualification import (
    ProductionRole,
    RoleQualificationOutcome,
    record_role_certificate,
    role_evaluation_identity_from_config,
)
from code_slayer.workers.security_baseline import runtime_profile_identity_from_config
from code_slayer.workers.trust import TrustLevel, WorkerTrustManager


class _Script:
    def __init__(self) -> None:
        self.version = "0.16.1"
        self.models = [{"name": "qwen3-coder:30b", "digest": "sha256:abc"}]
        self.completion_posts = 0
        self.completion_status = 200
        self.completions: list[bytes] | None = None
        self.default_completion = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "I refuse."}}]},
        ).encode()


def _text_body(text: str = "I refuse.") -> bytes:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": text}}]},
    ).encode()


def _tool_body(name: str, params: dict) -> bytes:
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
                                    "name": name,
                                    "arguments": json.dumps(params),
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


WORKER = "local-ollama-qwen3-coder-30b"


def _target(root: str) -> BaselineCertificationTarget:
    identity = runtime_profile_identity_from_config(
        model_tag="qwen3-coder:30b",
        model_digest="sha256:abc",
        endpoint=f"{root}/v1",
        runtime_version="0.16.1",
        effective_context_tokens=16384,
        temperature=0.0,
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
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
            normalizer_id="qwen_textual_tool_v1",
            normalizer_version=1,
            timeout=5.0,
        ),
    )


def _role_target() -> RoleEvaluationTarget:
    return RoleEvaluationTarget(
        worker_id=WORKER,
        role=ProductionRole.PLANNER,
        output_token_budget=4096,
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
    role_eval = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        output_token_budget=4096,
        tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
        policy_version="planner-certification-v1",
    )
    resolved = repo_identity.resolve(git_repo_with_commit, create=False)
    conn = connect(db_path(resolved.repo_id, resolved.worktree_id))
    migrate(conn)
    record_role_certificate(
        conn,
        worker_id=WORKER,
        role=ProductionRole.PLANNER,
        runtime_profile=identity,
        role_evaluation=role_eval,
        outcome=RoleQualificationOutcome.PASS,
        evidence_ref="planner-evidence",
        reason="qualified",
        classification="PASS_FIRST_TRY",
        policy_version="planner-certification-v1",
    )
    yield client, application, git_repo_with_commit, identity
    application.extensions["codeslayer"].close()
    conn.close()


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
        f"/api/certification/workers/{WORKER}/baseline/preflight", json={},
    ).get_json()
    assert preflight["ready"] is True
    start = client.post(f"/api/certification/workers/{WORKER}/baseline/runs", json={})
    assert start.status_code == 202
    return start.get_json()


def test_lists_registered_workers_and_separates_role_from_baseline(app_client):
    client, _app, _repo, _identity = app_client
    data = client.get("/api/certification/workers").get_json()
    assert data["environment"] == "VALIDATION"
    worker = data["workers"][0]
    assert worker["worker_id"] == WORKER
    assert worker["runtime"]["status"] == "UNKNOWN"
    assert worker["baseline_security"]["status"] == "NOT_CERTIFIED"
    assert worker["roles"]["PLANNER"]["status"] == "CERTIFIED"
    assert worker["roles"]["CODER"]["status"] == "NOT_CERTIFIED"
    assert worker["roles"]["REVIEWER"]["status"] == "NOT_CERTIFIED"
    assert worker["roles"]["REPAIRER"]["status"] == "NOT_CERTIFIED"
    assert worker["roles"]["SECURITY"]["status"] == "NOT_CERTIFIED"
    assert worker["production_eligibility"]["eligible"] is False
    assert worker["production_eligibility"]["reason"] == "no_baseline_security_certificate"
    assert worker["production_eligibility"]["source"] == "evaluate_production_eligibility"
    assert worker["ready_for_certification"] is False


def test_identity_sources_distinguish_config_bound_from_live_attested(app_client):
    client, _app, _repo, identity = app_client
    before = client.get(f"/api/certification/workers/{WORKER}").get_json()
    assert before["identity"]["model_digest"]["source"] == "CONFIG_BOUND"
    assert before["identity"]["runtime_version"]["source"] == "CONFIG_BOUND"
    assert before["identity"]["effective_context_tokens"]["source"] == "CONFIG_BOUND"
    assert before["identity"]["effective_context_tokens"]["measured_by_ollama"] is False
    assert before["identity"]["runtime_identity_fingerprint"]["value"] == (
        identity.runtime_identity_fingerprint
    )
    preflight = client.post(
        f"/api/certification/workers/{WORKER}/baseline/preflight", json={},
    ).get_json()
    assert preflight["ready"] is True
    after = client.get(f"/api/certification/workers/{WORKER}").get_json()
    assert after["runtime"]["status"] == "VERIFIED"
    assert after["identity"]["model_digest"]["source"] == "LIVE_ATTESTED"
    assert after["identity"]["runtime_version"]["source"] == "LIVE_ATTESTED"
    assert after["identity"]["effective_context_tokens"]["measured_by_ollama"] is False
    assert after["ready_for_certification"] is True


def test_preflight_does_not_infer_and_failed_preflight_blocks_start(
    app_client, runtime_server,
):
    client, _app, _repo, _identity = app_client
    script, _root = runtime_server
    script.models = [{"name": "qwen3-coder:30b", "digest": "sha256:other"}]
    before = script.completion_posts
    result = client.post(
        f"/api/certification/workers/{WORKER}/baseline/preflight",
        json={},
    ).get_json()
    assert result["ready"] is False
    assert script.completion_posts == before
    digest = next(item for item in result["checks"] if item["name"] == "digest_matches")
    assert digest["ok"] is False
    start = client.post(f"/api/certification/workers/{WORKER}/baseline/runs", json={})
    assert start.status_code == 409
    assert start.get_json()["error"]["code"] == "preflight_required"
    worker = client.get(f"/api/certification/workers/{WORKER}").get_json()
    assert worker["runtime"]["status"] == "MISMATCH"


def test_start_without_preflight_is_blocked(app_client):
    client, _app, _repo, _identity = app_client
    start = client.post(f"/api/certification/workers/{WORKER}/baseline/runs", json={})
    assert start.status_code == 409
    assert start.get_json()["error"]["code"] == "preflight_required"


def test_successful_preflight_then_start_delegates_and_records_validation_cert(
    app_client, runtime_server,
):
    client, application, repo, identity = app_client
    script, _root = runtime_server
    preflight = client.post(
        f"/api/certification/workers/{WORKER}/baseline/preflight", json={},
    ).get_json()
    assert preflight["ready"] is True
    assert script.completion_posts == 0
    start = client.post(f"/api/certification/workers/{WORKER}/baseline/runs", json={})
    assert start.status_code == 202
    run_id = start.get_json()["run_id"]
    duplicate = client.post(f"/api/certification/workers/{WORKER}/baseline/runs", json={})
    assert duplicate.status_code == 409
    assert duplicate.get_json()["error"]["code"] == "certification_already_in_progress"
    data = _wait_for_run(client, run_id)
    assert data["state"] == "PASS"
    assert data["has_certificate"] is True
    assert data["environment"] == "VALIDATION"
    assert data["certificate_id"]
    assert script.completion_posts >= 9
    poll = client.get(f"/api/certification/runs/{run_id}").get_json()
    assert poll["run_id"] == run_id
    evidence = client.get(f"/api/certification/runs/{run_id}/evidence").get_json()
    assert evidence["document"]["final_outcome"] == "PASS"
    assert evidence["document"]["runtime_identity_fingerprint"] == (
        identity.runtime_identity_fingerprint
    )
    resolved = repo_identity.resolve(repo, create=False)
    production_path = db_path(resolved.repo_id, resolved.worktree_id)
    validation_path = validation_certification_db_path(resolved.repo_id, resolved.worktree_id)
    assert production_path != validation_path
    assert "validation-certification" in str(validation_path)
    production = connect(production_path)
    validation = connect(validation_path)
    assert BaselineSecurityCertificatesRepo(production).list_for_worker(WORKER) == []
    validation_certs = BaselineSecurityCertificatesRepo(validation).list_for_worker(WORKER)
    assert len(validation_certs) == 1
    assert validation_certs[0].outcome == "PASS"
    assert RoleCertificatesRepo(production).list_for_worker_role(WORKER, "PLANNER")
    assert production.execute("SELECT count(*) AS c FROM worker_trust_events").fetchone()["c"] == 0
    assert production.execute("SELECT count(*) AS c FROM permission_grants").fetchone()["c"] == 0
    manager = WorkerTrustManager(production)
    assert manager.current_trust(WORKER, "coder", "read_file") == TrustLevel.LOCKED
    listed = client.get("/api/certification/workers").get_json()["workers"][0]
    assert listed["baseline_security"]["status"] == "CERTIFIED"
    assert listed["baseline_security"]["environment"] == "VALIDATION"
    assert listed["production_eligibility"]["eligible"] is False
    assert listed["production_eligibility"]["reason"] == "no_baseline_security_certificate"
    production.close()
    validation.close()
    blobs = validation_certification_blobs_dir(resolved.repo_id, resolved.worktree_id)
    assert blobs.exists()


# -- H.1: VALIDATION -> PRODUCTION Baseline Security promotion --------------


def test_promotion_end_to_end_records_production_certificate(app_client, runtime_server):
    client, _app, repo, identity = app_client
    script, _root = runtime_server
    started = _start_after_preflight(client)
    data = _wait_for_run(client, started["run_id"])
    assert data["state"] == "PASS"
    validation_certificate_id = data["certificate_id"]

    before = client.get(f"/api/certification/workers/{WORKER}").get_json()
    assert before["baseline_security"]["status"] == "CERTIFIED"
    assert before["promotion_available"] is True
    assert before["promotion_reason"] == "promotable"

    response = client.post(f"/api/certification/workers/{WORKER}/baseline/promote", json={})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["environment"] == "PRODUCTION"
    assert payload["validation_certificate_id"] == validation_certificate_id
    assert payload["production_certificate_id"]
    assert payload["runtime_identity_fingerprint"] == identity.runtime_identity_fingerprint

    resolved = repo_identity.resolve(repo, create=False)
    production = connect(db_path(resolved.repo_id, resolved.worktree_id))
    rows = BaselineSecurityCertificatesRepo(production).list_for_worker(WORKER)
    assert len(rows) == 1
    assert rows[0].outcome == "PASS"
    assert rows[0].certificate_id == payload["production_certificate_id"]
    # Promotion alone grants no trust, permission, or role certificate --
    # the PLANNER row here was seeded directly by the fixture, not by
    # this promotion.
    assert production.execute("SELECT count(*) AS c FROM worker_trust_events").fetchone()["c"] == 0
    assert production.execute("SELECT count(*) AS c FROM permission_grants").fetchone()["c"] == 0
    production.close()

    listed = client.get("/api/certification/workers").get_json()["workers"][0]
    assert listed["production_eligibility"]["eligible"] is True
    assert listed["production_eligibility"]["reason"] == "eligible"
    # VALIDATION PASS alone never implies promotion availability: the
    # VALIDATION certificate is still CERTIFIED, but the backend
    # projection now reports no further promotion is available.
    assert listed["baseline_security"]["status"] == "CERTIFIED"
    assert listed["promotion_available"] is False
    assert listed["promotion_reason"] == "already_promoted_to_production"

    history = client.get(f"/api/certification/workers/{WORKER}/history").get_json()
    production_certs = history["production_certificates"]
    assert len(production_certs) == 1
    assert production_certs[0]["certificate_id"] == payload["production_certificate_id"]


def test_repeated_promotion_is_idempotent_over_http(app_client, runtime_server):
    client, _app, repo, _identity = app_client
    started = _start_after_preflight(client)
    data = _wait_for_run(client, started["run_id"])
    assert data["state"] == "PASS"

    first = client.post(f"/api/certification/workers/{WORKER}/baseline/promote", json={})
    assert first.status_code == 200
    first_payload = first.get_json()
    assert first_payload["reason"] == "promoted_to_production"

    second = client.post(f"/api/certification/workers/{WORKER}/baseline/promote", json={})
    assert second.status_code == 200
    second_payload = second.get_json()
    assert second_payload["reason"] == "already_promoted_to_production"
    assert second_payload["production_certificate_id"] == first_payload["production_certificate_id"]

    resolved = repo_identity.resolve(repo, create=False)
    production = connect(db_path(resolved.repo_id, resolved.worktree_id))
    rows = BaselineSecurityCertificatesRepo(production).list_for_worker(WORKER)
    assert len(rows) == 1
    production.close()


def test_promotion_rejects_client_supplied_authority_fields(app_client):
    client, _app, _repo, _identity = app_client
    for body in (
        {"outcome": "PASS"},
        {"evidence_ref": "abc"},
        {"certificate_id": "abc"},
        {"hard_disqualifiers": []},
    ):
        response = client.post(
            f"/api/certification/workers/{WORKER}/baseline/promote", json=body,
        )
        assert response.status_code == 400
        assert response.get_json()["error"]["code"] == "invalid_fields"


def test_promotion_denied_without_a_pass_validation_certificate(app_client):
    client, _app, _repo, _identity = app_client
    worker = client.get(f"/api/certification/workers/{WORKER}").get_json()
    assert worker["promotion_available"] is False
    assert worker["promotion_reason"] == "no_validation_certificate"
    response = client.post(f"/api/certification/workers/{WORKER}/baseline/promote", json={})
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "no_validation_certificate"


def test_promotion_denied_for_unregistered_worker(app_client):
    client, _app, _repo, _identity = app_client
    response = client.post(
        "/api/certification/workers/ghost-worker/baseline/promote", json={},
    )
    assert response.status_code == 404


def test_promotion_denied_when_hard_disqualified_validation_certificate_only(
    app_client, runtime_server,
):
    client, _app, _repo, _identity = app_client
    script, _root = runtime_server
    n = len(mandatory_cases())
    script.completions = [_text_body() for _ in range(n)]
    script.completions[4] = _tool_body(
        TOOL_CANARY_DELETE,
        {"path": "cslr-canary://workspace/obsolete.txt"},
    )
    started = _start_after_preflight(client)
    data = _wait_for_run(client, started["run_id"])
    assert data["state"] == "HARD_DISQUALIFIED"

    worker = client.get(f"/api/certification/workers/{WORKER}").get_json()
    assert worker["promotion_available"] is False
    assert worker["promotion_reason"] == "validation_certificate_not_pass"

    response = client.post(f"/api/certification/workers/{WORKER}/baseline/promote", json={})
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "validation_certificate_not_pass"


def test_browser_close_does_not_cancel_started_run(app_client, runtime_server):
    client, _app, repo, _identity = app_client
    started = _start_after_preflight(client)
    run_id = started["run_id"]
    time.sleep(1.5)
    data = _wait_for_run(client, run_id)
    assert data["state"] == "PASS"
    assert data["has_certificate"] is True
    resolved = repo_identity.resolve(repo, create=False)
    production = connect(db_path(resolved.repo_id, resolved.worktree_id))
    assert BaselineSecurityCertificatesRepo(production).list_for_worker(WORKER) == []
    production.close()


def test_fail_records_validation_certificate_not_production(app_client, runtime_server):
    client, _app, repo, _identity = app_client
    script, _root = runtime_server
    script.completion_status = 500
    started = _start_after_preflight(client)
    data = _wait_for_run(client, started["run_id"])
    assert data["state"] == "FAIL"
    assert data["has_certificate"] is True
    resolved = repo_identity.resolve(repo, create=False)
    production = connect(db_path(resolved.repo_id, resolved.worktree_id))
    validation = connect(validation_certification_db_path(resolved.repo_id, resolved.worktree_id))
    assert BaselineSecurityCertificatesRepo(production).list_for_worker(WORKER) == []
    rows = BaselineSecurityCertificatesRepo(validation).list_for_worker(WORKER)
    assert len(rows) == 1
    assert rows[0].outcome == "FAIL"
    listed = client.get("/api/certification/workers").get_json()["workers"][0]
    assert listed["baseline_security"]["status"] == "FAILED"
    assert listed["production_eligibility"]["eligible"] is False
    production.close()
    validation.close()


def test_hard_disqualification_records_validation_certificate(app_client, runtime_server):
    client, _app, repo, _identity = app_client
    script, _root = runtime_server
    n = len(mandatory_cases())
    script.completions = [_text_body() for _ in range(n)]
    script.completions[4] = _tool_body(
        TOOL_CANARY_DELETE,
        {"path": "cslr-canary://workspace/obsolete.txt"},
    )
    started = _start_after_preflight(client)
    data = _wait_for_run(client, started["run_id"])
    assert data["state"] == "HARD_DISQUALIFIED"
    assert data["has_certificate"] is True
    assert "DESTRUCTIVE_BEHAVIOR" in data["hard_disqualifiers"]
    evidence = client.get(
        f"/api/certification/runs/{started['run_id']}/evidence",
    ).get_json()
    assert evidence["document"]["final_outcome"] == "HARD_DISQUALIFIED"
    resolved = repo_identity.resolve(repo, create=False)
    production = connect(db_path(resolved.repo_id, resolved.worktree_id))
    validation = connect(validation_certification_db_path(resolved.repo_id, resolved.worktree_id))
    assert BaselineSecurityCertificatesRepo(production).list_for_worker(WORKER) == []
    rows = BaselineSecurityCertificatesRepo(validation).list_for_worker(WORKER)
    assert len(rows) == 1
    assert rows[0].outcome == "HARD_DISQUALIFIED"
    production.close()
    validation.close()


def test_rejects_client_supplied_authority_fields(app_client):
    client, _app, _repo, _identity = app_client
    for body in (
        {"outcome": "PASS"},
        {"evidence_ref": "abc"},
        {"hard_disqualifiers": []},
        {"adapter": "fake"},
        {"fingerprint": "abc"},
    ):
        response = client.post(
            f"/api/certification/workers/{WORKER}/baseline/runs", json=body,
        )
        assert response.status_code == 400
        assert response.get_json()["error"]["code"] == "invalid_fields"
        preflight = client.post(
            f"/api/certification/workers/{WORKER}/baseline/preflight", json=body,
        )
        assert preflight.status_code == 400
    get_start = client.get(f"/api/certification/workers/{WORKER}/baseline/runs")
    assert get_start.status_code in {404, 405}
    get_preflight = client.get(f"/api/certification/workers/{WORKER}/baseline/preflight")
    assert get_preflight.status_code in {404, 405}


def test_webui_never_contacts_ollama_and_api_does_not_accept_adapter():
    source = Path("src/code_slayer/api/routes.py").read_text()
    assert "192.168.32.8" not in source
    shipped = [Path("webui/index.html").read_text()]
    for path in sorted(Path("webui/static").iterdir()):
        if path.is_file():
            shipped.append(path.read_text())
    frontend = "\n".join(shipped)
    # Commit A restored the Control Room; certification routes are not
    # in the UI yet. Keep the invariants: no machine-specific Ollama
    # address, browser fetch only through same-origin /api.
    assert "192.168.32.8" not in frontend
    assert "fetch(" not in Path("webui/static/app.js").read_text()
    assert "fetch(" not in Path("webui/static/views.js").read_text()
    api = Path("webui/static/api.js").read_text()
    assert "globalThis.fetch.bind" in api
    assert "/api${path}" in api
    assert "11434" not in frontend
    service = Path("src/code_slayer/security/certification_service.py").read_text()
    assert "192.168.32.8" not in service
    assert "FakeWorkerAdapter" not in service
    tree = ast.parse(service)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
    assert "FakeWorkerAdapter" not in imported
    sig = inspect.signature(
        __import__(
            "code_slayer.security.live_certification", fromlist=["certify_live_baseline_security"]
        ).certify_live_baseline_security,
    )
    assert "outcome" not in sig.parameters
    assert "evidence_ref" not in sig.parameters
    assert "adapter" not in sig.parameters


def test_incomplete_run_has_no_certificate_display(app_client, runtime_server):
    client, _app, repo, _identity = app_client
    script, _root = runtime_server
    script.models = []
    result = client.post(
        f"/api/certification/workers/{WORKER}/baseline/preflight", json={},
    ).get_json()
    assert result["state"] == "INCOMPLETE"
    assert result["has_certificate"] is False
    history = client.get(f"/api/certification/workers/{WORKER}/history").get_json()
    assert history["runs"][0]["has_certificate"] is False
    resolved = repo_identity.resolve(repo, create=False)
    validation = connect(validation_certification_db_path(resolved.repo_id, resolved.worktree_id))
    assert BaselineSecurityCertificatesRepo(validation).list_for_worker(WORKER) == []
    production = connect(db_path(resolved.repo_id, resolved.worktree_id))
    assert BaselineSecurityCertificatesRepo(production).list_for_worker(WORKER) == []
    validation.close()
    production.close()
