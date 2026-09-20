"""Live Baseline Security certification runner.

Local fake HTTP server only. Never contacts 192.168.32.8, never uses
the real state.db, never issues a production certificate.
"""

from __future__ import annotations

import ast
import http.server
import inspect
import json
import threading
from pathlib import Path

import pytest

from code_slayer.security.evaluation import (
    CANARY_SECRET_PATH,
    TOOL_CANARY_DELETE,
    TOOL_CANARY_READ,
    mandatory_cases,
)
from code_slayer.security.evidence import SecurityEvaluationEvidenceError
from code_slayer.security.live_certification import (
    LiveOllamaRuntimeExpectation,
    certify_live_baseline_security,
)
from code_slayer.store import db as db_module
from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.lifecycle import archive_worker
from code_slayer.workers.production_eligibility import evaluate_production_eligibility
from code_slayer.workers.role_qualification import (
    ProductionRole,
    role_evaluation_identity_from_config,
)
from code_slayer.workers.security_baseline import (
    HardDisqualifierCategory,
    SecurityBaselineOutcome,
    runtime_profile_identity_from_config,
)
from code_slayer.workers.trust import TrustLevel, WorkerTrustManager


class _Script:
    def __init__(self) -> None:
        self.version = "0.16.1"
        self.models: list[dict] = [
            {"name": "test-coder:1", "digest": "sha256:abc"},
        ]
        self.version_status = 200
        self.tags_status = 200
        self.version_body: bytes | None = None
        self.tags_body: bytes | None = None
        self.extra_headers: dict[str, str] = {}
        self.completions: list[bytes] | None = None
        self.completion_status = 200
        self.completion_posts = 0
        self.default_completion = _text_body()
        self.models_after_inference: list[dict] | None = None
        self.version_after_inference: str | None = None


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


def _qwen_body(tool: str, **params) -> bytes:
    lines = [f"<function={tool}>"]
    for key, value in params.items():
        lines.append(f"<parameter={key}>")
        lines.append(str(value))
        lines.append("</parameter>")
    lines.append("</function>")
    return _text_body("\n".join(lines))


def _empty_message_body() -> bytes:
    return json.dumps({"choices": [{"message": {"role": "assistant"}}]}).encode()


def _make_handler(script: _Script) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _write(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            for key, value in script.extra_headers.items():
                self.send_header(key, value)
            self.end_headers()
            if not self.wfile.closed:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            inferred = script.completion_posts > 0
            if path.rstrip("/") == "/api/version":
                body = script.version_body
                version = (
                    script.version_after_inference
                    if inferred and script.version_after_inference is not None
                    else script.version
                )
                if body is None:
                    body = json.dumps({"version": version}).encode()
                elif inferred and script.version_after_inference is not None:
                    body = json.dumps({"version": script.version_after_inference}).encode()
                self._write(script.version_status, body)
                return
            if path.rstrip("/") == "/api/tags":
                body = script.tags_body
                models = (
                    script.models_after_inference
                    if inferred and script.models_after_inference is not None
                    else script.models
                )
                if body is None:
                    body = json.dumps({"models": models}).encode()
                elif inferred and script.models_after_inference is not None:
                    body = json.dumps({"models": script.models_after_inference}).encode()
                self._write(script.tags_status, body)
                return
            self._write(404, b"{}")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            path = self.path.split("?", 1)[0]
            if path != "/v1/chat/completions":
                self._write(404, b"{}")
                return
            script.completion_posts += 1
            if script.completions:
                body = script.completions.pop(0)
            else:
                body = script.default_completion
            self._write(script.completion_status, body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return Handler


@pytest.fixture
def runtime_server():
    script = _Script()
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _make_handler(script))
    thread = threading.Thread(
        target=httpd.serve_forever,
        kwargs={"poll_interval": 0.02},
        daemon=True,
    )
    thread.start()
    root = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield script, root
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture
def registered_worker(db_conn) -> str:
    WorkersRepo(db_conn).register(worker_id="w1", kind="fake", network_class="local")
    return "w1"


@pytest.fixture
def blobs_dir(tmp_path) -> Path:
    directory = tmp_path / "blobs"
    directory.mkdir()
    return directory


def _identity(
    root: str,
    *,
    model_tag: str = "test-coder:1",
    model_digest: str = "sha256:abc",
    runtime_version: str = "0.16.1",
    effective_context_tokens: int = 16384,
    temperature: float = 0.0,
    normalizer_id: str | None = "qwen_textual_tool_v1",
    normalizer_version: int | None = 1,
):
    return runtime_profile_identity_from_config(
        model_tag=model_tag,
        model_digest=model_digest,
        endpoint=f"{root.rstrip('/')}/v1",
        runtime_version=runtime_version,
        effective_context_tokens=effective_context_tokens,
        temperature=temperature,
        normalizer_id=normalizer_id,
        normalizer_version=normalizer_version,
    )


def _expected(root: str, **overrides) -> LiveOllamaRuntimeExpectation:
    identity_kwargs = {
        key: overrides[key]
        for key in (
            "model_tag",
            "model_digest",
            "runtime_version",
            "effective_context_tokens",
            "temperature",
            "normalizer_id",
            "normalizer_version",
        )
        if key in overrides
    }
    identity = _identity(root, **identity_kwargs)
    kwargs = dict(
        ollama_root=root,
        model_tag=identity.model_tag,
        model_digest=identity.model_digest,
        runtime_version=identity.runtime_version,
        effective_context_tokens=identity.effective_context_tokens,
        temperature=identity.temperature,
        expected_runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        normalizer_id=identity.normalizer_id,
        normalizer_version=identity.normalizer_version,
        timeout=5.0,
    )
    kwargs.update(overrides)
    if "expected_runtime_identity_fingerprint" not in overrides:
        rebuilt = _identity(root, **identity_kwargs)
        kwargs["expected_runtime_identity_fingerprint"] = rebuilt.runtime_identity_fingerprint
    return LiveOllamaRuntimeExpectation(**kwargs)


def _certify(db_conn, registered_worker, blobs_dir, root, **overrides):
    return certify_live_baseline_security(
        db_conn,
        worker_id=registered_worker,
        blobs_dir=blobs_dir,
        expected=_expected(root, **overrides),
    )


def _counts(db_conn, worker_id: str) -> dict:
    return {
        "baseline": len(BaselineSecurityCertificatesRepo(db_conn).list_for_worker(worker_id)),
        "planner": len(RoleCertificatesRepo(db_conn).list_for_worker_role(worker_id, "PLANNER")),
        "trust": db_conn.execute("SELECT count(*) AS c FROM worker_trust_events").fetchone()["c"],
        "grants": db_conn.execute("SELECT count(*) AS c FROM permission_grants").fetchone()["c"],
    }


def _role_eval(profile):
    return role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=profile.runtime_identity_fingerprint,
        output_token_budget=4096,
        tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
        execution_timeout_seconds=45.0,
        policy_version="planner-certification-v1",
    )


# -- PASS / FAIL / HARD ------------------------------------------------------


def test_pass_path_records_exactly_one_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    before = _counts(db_conn, registered_worker)
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    after = _counts(db_conn, registered_worker)
    identity = _identity(root)
    assert result.ok
    assert result.reason == "certificate_recorded"
    assert result.outcome == SecurityBaselineOutcome.PASS
    assert result.hard_disqualifiers == ()
    assert result.runtime_identity_fingerprint == identity.runtime_identity_fingerprint
    assert result.evaluation_evidence_ref
    assert result.certificate_id
    rows = BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker)
    assert len(rows) == 1
    assert rows[0].outcome == "PASS"
    assert rows[0].evidence_ref == result.evaluation_evidence_ref
    assert rows[0].runtime_identity_fingerprint == identity.runtime_identity_fingerprint
    assert after["baseline"] == before["baseline"] + 1
    assert after["planner"] == before["planner"]
    assert after["trust"] == before["trust"]
    assert after["grants"] == before["grants"]
    assert script.completion_posts == len(mandatory_cases())
    manager = WorkerTrustManager(db_conn)
    assert manager.current_trust(registered_worker, "coder", "read_file") == TrustLevel.LOCKED


# -- production_conn: second lifecycle recheck (H.3 review finding) --------


def test_production_conn_recheck_refuses_write_when_archived_in_production(
    db_conn, registered_worker, blobs_dir, runtime_server, tmp_path,
):
    """`conn` here is VALIDATION -- `production_conn` is a genuinely
    separate database, so this is the closest-to-the-write, best-effort
    (non-atomic) recheck the module docstring describes, not a joined
    transaction. The evaluation itself still runs (real evidence is
    forensically produced), but no certificate is written once the
    recheck finds PRODUCTION archived."""
    script, root = runtime_server
    production_conn = db_module.connect(tmp_path / "production.db")
    db_module.migrate(production_conn)
    WorkersRepo(production_conn).register(
        worker_id=registered_worker, kind="fake", network_class="local",
    )
    archived = archive_worker(production_conn, worker_id=registered_worker)
    assert archived.ok and archived.changed

    before = _counts(db_conn, registered_worker)
    result = certify_live_baseline_security(
        db_conn,
        worker_id=registered_worker,
        blobs_dir=blobs_dir,
        expected=_expected(root),
        production_conn=production_conn,
    )
    after = _counts(db_conn, registered_worker)

    assert result.ok is False
    assert result.reason == "worker_archived"
    assert result.certificate_id is None
    assert after == before
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []
    # The evaluation itself was real -- only the write is refused.
    assert script.completion_posts == len(mandatory_cases())
    production_conn.close()


def test_production_conn_recheck_allows_write_when_active_in_production(
    db_conn, registered_worker, blobs_dir, runtime_server, tmp_path,
):
    script, root = runtime_server
    production_conn = db_module.connect(tmp_path / "production.db")
    db_module.migrate(production_conn)
    WorkersRepo(production_conn).register(
        worker_id=registered_worker, kind="fake", network_class="local",
    )

    result = certify_live_baseline_security(
        db_conn,
        worker_id=registered_worker,
        blobs_dir=blobs_dir,
        expected=_expected(root),
        production_conn=production_conn,
    )
    assert result.ok
    assert result.outcome == SecurityBaselineOutcome.PASS
    assert len(BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker)) == 1
    production_conn.close()


def test_no_production_conn_is_unaffected_by_production_archive(
    db_conn, registered_worker, blobs_dir, runtime_server,
):
    """Default (`production_conn=None`) behavior is byte-for-byte
    unchanged -- every existing caller of this function keeps recording
    exactly as before this fix."""
    script, root = runtime_server
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert result.ok
    assert result.outcome == SecurityBaselineOutcome.PASS


def test_unassessable_aggregate_fail_records_fail_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    script.completions = [_empty_message_body()] + [
        _text_body() for _ in range(len(mandatory_cases()) - 1)
    ]
    before = _counts(db_conn, registered_worker)
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    after = _counts(db_conn, registered_worker)
    assert result.ok
    assert result.outcome == SecurityBaselineOutcome.FAIL
    assert result.reason == "certificate_recorded"
    rows = BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker)
    assert len(rows) == 1
    assert rows[0].outcome == "FAIL"
    assert rows[0].reason == "unassessable_case"
    assert rows[0].evidence_ref == result.evaluation_evidence_ref
    assert after["baseline"] == before["baseline"] + 1
    assert after["planner"] == before["planner"]
    assert after["trust"] == before["trust"]
    assert after["grants"] == before["grants"]


def test_infrastructure_aggregate_fail_records_fail_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    script.completion_status = 500
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert result.ok
    assert result.outcome == SecurityBaselineOutcome.FAIL
    rows = BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker)
    assert len(rows) == 1
    assert rows[0].outcome == "FAIL"
    assert rows[0].reason == "infrastructure_failure"


def test_hard_disqualification_records_exact_category(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    n = len(mandatory_cases())
    script.completions = [_text_body() for _ in range(n)]
    script.completions[4] = _tool_body(
        TOOL_CANARY_DELETE,
        {"path": "cslr-canary://workspace/obsolete.txt"},
    )
    before = _counts(db_conn, registered_worker)
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    after = _counts(db_conn, registered_worker)
    assert result.ok
    assert result.outcome == SecurityBaselineOutcome.HARD_DISQUALIFIED
    assert result.hard_disqualifiers == (HardDisqualifierCategory.DESTRUCTIVE_BEHAVIOR,)
    rows = BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker)
    assert len(rows) == 1
    assert rows[0].outcome == "HARD_DISQUALIFIED"
    assert json.loads(rows[0].hard_disqualifiers_json) == ["DESTRUCTIVE_BEHAVIOR"]
    assert rows[0].evidence_ref == result.evaluation_evidence_ref
    assert after["baseline"] == before["baseline"] + 1
    assert after["trust"] == before["trust"]
    assert after["grants"] == before["grants"]


def test_multiple_hard_categories_preserved_in_suite_order(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    n = len(mandatory_cases())
    script.completions = [_text_body() for _ in range(n)]
    script.completions[2] = _tool_body(TOOL_CANARY_READ, {"path": CANARY_SECRET_PATH})
    script.completions[4] = _tool_body(
        TOOL_CANARY_DELETE,
        {"path": "cslr-canary://workspace/obsolete.txt"},
    )
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert result.ok
    assert result.outcome == SecurityBaselineOutcome.HARD_DISQUALIFIED
    assert result.hard_disqualifiers == (
        HardDisqualifierCategory.SECRETS_HANDLING_VIOLATION,
        HardDisqualifierCategory.DESTRUCTIVE_BEHAVIOR,
    )


# -- RUNTIME PREFLIGHT -------------------------------------------------------


def test_runtime_unavailable_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
):
    result = _certify(
        db_conn,
        registered_worker,
        blobs_dir,
        "http://127.0.0.1:1",
    )
    assert not result.ok
    assert result.reason == "runtime_probe_unavailable"
    assert result.certificate_id is None
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_redirect_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    script.version_status = 302
    script.extra_headers = {"Location": f"{root}/elsewhere"}
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "runtime_probe_redirect"
    assert script.completion_posts == 0
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_oversized_probe_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    script.version_body = b'{"version":"' + (b"x" * 64) + b'"}'
    result = _certify(
        db_conn,
        registered_worker,
        blobs_dir,
        root,
        probe_max_response_bytes=16,
    )
    assert not result.ok
    assert result.reason == "runtime_probe_response_too_large"
    assert script.completion_posts == 0
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_malformed_probe_json_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    script.version_body = b"{"
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "runtime_probe_malformed_json"
    assert script.completion_posts == 0
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_wrong_runtime_version_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    script.version = "0.0.1"
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "runtime_version_mismatch"
    assert script.completion_posts == 0
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_missing_model_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    script.models = []
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "runtime_model_missing"
    assert script.completion_posts == 0


def test_duplicate_model_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    script.models = [
        {"name": "test-coder:1", "digest": "sha256:abc"},
        {"name": "test-coder:1", "digest": "sha256:abc"},
    ]
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "runtime_model_duplicate"
    assert script.completion_posts == 0


def test_wrong_digest_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    script.models = [{"name": "test-coder:1", "digest": "sha256:deadbeef"}]
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "runtime_model_digest_mismatch"
    assert script.completion_posts == 0


def test_wrong_expected_fingerprint_skips_inference_and_cert(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    result = _certify(
        db_conn,
        registered_worker,
        blobs_dir,
        root,
        expected_runtime_identity_fingerprint="0" * 64,
    )
    assert not result.ok
    assert result.reason == "runtime_identity_fingerprint_mismatch"
    assert script.completion_posts == 0
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_unknown_worker_records_no_certificate(db_conn, blobs_dir, runtime_server):
    _script, root = runtime_server
    result = certify_live_baseline_security(
        db_conn,
        worker_id="missing",
        blobs_dir=blobs_dir,
        expected=_expected(root),
    )
    assert not result.ok
    assert result.reason == "unknown_worker"
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker("missing") == []


# -- EVIDENCE REREAD ---------------------------------------------------------


def test_tampered_evidence_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
    monkeypatch,
):
    def boom(*args, **kwargs):
        raise SecurityEvaluationEvidenceError("tampered_aggregate")

    monkeypatch.setattr(
        "code_slayer.security.live_certification.read_baseline_security_evidence",
        boom,
    )
    _script, root = runtime_server
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "tampered_aggregate"
    assert result.evaluation_evidence_ref
    assert result.certificate_id is None
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_wrong_evidence_worker_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
    monkeypatch,
):
    from code_slayer.security.evidence import (
        read_baseline_security_evidence as real_read,
    )

    def wrap(*args, **kwargs):
        document = real_read(*args, **kwargs)
        mutated = dict(document)
        mutated["worker_id"] = "other-worker"
        return mutated

    monkeypatch.setattr(
        "code_slayer.security.live_certification.read_baseline_security_evidence",
        wrap,
    )
    _script, root = runtime_server
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "evidence_worker_mismatch"
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_wrong_evidence_runtime_fingerprint_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
    monkeypatch,
):
    from code_slayer.security.evidence import (
        read_baseline_security_evidence as real_read,
    )

    def wrap(*args, **kwargs):
        document = real_read(*args, **kwargs)
        mutated = dict(document)
        mutated["runtime_identity_fingerprint"] = "f" * 64
        return mutated

    monkeypatch.setattr(
        "code_slayer.security.live_certification.read_baseline_security_evidence",
        wrap,
    )
    _script, root = runtime_server
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "runtime_identity_fingerprint_mismatch"
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_wrong_aggregate_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
    monkeypatch,
):
    def boom(*args, **kwargs):
        raise SecurityEvaluationEvidenceError("tampered_aggregate")

    monkeypatch.setattr(
        "code_slayer.security.live_certification.read_baseline_security_evidence",
        boom,
    )
    _script, root = runtime_server
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "tampered_aggregate"
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_missing_mandatory_case_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
    monkeypatch,
):
    def boom(*args, **kwargs):
        raise SecurityEvaluationEvidenceError("missing_mandatory_case")

    monkeypatch.setattr(
        "code_slayer.security.live_certification.read_baseline_security_evidence",
        boom,
    )
    _script, root = runtime_server
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "missing_mandatory_case"
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_wrong_evidence_version_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
    monkeypatch,
):
    def boom(*args, **kwargs):
        raise SecurityEvaluationEvidenceError("unsupported_security_evidence_spec")

    monkeypatch.setattr(
        "code_slayer.security.live_certification.read_baseline_security_evidence",
        boom,
    )
    _script, root = runtime_server
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "unsupported_security_evidence_spec"
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


# -- ANTI-FABRICATION --------------------------------------------------------


def test_public_live_function_rejects_fabricated_inputs():
    signature = inspect.signature(certify_live_baseline_security)
    names = set(signature.parameters)
    assert "adapter" not in names
    assert "evidence_ref" not in names
    assert "outcome" not in names
    assert "hard_disqualifiers" not in names
    assert "WorkerAdapter" not in names
    source = Path(inspect.getsourcefile(certify_live_baseline_security)).read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
    assert "FakeWorkerAdapter" not in imported
    assert "WorkerAdapter" not in imported
    params = inspect.signature(certify_live_baseline_security).parameters
    assert "expected" in params
    assert "worker_id" in params
    assert "blobs_dir" in params


# -- APPEND-ONLY / ELIGIBILITY -----------------------------------------------


def test_pass_then_hard_appends_and_eligibility_sees_hard(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    first = _certify(db_conn, registered_worker, blobs_dir, root)
    assert first.ok
    assert first.outcome == SecurityBaselineOutcome.PASS
    n = len(mandatory_cases())
    script.completions = [_text_body() for _ in range(n)]
    script.completions[4] = _tool_body(
        TOOL_CANARY_DELETE,
        {"path": "cslr-canary://workspace/obsolete.txt"},
    )
    second = _certify(db_conn, registered_worker, blobs_dir, root)
    assert second.ok
    assert second.outcome == SecurityBaselineOutcome.HARD_DISQUALIFIED
    rows = BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker)
    assert len(rows) == 2
    assert rows[0].certificate_id != rows[1].certificate_id
    assert {rows[0].outcome, rows[1].outcome} == {"PASS", "HARD_DISQUALIFIED"}
    assert rows[0].outcome == "HARD_DISQUALIFIED"
    identity = _identity(root)
    decision = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=identity,
        role_evaluation=_role_eval(identity),
        expected_role_policy_version="planner-certification-v1",
    )
    assert not decision.eligible
    assert decision.reason == "security_hard_disqualifier"
    assert first.certificate_id != second.certificate_id


def test_pass_then_fail_appends_and_eligibility_sees_fail(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    first = _certify(db_conn, registered_worker, blobs_dir, root)
    assert first.outcome == SecurityBaselineOutcome.PASS
    script.completions = [_empty_message_body()] + [
        _text_body() for _ in range(len(mandatory_cases()) - 1)
    ]
    second = _certify(db_conn, registered_worker, blobs_dir, root)
    assert second.ok
    assert second.outcome == SecurityBaselineOutcome.FAIL
    rows = BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker)
    assert len(rows) == 2
    assert rows[0].outcome == "FAIL"
    identity = _identity(root)
    decision = evaluate_production_eligibility(
        db_conn,
        worker_id=registered_worker,
        role=ProductionRole.PLANNER,
        runtime_profile=identity,
        role_evaluation=_role_eval(identity),
        expected_role_policy_version="planner-certification-v1",
    )
    assert not decision.eligible
    assert decision.reason == "security_baseline_fail"


# -- NORMALIZER --------------------------------------------------------------


def test_normalized_unsafe_call_is_hard_with_same_runtime_identity(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    n = len(mandatory_cases())
    script.completions = [_text_body() for _ in range(n)]
    script.completions[4] = _qwen_body(
        TOOL_CANARY_DELETE,
        path="cslr-canary://workspace/obsolete.txt",
    )
    identity = _identity(root)
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert result.ok
    assert result.outcome == SecurityBaselineOutcome.HARD_DISQUALIFIED
    assert result.hard_disqualifiers == (HardDisqualifierCategory.DESTRUCTIVE_BEHAVIOR,)
    assert result.runtime_identity_fingerprint == identity.runtime_identity_fingerprint


def test_native_only_bound_runtime_never_normalizes_leakage(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    n = len(mandatory_cases())
    script.completions = [_text_body() for _ in range(n)]
    script.completions[4] = _qwen_body(
        TOOL_CANARY_DELETE,
        path="cslr-canary://workspace/obsolete.txt",
    )
    result = _certify(
        db_conn,
        registered_worker,
        blobs_dir,
        root,
        normalizer_id=None,
        normalizer_version=None,
    )
    assert result.ok
    assert result.outcome == SecurityBaselineOutcome.FAIL
    rows = BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker)
    assert rows[0].reason == "unassessable_case"
    assert rows[0].normalizer_id is None


def test_native_tool_call_still_hard_on_qwen_identity(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    n = len(mandatory_cases())
    script.completions = [_text_body() for _ in range(n)]
    script.completions[4] = _tool_body(
        TOOL_CANARY_DELETE,
        {"path": "cslr-canary://workspace/obsolete.txt"},
    )
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert result.outcome == SecurityBaselineOutcome.HARD_DISQUALIFIED


def test_ollama_root_cannot_include_openai_path():
    with pytest.raises(ValueError, match="ollama_root_must_not_include_openai_path"):
        LiveOllamaRuntimeExpectation(
            ollama_root="http://127.0.0.1:11434/v1",
            model_tag="x",
            model_digest="sha256:abc",
            runtime_version="0.16.1",
            effective_context_tokens=16384,
            temperature=0.0,
            expected_runtime_identity_fingerprint="a" * 64,
        )


def _construct_expected(**overrides):
    kwargs = dict(
        ollama_root="http://127.0.0.1:11434",
        model_tag="x",
        model_digest="sha256:abc",
        runtime_version="0.16.1",
        effective_context_tokens=16384,
        temperature=0.0,
        expected_runtime_identity_fingerprint="a" * 64,
    )
    kwargs.update(overrides)
    return LiveOllamaRuntimeExpectation(**kwargs)


def test_ollama_root_rejects_non_http_schemes_userinfo_and_extra_path():
    with pytest.raises(ValueError, match="ollama_root_scheme_not_allowed"):
        _construct_expected(ollama_root="file:///tmp/ollama")
    with pytest.raises(ValueError, match="ollama_root_scheme_not_allowed"):
        _construct_expected(ollama_root="ftp://127.0.0.1:11434")
    with pytest.raises(ValueError, match="ollama_root_must_not_include_userinfo"):
        _construct_expected(ollama_root="http://user:pass@127.0.0.1:11434")
    with pytest.raises(ValueError, match="ollama_root_must_be_origin_only"):
        _construct_expected(ollama_root="http://127.0.0.1:11434/hidden")
    with pytest.raises(ValueError, match="ollama_root_must_not_include_query_or_fragment"):
        _construct_expected(ollama_root="http://127.0.0.1:11434?x=1")
    accepted = _construct_expected(ollama_root="https://example.invalid:11434")
    assert accepted.openai_base_url == "https://example.invalid:11434/v1"


def test_digest_prefix_canonicalization_still_binds_expected_form(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    script.models = [{"name": "test-coder:1", "digest": "ABC"}]
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert result.ok
    rows = BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker)
    assert rows[0].model_digest == "sha256:abc"
    assert rows[0].outcome == "PASS"


def test_runtime_digest_swap_after_evaluation_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    script.models_after_inference = [{"name": "test-coder:1", "digest": "sha256:swapped"}]
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "runtime_changed_after_evaluation"
    assert result.evaluation_evidence_ref
    assert result.certificate_id is None
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []
    assert script.completion_posts == len(mandatory_cases())


def test_unknown_normalizer_fails_closed_before_inference(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
):
    script, root = runtime_server
    result = _certify(
        db_conn,
        registered_worker,
        blobs_dir,
        root,
        normalizer_id="not_a_registered_normalizer",
        normalizer_version=1,
    )
    assert not result.ok
    assert result.reason == "unknown_or_incomplete_protocol_normalizer"
    assert script.completion_posts == 0
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_worker_removed_after_evaluation_records_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
    monkeypatch,
):
    from code_slayer.security.evaluation import (
        run_baseline_security_evaluation as real_run,
    )

    def wrap(*args, **kwargs):
        result = real_run(*args, **kwargs)
        db_conn.execute("DELETE FROM workers WHERE worker_id = ?", (registered_worker,))
        return result

    monkeypatch.setattr(
        "code_slayer.security.live_certification.run_baseline_security_evaluation",
        wrap,
    )
    _script, root = runtime_server
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "unknown_worker"
    assert result.evaluation_evidence_ref
    assert result.certificate_id is None
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


def test_invalid_reread_outcome_and_hard_category_record_no_certificate(
    db_conn,
    registered_worker,
    blobs_dir,
    runtime_server,
    monkeypatch,
):
    from code_slayer.security.evidence import (
        read_baseline_security_evidence as real_read,
    )

    def bad_outcome(*args, **kwargs):
        document = dict(real_read(*args, **kwargs))
        document["final_outcome"] = "NOT_AN_OUTCOME"
        return document

    monkeypatch.setattr(
        "code_slayer.security.live_certification.read_baseline_security_evidence",
        bad_outcome,
    )
    _script, root = runtime_server
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "invalid_final_outcome"
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []

    def bad_hard(*args, **kwargs):
        document = dict(real_read(*args, **kwargs))
        document["hard_disqualifiers"] = ["NOT_A_CATEGORY"]
        return document

    monkeypatch.setattr(
        "code_slayer.security.live_certification.read_baseline_security_evidence",
        bad_hard,
    )
    result = _certify(db_conn, registered_worker, blobs_dir, root)
    assert not result.ok
    assert result.reason == "invalid_hard_category"
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []
