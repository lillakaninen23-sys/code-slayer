"""Deterministic HTTP-to-runner tests, real temp repositories and control stores."""

from unittest.mock import patch

import pytest

from code_slayer.api import create_app
from code_slayer.api.service import RuntimeBindings
from code_slayer.audit.events import EventType
from code_slayer.audit.verify import verify_chain
from code_slayer.audit.writer import AuditWriter
from code_slayer.runner import LocalWorkerRunner
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.conformance import run_conformance_suite
from code_slayer.workers.fake_adapter import FakeWorkerAdapter
from code_slayer.workers.fake_prompt_analyst import FakePromptAnalyst
from code_slayer.workers.promotion import promote_from_conformance
from code_slayer.workers.prompt_analysis import Ambiguity, AmbiguityRiskClass, PromptAnalysis
from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind, WorkerToolCall
from code_slayer.workers.question_gate import ResolutionKind
from tests.repo_helpers import git

PROMPT = "Read README.md."
WORKER = "test-worker"


def text_response(text="done"):
    return WorkerResponse(kind=WorkerResponseKind.TEXT, text=text)


def read_response():
    return WorkerResponse(
        kind=WorkerResponseKind.TOOL_CALL,
        tool_call=WorkerToolCall(tool="read_file", params={"path": "README.md"}),
    )


@pytest.fixture
def setup(git_repo_with_commit):
    repo = git_repo_with_commit
    with_runner = LocalWorkerRunner(repo)
    WorkersRepo(with_runner._control_conn).register(
        worker_id=WORKER,
        kind="fake",
        network_class="local",
    )
    responses = [
        text_response(),
        text_response(),
        read_response(),
        text_response(),
        read_response(),
    ]
    suite = run_conformance_suite(
        with_runner._control_conn,
        FakeWorkerAdapter(responses),
        worker_id=WORKER,
        role="coder",
    )
    assert promote_from_conformance(
        with_runner._control_conn,
        worker_id=WORKER,
        role="coder",
        capability="read_file",
        run_id=suite.run_id,
    ).ok
    with_runner.close()
    return repo


def application(repo, *, blocked=False, execute=False):
    ambiguity = Ambiguity(
        "scope", "Which files?", "Specific scope required.", AmbiguityRiskClass.DESTRUCTIVE
    )
    analyst = FakePromptAnalyst(
        [
            PromptAnalysis(
                original_prompt=PROMPT,
                ambiguities=(ambiguity,) if blocked else (),
            )
        ]
    )
    adapter = FakeWorkerAdapter([read_response(), text_response()])
    bindings = RuntimeBindings(lambda: analyst, (lambda worker, role: adapter) if execute else None)
    app = create_app(repo, bindings=bindings)
    return app.test_client(), analyst, adapter


def start(client, **extra):
    return client.post(
        "/api/runs",
        json={
            "prompt": PROMPT,
            "worker_id": WORKER,
            "role": "coder",
            **extra,
        },
    )


def test_health_project_list_and_unknown(setup):
    client, _, _ = application(setup)
    health = client.get("/api/health").json
    assert health["status"] == "ok" and health["schema_version"] == health["known_schema_version"]
    project = client.get("/api/project").json
    assert project["repository_path"] == str(setup)
    assert len(project["head"]) == 40 and project["repo_id"] and project["worktree_id"]
    assert project["control_plane_available"] and not project["detached"]
    assert client.get("/api/runs").json == {"runs": [], "next_offset": None}
    for path in (
        "/api/runs/missing",
        "/api/runs/missing/audit",
        "/api/workers/missing/trust",
        "/api/workers/missing/conformance",
    ):
        response = client.get(path)
        assert response.status_code == 404 and response.json["error"]["code"] == "not_found"
    assert client.post("/api/runs/missing/resume", json={}).status_code == 404


def test_start_delegates_and_durable_detail(setup):
    client, analyst, adapter = application(setup)
    original = LocalWorkerRunner.start
    calls = []

    def wrapped(self, **kwargs):
        calls.append(kwargs)
        return original(self, **kwargs)

    with patch.object(LocalWorkerRunner, "start", wrapped):
        response = start(client)
    assert response.status_code == 201
    run_id = response.json["run_id"]
    assert calls[0]["original_prompt"] == PROMPT
    assert calls[0]["requires_mutation"] is False
    assert "resolutions" not in calls[0] and "cloud_escalation" not in calls[0]
    assert len(analyst.calls) == 1 and adapter.calls == ()
    detail = client.get("/api/runs/" + run_id).json
    assert detail["status"] == "READY" and detail["original_prompt_hash"]
    assert detail["task_status"] is None and detail["checkpoint_refs"] == []
    assert "original_prompt" not in detail and PROMPT not in response.text
    assert client.get("/api/runs").json["runs"][0]["run_id"] == run_id
    assert detail["next_safe_action"] == "configure_worker_adapter"


def test_question_resolution_resume_and_idempotency(setup):
    client, analyst, adapter = application(setup, blocked=True, execute=True)
    response = start(client)
    run_id = response.json["run_id"]
    path = "/api/runs/" + run_id
    assert response.json["status"] == "BLOCKED_ON_QUESTIONS"
    assert adapter.calls == ()
    detail = client.get(path).json
    assert detail["questions"][0]["ambiguity_id"] == "scope"
    assert detail["next_safe_action"] == "answer_then_resume"
    original = LocalWorkerRunner.record_user_resolution
    calls = []

    def wrapped(self, *args, **kwargs):
        calls.append((args, kwargs))
        return original(self, *args, **kwargs)

    with patch.object(LocalWorkerRunner, "record_user_resolution", wrapped):
        response = client.post(
            path + "/resolutions",
            json={
                "ambiguity_id": "scope",
                "answer": "README.md only",
                "resolution_kind": "FACT",
            },
        )
    assert response.status_code == 200
    assert calls[0][1] == {"resolution_kind": ResolutionKind.FACT}
    # A fact cannot authorize a destructive ambiguity; real QuestionGate still asks.
    assert client.post(path + "/resume", json={}).json["status"] == "BLOCKED_ON_QUESTIONS"
    assert adapter.calls == ()
    client.post(
        path + "/resolutions",
        json={
            "ambiguity_id": "scope",
            "answer": "Read README.md only",
            "resolution_kind": "AUTHORIZATION",
        },
    )
    original_resume = LocalWorkerRunner.resume
    resumed = []

    def resume_wrapped(self, *args, **kwargs):
        resumed.append(kwargs)
        return original_resume(self, *args, **kwargs)

    with patch.object(LocalWorkerRunner, "resume", resume_wrapped):
        done = client.post(path + "/resume", json={})
    assert done.status_code == 200 and done.json["status"] == "COMPLETED"
    assert set(resumed[0]) == {"adapter"}
    assert len(adapter.calls) == 2 and len(analyst.calls) == 1
    # Fresh app simulates restart, with no analyst/adapter available.
    fresh = create_app(setup).test_client()
    assert fresh.post(path + "/resume", json={}).json == done.json
    detail = fresh.get(path).json
    assert detail["task_status"] == "COMPLETED" and detail["next_safe_action"] == "none"
    assert (
        client.post(
            path + "/resolutions",
            json={
                "ambiguity_id": "scope",
                "answer": "later",
                "resolution_kind": "FACT",
            },
        ).status_code
        == 409
    )
    runner = LocalWorkerRunner(setup)
    assert verify_chain(runner._control_conn).ok
    runner.close()


def test_workers_exact_trust_conformance(setup):
    client, _, _ = application(setup)
    workers = client.get("/api/workers").json["workers"]
    assert workers[0]["worker_id"] == WORKER
    assert workers[0]["availability_state"] == "UNKNOWN"
    assert "last_error" not in workers[0] and "capabilities_json" not in workers[0]
    trust = client.get(f"/api/workers/{WORKER}/trust").json
    scopes = {(s["role"], s["capability"]): s for s in trust["scopes"]}
    assert scopes["coder", "read_file"]["level"] == "GUARDED"
    assert scopes["coder", "write_file"]["level"] == "LOCKED"
    assert scopes["coder", None]["level"] == "LOCKED"
    assert len(scopes["coder", "read_file"]["history"]) == 1
    assert trust["matching"] == "exact" and trust["unrecorded_scope_level"] == "LOCKED"
    conformance = client.get(f"/api/workers/{WORKER}/conformance").json
    assert conformance["runs"][0]["status"] == "PASSED"
    assert len(conformance["runs"][0]["results"]) == 5


def test_audit_is_scoped_bounded_redacted(setup):
    client, _, _ = application(setup, execute=True)
    run_id = start(client).json["run_id"]
    runner = LocalWorkerRunner(setup)
    AuditWriter(runner._control_conn).append(
        task_id=run_id,
        event_type=EventType.RUN_FINISHED,
        actor_type="system",
        actor_id="test",
        payload={
            "reason": "failed:/secret/token",
            "token": "CREDENTIAL",
            "prompt": PROMPT,
            "path": "/private/path",
            "blob": "x" * 100000,
        },
    )
    runner.close()
    response = client.get(f"/api/runs/{run_id}/audit")
    events = response.json["events"]
    types = {e["event_type"] for e in events}
    assert {
        "PROMPT_ANALYSIS_RECORDED",
        "QUESTION_GATE_DECISION",
        "CLOUD_ESCALATION_EVALUATED",
        "WORKER_TOOL_CALL_EVALUATED",
        "OPERATION_FINISHED",
        "RUN_FINISHED",
    } <= types
    assert all(e["association"] == "run" for e in events)
    assert all(s not in response.text for s in ("CREDENTIAL", "/private", "/secret", PROMPT))
    assert len(client.get(f"/api/runs/{run_id}/audit?limit=2").json["events"]) == 2
    # Identical prompt content does not cross-associate new run-scoped provenance.
    other, _, _ = application(setup, execute=True)
    other_id = start(other).json["run_id"]
    other_events = other.get(f"/api/runs/{other_id}/audit").json["events"]
    assert not {e["id"] for e in events} & {e["id"] for e in other_events}


@pytest.mark.parametrize(
    "field",
    [
        "trust",
        "trust_level",
        "lease_generation",
        "fencing_token",
        "worker_session_id",
        "worktree_path",
        "job_worktree_path",
        "db_path",
        "requires_mutation",
        "resolutions",
        "cloud_escalation",
        "allow_cloud",
        "source",
        "adapter",
        "prompt_analyst",
    ],
)
def test_authority_inputs_rejected_everywhere(setup, field):
    client, _, adapter = application(setup, blocked=True)
    assert start(client, **{field: "forged"}).status_code == 400
    assert client.get("/api/runs").json["runs"] == []
    run_id = start(client).json["run_id"]
    assert client.post(f"/api/runs/{run_id}/resume", json={field: "forged"}).status_code == 400
    assert (
        client.post(
            f"/api/runs/{run_id}/resolutions",
            json={
                "ambiguity_id": "scope",
                "answer": "x",
                "resolution_kind": "FACT",
                field: "forged",
            },
        ).status_code
        == 400
    )
    assert adapter.calls == ()


def test_malformed_input_and_errors(setup):
    client, _, _ = application(setup, blocked=True)
    for data in ([], None, {}, {"prompt": ""}, {"prompt": 1}):
        response = client.post("/api/runs", json=data)
        assert 400 <= response.status_code < 500 and "error" in response.json
    assert client.post("/api/runs", data="{", content_type="application/json").status_code == 400
    assert start(client, capability_profile="mutation").status_code == 400
    assert start(client, worker_id="unknown").status_code == 404
    assert start(client, prompt="x" * 70000).status_code == 413
    assert client.get("/api/runs?limit=-1").status_code == 400
    run_id = start(client).json["run_id"]
    assert (
        client.post(
            f"/api/runs/{run_id}/resolutions",
            json={
                "ambiguity_id": "other",
                "answer": "x",
                "resolution_kind": "FACT",
            },
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/runs/{run_id}/resolutions",
            json={
                "ambiguity_id": "scope",
                "answer": "x",
                "resolution_kind": "SAFE_DEFAULT",
            },
        ).status_code
        == 400
    )
    with patch.object(LocalWorkerRunner, "start", side_effect=RuntimeError("secret password")):
        response = start(client)
    assert response.status_code == 500 and "secret" not in response.text
    assert set(response.json["error"]) == {"code", "message", "retryable"}


def test_default_configuration_and_browser_boundary(setup):
    client = create_app(setup).test_client()
    assert client.get("/api/health").json["actions"]["start"] is False
    assert start(client).json["error"]["code"] == "analyst_not_configured"
    assert client.get("/api/runs").json["runs"] == []
    assert (
        client.post("/api/runs", json={}, headers={"Origin": "https://evil.invalid"}).status_code
        == 403
    )
    assert client.get("/api/health", headers={"Host": "evil.invalid"}).status_code == 400
    assert client.get("/api/health", headers={"Origin": "null"}).status_code == 403
    assert client.get("/api/health", headers={"Origin": "http://localhost"}).status_code == 200
    assert "Access-Control-Allow-Origin" not in client.get("/api/health").headers
    assert client.post(f"/api/workers/{WORKER}/trust", json={}).status_code == 405


def test_cloud_gate_cannot_be_globalized(setup):
    runner = LocalWorkerRunner(setup)
    WorkersRepo(runner._control_conn).register(
        worker_id="cloud", kind="fake", network_class="cloud"
    )
    runner.close()
    client, _, adapter = application(setup, execute=True)
    response = start(client, worker_id="cloud")
    assert response.json["status"] == "FAILED"
    assert "cloud" in response.json["reason"]
    assert adapter.calls == ()
    events = client.get(f"/api/runs/{response.json['run_id']}/audit").json["events"]
    assert "CLOUD_ESCALATION_EVALUATED" in {e["event_type"] for e in events}


def test_list_pagination_newest_first(setup):
    ids = []
    for _ in range(3):
        client, _, _ = application(setup)
        ids.append(start(client).json["run_id"])
    result = client.get("/api/runs?limit=2").json
    assert [r["run_id"] for r in result["runs"]] == list(reversed(ids))[:2]
    assert result["next_offset"] == 2
    assert client.get("/api/runs?limit=2&offset=2").json["runs"][0]["run_id"] == ids[0]


def test_static_assets_are_explicit(setup, tmp_path):
    root = tmp_path / "ui"
    root.mkdir()
    (root / "static").mkdir()
    (root / "index.html").write_text("<h1>Code Slayer</h1>")
    (root / "static/app.js").write_text("/* static */")
    (root / "private").write_text("secret")
    client = create_app(setup, webui_dir=root).test_client()
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/private").status_code == 404
    assert client.get("/static/../private").status_code == 404


def test_interrupted_run_and_job_execution_state(setup):
    from code_slayer.store.task_repo import TaskRepo

    # An existing active task makes bootstrap fail closed through the real runner.
    runner = LocalWorkerRunner(setup)
    TaskRepo(runner._control_conn).create(
        description="existing task",
        repo_root=str(setup),
        repo_id=runner._primary.repo_id,
        worktree_id=runner._primary.worktree_id,
    )
    runner.close()
    client, _, _ = application(setup, execute=True)
    result = start(client).json
    assert result["status"] == "INTERRUPTED_RESUMABLE"
    detail = client.get("/api/runs/" + result["run_id"]).json
    assert detail["next_safe_action"] == "operator_reconciliation"
    assert client.post("/api/runs/" + result["run_id"] + "/resume", json={}).json == result
    # A historical job run remains inspectable through its own execution database.
    runner = LocalWorkerRunner(setup)
    result = runner.start(
        original_prompt=PROMPT,
        worker_id=WORKER,
        role="coder",
        requires_mutation=True,
        prompt_analyst=FakePromptAnalyst([PromptAnalysis(original_prompt=PROMPT)]),
        adapter=FakeWorkerAdapter([]),
    )
    runner.close()
    detail = client.get("/api/runs/" + result.run_id).json
    assert detail["status"] == "DENIED_TRUST"
    assert detail["execution"]["isolated"] is True
    assert detail["execution_state_available"] is True and detail["task_status"] == "FAILED"
    assert detail["checkpoint_refs"] == []
    audit = client.get("/api/runs/" + result.run_id + "/audit").json
    assert {"control", "execution"} == {e["plane"] for e in audit["events"]}
    assert "job_worktree_path" not in detail


def test_reads_do_not_write_or_call_runner_actions(setup):
    client, _, _ = application(setup, execute=True)
    run_id = start(client).json["run_id"]
    runner = LocalWorkerRunner(setup)
    before = runner._control_conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    with (
        patch.object(LocalWorkerRunner, "start", side_effect=AssertionError),
        patch.object(LocalWorkerRunner, "resume", side_effect=AssertionError),
        patch.object(LocalWorkerRunner, "record_user_resolution", side_effect=AssertionError),
    ):
        for path in (
            "/api/health",
            "/api/project",
            "/api/runs",
            "/api/runs/" + run_id,
            "/api/runs/" + run_id + "/audit",
            "/api/workers",
            f"/api/workers/{WORKER}/trust",
            f"/api/workers/{WORKER}/conformance",
        ):
            assert client.get(path).status_code == 200
    assert runner._control_conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == before
    runner.close()


# --- Phase 8.1: repository intelligence API ---------------------------------

def test_api_intelligence_status(setup):
    client, _, _ = application(setup)
    status = client.get("/api/intelligence/status").json
    assert status["indexed"] is False
    refreshed = client.post("/api/intelligence/refresh", json={}).json
    assert refreshed["indexed"] is True and refreshed["current"] is True
    status = client.get("/api/intelligence/status").json
    assert status["snapshot_id"] == refreshed["snapshot_id"]


def test_api_intelligence_query(setup):
    (setup / "billing.py").write_text("def charge_customer():\n    pass\n")
    client, _, _ = application(setup)
    client.post("/api/intelligence/refresh", json={})
    result = client.post(
        "/api/intelligence/query", json={"text": "please update charge_customer"},
    ).json
    assert result["stale"] is False
    assert any(c["path"] == "billing.py" for c in result["candidates"])
    assert all(c["reasons"] for c in result["candidates"])


def test_api_intelligence_context_pack(setup):
    (setup / "billing.py").write_text("def charge_customer():\n    pass\n")
    client, _, _ = application(setup)
    client.post("/api/intelligence/refresh", json={})
    pack = client.post(
        "/api/intelligence/context-pack",
        json={"text": "charge_customer", "max_files": 1, "max_bytes": 5000},
    ).json
    assert pack["files"] and pack["files"][0]["path"] == "billing.py"
    assert pack["stale"] is False


def test_api_intelligence_before_indexing_returns_explicit_state(setup):
    client, _, _ = application(setup)
    assert client.get("/api/intelligence/status").json["indexed"] is False
    assert client.post("/api/intelligence/query", json={"text": "anything"}).json == {
        "stale": False, "candidates": [],
    }
    response = client.post("/api/intelligence/context-pack", json={"text": "anything"})
    assert response.status_code == 409


@pytest.mark.parametrize("field", ["repo_path", "db_path", "root", "state_root", "path"])
def test_api_rejects_arbitrary_path_override(setup, field):
    client, _, _ = application(setup)
    assert client.post(
        "/api/intelligence/query", json={"text": "anything", field: "/etc"},
    ).status_code == 400
    assert client.post(
        "/api/intelligence/context-pack", json={"text": "anything", field: "/etc"},
    ).status_code == 400
    assert client.post(
        "/api/intelligence/refresh", json={field: "/etc"},
    ).status_code == 400


def test_api_intelligence_query_rejects_malformed_input(setup):
    client, _, _ = application(setup)
    assert client.post("/api/intelligence/query", json={}).status_code == 400
    assert client.post("/api/intelligence/query", json={"text": ""}).status_code == 400
    assert client.post(
        "/api/intelligence/query", json={"text": "x", "limit": 0},
    ).status_code == 400
    assert client.post(
        "/api/intelligence/query", json={"text": "x", "limit": "10"},
    ).status_code == 400
    assert client.post(
        "/api/intelligence/query", json={"text": "x", "limit": 10_000},
    ).status_code == 400


def test_api_intelligence_never_executes_or_mutates(setup):
    """Repository intelligence over HTTP is read-only end to end: no
    audit events are produced by it, and the primary repository is
    untouched."""
    runner = LocalWorkerRunner(setup)
    before_audit = runner._control_conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    before_head = git(setup, "rev-parse", "HEAD")
    client, _, _ = application(setup)
    client.post("/api/intelligence/refresh", json={})
    client.post("/api/intelligence/query", json={"text": "anything"})
    client.post("/api/intelligence/context-pack", json={"text": "anything"})
    client.get("/api/intelligence/status")
    assert git(setup, "rev-parse", "HEAD") == before_head
    after_audit = runner._control_conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]
    assert after_audit == before_audit
    runner.close()
