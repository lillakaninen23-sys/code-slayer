"""H.3 Stage 3: Certification Center archived-worker gates.

Drives `security.certification_service.CertificationService` directly
(no Flask app, no background `CertificationJobExecutor` thread) so
every scenario is deterministic: preflight/start/promote/execute are
each invoked by the test itself, in an exact, controlled order --
including the "queued, then archived, then claimed" race that a
background-poller-driven test could not reliably reproduce.

Covers: preflight stops before any live Ollama contact for an
archived worker; a stale READY preflight cannot start certification
after archival; a run queued before archive still finishes INCOMPLETE/
`worker_archived` with zero model calls and no certificate if the
worker is archived before it is claimed; Baseline Security promotion
is blocked after archive; Planner certification start is blocked
after archive; `worker_summary()` reports lifecycle state and stops
claiming certification-readiness for an archived worker.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from code_slayer.repo import identity as repo_identity
from code_slayer.runner import LocalWorkerRunner
from code_slayer.security.certification_service import (
    BaselineCertificationTarget,
    CertificationBlocked,
    CertificationService,
    RoleEvaluationTarget,
)
from code_slayer.security.live_certification import LiveOllamaRuntimeExpectation
from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.workers.lifecycle import archive_worker, reactivate_worker
from code_slayer.workers.role_qualification import ProductionRole
from code_slayer.workers.security_baseline import (
    SecurityBaselineOutcome,
    record_baseline_certificate,
    runtime_profile_identity_from_config,
)

WORKER = "lifecycle-cert-worker"


class _Script:
    def __init__(self) -> None:
        self.version = "0.16.1"
        self.models = [{"name": "qwen3-coder:30b", "digest": "sha256:abc"}]
        self.completion_posts = 0


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
            script.completion_posts += 1
            # Never expected to be reached by any test in this file --
            # every scenario here refuses BEFORE a model call.
            self._write(200, json.dumps({"choices": []}).encode())

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
        model_tag="qwen3-coder:30b", model_digest="sha256:abc", endpoint=f"{root}/v1",
        runtime_version="0.16.1", effective_context_tokens=16384, temperature=0.0,
        normalizer_id=None, normalizer_version=None,
    )
    return BaselineCertificationTarget(
        worker_id=WORKER,
        expectation=LiveOllamaRuntimeExpectation(
            ollama_root=root, model_tag="qwen3-coder:30b", model_digest="sha256:abc",
            runtime_version="0.16.1", effective_context_tokens=16384, temperature=0.0,
            expected_runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
            normalizer_id=None, normalizer_version=None, timeout=5.0,
        ),
    )


def _role_target() -> RoleEvaluationTarget:
    return RoleEvaluationTarget(
        worker_id=WORKER, role=ProductionRole.PLANNER, output_token_budget=1024,
        tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
        policy_version="planner-certification-v1",
    )


@pytest.fixture
def service(git_repo_with_commit, runtime_server):
    _script, root = runtime_server
    runner = LocalWorkerRunner(git_repo_with_commit)
    runner.register_worker(worker_id=WORKER, kind="openai_compatible", network_class="local")
    runner.close()
    resolved = repo_identity.resolve(git_repo_with_commit, create=False)
    svc = CertificationService(
        resolved.repo_id, resolved.worktree_id,
        targets=(_target(root),), role_targets=(_role_target(),),
    )
    yield svc
    svc.close()


def _seed_production_baseline_pass(svc: CertificationService, root: str):
    identity = runtime_profile_identity_from_config(
        model_tag="qwen3-coder:30b", model_digest="sha256:abc", endpoint=f"{root}/v1",
        runtime_version="0.16.1", effective_context_tokens=16384, temperature=0.0,
        normalizer_id=None, normalizer_version=None,
    )
    result = record_baseline_certificate(
        svc.production_conn(), worker_id=WORKER, runtime_profile=identity,
        outcome=SecurityBaselineOutcome.PASS, evidence_ref="baseline-ev", reason="ok",
    )
    assert result.ok, result.reason
    return result.certificate


# -- preflight stops before any live Ollama contact for an archived worker --


def test_baseline_preflight_never_contacts_ollama_when_archived(service, runtime_server):
    script, _root = runtime_server
    archived = archive_worker(service.production_conn(), worker_id=WORKER)
    assert archived.ok and archived.changed

    result = service.run_preflight(WORKER)
    assert result["ready"] is False
    named = {c["name"]: c for c in result["checks"]}
    assert named["worker_lifecycle_active"]["ok"] is False
    assert named["worker_lifecycle_active"]["detail"] == "worker_archived"
    assert named["ollama_reachable"]["detail"] == "not_evaluated"
    assert script.completion_posts == 0


def test_planner_preflight_never_contacts_ollama_when_archived(service, runtime_server):
    script, root = runtime_server
    _seed_production_baseline_pass(service, root)
    archived = archive_worker(service.production_conn(), worker_id=WORKER)
    assert archived.ok and archived.changed

    result = service.run_planner_preflight(WORKER)
    assert result["ready"] is False
    named = {c["name"]: c for c in result["checks"]}
    assert named["worker_lifecycle_active"]["ok"] is False
    assert named["ollama_reachable"]["detail"] == "not_evaluated"
    assert script.completion_posts == 0


def test_baseline_preflight_succeeds_normally_when_active(service):
    result = service.run_preflight(WORKER)
    named = {c["name"]: c for c in result["checks"]}
    assert named["worker_lifecycle_active"]["ok"] is True


# -- stale READY preflight cannot start certification after archive ---------


def test_baseline_start_refuses_stale_ready_preflight_after_archive(service):
    ready = service.run_preflight(WORKER)
    assert ready["ready"] is True, ready
    archived = archive_worker(service.production_conn(), worker_id=WORKER)
    assert archived.ok and archived.changed

    with pytest.raises(CertificationBlocked) as excinfo:
        service.start_baseline_run(WORKER)
    assert excinfo.value.code == "worker_archived"
    # No run was queued -- the READY row from preflight is untouched.
    row = service.get_run(ready["run_id"])
    assert row["state"] == "READY"


def test_planner_start_refuses_stale_ready_preflight_after_archive(service, runtime_server):
    _script, root = runtime_server
    _seed_production_baseline_pass(service, root)
    ready = service.run_planner_preflight(WORKER)
    assert ready["ready"] is True, ready
    archived = archive_worker(service.production_conn(), worker_id=WORKER)
    assert archived.ok and archived.changed

    with pytest.raises(CertificationBlocked) as excinfo:
        service.start_planner_certification(WORKER)
    assert excinfo.value.code == "worker_archived"


def test_baseline_start_succeeds_again_after_reactivation(service):
    service.run_preflight(WORKER)
    archive_worker(service.production_conn(), worker_id=WORKER)
    reactivate_worker(service.production_conn(), worker_id=WORKER)
    # A fresh preflight is required again (the READY row was never
    # superseded by the refused start attempt, and re-running preflight
    # here proves the normal path is unobstructed post-reactivation).
    ready = service.run_preflight(WORKER)
    assert ready["ready"] is True
    started = service.start_baseline_run(WORKER)
    assert started["state"] == "QUEUED"


# -- queued-before-archive: claim/execute must not call the model -----------


def test_baseline_queued_run_finishes_incomplete_without_model_call_if_archived_before_claim(
    service, runtime_server,
):
    script, _root = runtime_server
    service.run_preflight(WORKER)
    queued = service.start_baseline_run(WORKER)
    assert queued["state"] == "QUEUED"

    archived = archive_worker(service.production_conn(), worker_id=WORKER)
    assert archived.ok and archived.changed

    claimed = service.claim_run(queued["run_id"])
    assert claimed is not None
    service.execute_claimed_run(claimed)

    finished = service.get_run(queued["run_id"])
    assert finished["state"] == "INCOMPLETE"
    assert finished["reason"] == "worker_archived"
    assert script.completion_posts == 0
    assert BaselineSecurityCertificatesRepo(service.production_conn()).list_for_worker(WORKER) == []


def test_planner_queued_run_finishes_incomplete_without_model_call_if_archived_before_claim(
    service, runtime_server,
):
    script, root = runtime_server
    _seed_production_baseline_pass(service, root)
    service.run_planner_preflight(WORKER)
    queued = service.start_planner_certification(WORKER)
    assert queued["state"] == "QUEUED"

    archived = archive_worker(service.production_conn(), worker_id=WORKER)
    assert archived.ok and archived.changed

    claimed = service.claim_run(queued["run_id"])
    assert claimed is not None
    service.execute_claimed_run(claimed)

    finished = service.get_run(queued["run_id"])
    assert finished["state"] == "INCOMPLETE"
    assert finished["reason"] == "worker_archived"
    assert script.completion_posts == 0
    assert RoleCertificatesRepo(service.production_conn()).list_for_worker_role(
        WORKER, "PLANNER",
    ) == []


def test_baseline_queued_run_executes_normally_when_still_active(service, runtime_server):
    """Sanity check: the recheck does not spuriously refuse an ACTIVE
    worker -- the model IS contacted (the fake server's canned `{"choices":
    []}` response then fails classification harmlessly, proving the run
    reached execution, not that it certified PASS)."""
    script, _root = runtime_server
    service.run_preflight(WORKER)
    queued = service.start_baseline_run(WORKER)
    claimed = service.claim_run(queued["run_id"])
    service.execute_claimed_run(claimed)
    assert script.completion_posts > 0


# -- promotion blocked after archive -----------------------------------------


def test_promotion_blocked_after_archive(service):
    """`promote_to_production()`'s own lifecycle recheck fires before
    it ever looks for a VALIDATION PASS certificate to carry forward --
    no live evaluation is needed to prove this boundary."""
    archived = archive_worker(service.production_conn(), worker_id=WORKER)
    assert archived.ok and archived.changed

    with pytest.raises(CertificationBlocked) as excinfo:
        service.promote_to_production(WORKER)
    assert excinfo.value.code == "worker_archived"


# -- worker_summary() reports lifecycle and stops claiming readiness --------


def test_worker_summary_reports_lifecycle_state_and_changed_at(service):
    summary = service.worker_summary(WORKER)
    assert summary["lifecycle_state"] == "ACTIVE"
    assert summary["lifecycle_changed_at"] is None

    archive_worker(service.production_conn(), worker_id=WORKER)
    summary = service.worker_summary(WORKER)
    assert summary["lifecycle_state"] == "ARCHIVED"
    assert summary["lifecycle_changed_at"] is not None


def test_worker_summary_readiness_flags_become_false_when_archived(service, runtime_server):
    _script, root = runtime_server
    _seed_production_baseline_pass(service, root)
    service.run_preflight(WORKER)
    service.run_planner_preflight(WORKER)
    before = service.worker_summary(WORKER)
    assert before["ready_for_certification"] is True
    assert before["planner_ready_for_certification"] is True

    archive_worker(service.production_conn(), worker_id=WORKER)
    after = service.worker_summary(WORKER)
    assert after["ready_for_certification"] is False
    assert after["planner_ready_for_certification"] is False
    assert after["promotion_reason"] == "worker_archived"
    planner_action = next(a for a in after["future_actions"] if a["role"] == "PLANNER")
    assert planner_action["available"] is False
    assert planner_action["reason"] == "worker_archived"


def test_archived_worker_remains_visible_in_history_and_list(service):
    archive_worker(service.production_conn(), worker_id=WORKER)
    workers = service.list_workers()
    assert any(w["worker_id"] == WORKER for w in workers)
    history = service.history(WORKER)
    assert history is not None
