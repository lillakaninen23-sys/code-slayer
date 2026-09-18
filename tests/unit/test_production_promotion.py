"""H.1: VALIDATION -> PRODUCTION Baseline Security promotion.

Local fake HTTP server only. Never contacts a real Ollama instance,
never uses the real state.db, never grants trust/permission/role
certificates.
"""

from __future__ import annotations

import http.server
import json
import threading
from pathlib import Path

import pytest

import code_slayer.security.production_promotion as production_promotion
from code_slayer.security.evaluation import TOOL_CANARY_DELETE, mandatory_cases
from code_slayer.security.live_certification import (
    LiveOllamaRuntimeExpectation,
    certify_live_baseline_security,
)
from code_slayer.security.production_promotion import (
    describe_promotion_availability,
    promote_baseline_security_to_production,
)
from code_slayer.store import db as db_module
from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.content_store import ContentStore
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.security_baseline import (
    SecurityBaselineOutcome,
    runtime_profile_identity_from_config,
)


class _Script:
    def __init__(self) -> None:
        self.version = "0.16.1"
        self.models: list[dict] = [{"name": "test-coder:1", "digest": "sha256:abc"}]
        self.completions: list[bytes] | None = None
        self.completion_status = 200
        self.completion_posts = 0
        self.default_completion = _text_body()


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


def _make_handler(script: _Script) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _write(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if not self.wfile.closed:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

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
        target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True,
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
def validation_conn(db_conn):
    return db_conn


@pytest.fixture
def production_conn(tmp_path):
    conn = db_module.connect(tmp_path / "production.db")
    db_module.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def registered_worker(validation_conn, production_conn) -> str:
    WorkersRepo(validation_conn).register(worker_id="w1", kind="fake", network_class="local")
    WorkersRepo(production_conn).register(worker_id="w1", kind="fake", network_class="local")
    return "w1"


@pytest.fixture
def validation_blobs_dir(tmp_path) -> Path:
    directory = tmp_path / "validation-blobs"
    directory.mkdir()
    return directory


@pytest.fixture
def production_blobs_dir(tmp_path) -> Path:
    directory = tmp_path / "production-blobs"
    directory.mkdir()
    return directory


def _identity(root: str, **overrides):
    kwargs = dict(
        model_tag="test-coder:1",
        model_digest="sha256:abc",
        endpoint=f"{root.rstrip('/')}/v1",
        runtime_version="0.16.1",
        effective_context_tokens=16384,
        temperature=0.0,
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
    )
    kwargs.update(overrides)
    return runtime_profile_identity_from_config(**kwargs)


def _expected(root: str, **overrides) -> LiveOllamaRuntimeExpectation:
    identity_kwargs = {
        key: overrides.pop(key)
        for key in (
            "model_tag", "model_digest", "runtime_version", "effective_context_tokens",
            "temperature", "normalizer_id", "normalizer_version",
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
    return LiveOllamaRuntimeExpectation(**kwargs)


def _production_rows(production_conn, worker_id):
    return BaselineSecurityCertificatesRepo(production_conn).list_for_worker(worker_id)


def _certify_validation_pass(validation_conn, registered_worker, validation_blobs_dir, root):
    result = certify_live_baseline_security(
        validation_conn,
        worker_id=registered_worker,
        blobs_dir=validation_blobs_dir,
        expected=_expected(root),
    )
    assert result.ok
    assert result.outcome == SecurityBaselineOutcome.PASS
    return result


def _promote(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, root, **overrides,
):
    return promote_baseline_security_to_production(
        validation_conn,
        production_conn,
        worker_id=registered_worker,
        validation_blobs_dir=validation_blobs_dir,
        production_blobs_dir=production_blobs_dir,
        expected=_expected(root, **overrides),
    )


# -- happy path ---------------------------------------------------------


def test_promotion_records_exactly_one_production_certificate(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    script, root = runtime_server
    validation_result = _certify_validation_pass(
        validation_conn, registered_worker, validation_blobs_dir, root,
    )
    before_production = BaselineSecurityCertificatesRepo(production_conn).list_for_worker(
        registered_worker,
    )
    assert before_production == []

    result = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )

    assert result.ok, result.reason
    assert result.validation_certificate_id
    assert result.production_certificate_id
    assert result.evidence_ref == validation_result.evaluation_evidence_ref

    production_rows = BaselineSecurityCertificatesRepo(production_conn).list_for_worker(
        registered_worker,
    )
    assert len(production_rows) == 1
    assert production_rows[0].outcome == "PASS"
    assert production_rows[0].evidence_ref == validation_result.evaluation_evidence_ref

    # Evidence is independently re-readable from PRODUCTION alone.
    production_store = ContentStore(production_conn, production_blobs_dir)
    assert production_store.get_meta(result.evidence_ref) is not None

    # Promotion never touches trust, permissions, or role certificates.
    for table in ("worker_trust_events", "permission_grants", "worker_role_certificates"):
        count = production_conn.execute(f"SELECT count(*) AS c FROM {table}").fetchone()["c"]
        assert count == 0

    audit_rows = production_conn.execute(
        "SELECT event_type, payload_json FROM audit_events "
        "WHERE event_type = 'SECURITY_BASELINE_CERTIFICATE_PROMOTED_TO_PRODUCTION'",
    ).fetchall()
    assert len(audit_rows) == 1
    payload = json.loads(audit_rows[0]["payload_json"])
    assert payload["validation_certificate_id"] == result.validation_certificate_id
    assert payload["production_certificate_id"] == result.production_certificate_id


def test_first_promotion_creates_exactly_one_production_certificate(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    script, root = runtime_server
    _certify_validation_pass(validation_conn, registered_worker, validation_blobs_dir, root)
    result = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )
    assert result.ok
    assert result.reason == "promoted_to_production"
    assert len(_production_rows(production_conn, registered_worker)) == 1


def test_repeated_identical_promotion_does_not_create_a_second_certificate(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    """Same VALIDATION certificate, same server-verified runtime identity:
    a second promotion must return the SAME PRODUCTION certificate as an
    idempotent success, never mint a second, independent authority row."""
    script, root = runtime_server
    _certify_validation_pass(validation_conn, registered_worker, validation_blobs_dir, root)
    first = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )
    second = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )
    third = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )
    assert first.ok and second.ok and third.ok
    assert first.reason == "promoted_to_production"
    assert second.reason == "already_promoted_to_production"
    assert third.reason == "already_promoted_to_production"
    assert first.production_certificate_id == second.production_certificate_id
    assert first.production_certificate_id == third.production_certificate_id
    rows = _production_rows(production_conn, registered_worker)
    assert len(rows) == 1
    assert rows[0].certificate_id == first.production_certificate_id


def test_genuinely_different_subsequent_identity_is_not_deduplicated(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    """A second, later VALIDATION run against the SAME runtime identity
    produces a DIFFERENT evidence_ref (a different evaluation, even if
    the outcome is again PASS) and must promote as its own, separate
    PRODUCTION certificate -- never silently folded into the earlier
    promotion merely because the runtime identity matches."""
    script, root = runtime_server
    first_validation = _certify_validation_pass(
        validation_conn, registered_worker, validation_blobs_dir, root,
    )
    first = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )
    assert first.ok
    assert first.evidence_ref == first_validation.evaluation_evidence_ref

    second_validation = _certify_validation_pass(
        validation_conn, registered_worker, validation_blobs_dir, root,
    )
    assert second_validation.evaluation_evidence_ref != first_validation.evaluation_evidence_ref

    second = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )
    assert second.ok
    assert second.reason == "promoted_to_production"
    assert second.evidence_ref == second_validation.evaluation_evidence_ref
    assert second.production_certificate_id != first.production_certificate_id

    rows = _production_rows(production_conn, registered_worker)
    assert len(rows) == 2
    assert {row.certificate_id for row in rows} == {
        first.production_certificate_id, second.production_certificate_id,
    }
    assert {row.evidence_ref for row in rows} == {
        first_validation.evaluation_evidence_ref, second_validation.evaluation_evidence_ref,
    }


# -- fail closed ----------------------------------------------------------


def test_denies_when_no_validation_certificate_exists(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    script, root = runtime_server
    result = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )
    assert not result.ok
    assert result.reason == "no_validation_certificate"
    assert _production_rows(production_conn, registered_worker) == []


def test_denies_unknown_worker(
    validation_conn, production_conn, validation_blobs_dir, production_blobs_dir, runtime_server,
):
    script, root = runtime_server
    result = promote_baseline_security_to_production(
        validation_conn,
        production_conn,
        worker_id="ghost",
        validation_blobs_dir=validation_blobs_dir,
        production_blobs_dir=production_blobs_dir,
        expected=_expected(root),
    )
    assert not result.ok
    assert result.reason == "unknown_worker"


def test_denies_non_pass_validation_certificate(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    script, root = runtime_server
    script.completions = [_text_body() for _ in range(len(mandatory_cases()))]
    script.completions[0] = _tool_body(TOOL_CANARY_DELETE, {"path": "cslr-canary://workspace/obsolete.txt"})
    validation_result = certify_live_baseline_security(
        validation_conn,
        worker_id=registered_worker,
        blobs_dir=validation_blobs_dir,
        expected=_expected(root),
    )
    assert validation_result.outcome == SecurityBaselineOutcome.HARD_DISQUALIFIED

    result = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )
    assert not result.ok
    assert result.reason == "validation_certificate_not_pass"
    assert _production_rows(production_conn, registered_worker) == []


def test_denies_changed_model_digest_since_validation(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    script, root = runtime_server
    _certify_validation_pass(validation_conn, registered_worker, validation_blobs_dir, root)
    script.models = [{"name": "test-coder:1", "digest": "sha256:changed"}]

    result = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )
    assert not result.ok
    assert result.reason == "runtime_model_digest_mismatch"
    assert _production_rows(production_conn, registered_worker) == []


def test_denies_changed_runtime_version_since_validation(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    script, root = runtime_server
    _certify_validation_pass(validation_conn, registered_worker, validation_blobs_dir, root)
    script.version = "0.99.0"

    result = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )
    assert not result.ok
    assert result.reason == "runtime_version_mismatch"
    assert _production_rows(production_conn, registered_worker) == []


def test_denies_tampered_evidence(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    script, root = runtime_server
    validation_result = _certify_validation_pass(
        validation_conn, registered_worker, validation_blobs_dir, root,
    )
    blob_path = validation_blobs_dir / validation_result.evaluation_evidence_ref[:2] / (
        validation_result.evaluation_evidence_ref
    )
    blob_path.chmod(0o644)
    blob_path.write_bytes(b'{"tampered": true}')

    result = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )
    assert not result.ok
    assert result.reason == "durable_security_evidence_hash_mismatch"
    assert _production_rows(production_conn, registered_worker) == []


def test_denies_stale_runtime_identity_fingerprint(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    """A caller-supplied expectation whose own claimed fingerprint does
    not match what its own fields recompute to is refused before any
    certificate is even looked up."""
    script, root = runtime_server
    _certify_validation_pass(validation_conn, registered_worker, validation_blobs_dir, root)
    real = _expected(root)
    forged = LiveOllamaRuntimeExpectation(
        ollama_root=real.ollama_root,
        model_tag=real.model_tag,
        model_digest=real.model_digest,
        runtime_version=real.runtime_version,
        effective_context_tokens=real.effective_context_tokens,
        temperature=real.temperature,
        expected_runtime_identity_fingerprint="0" * 64,
        normalizer_id=real.normalizer_id,
        normalizer_version=real.normalizer_version,
        timeout=5.0,
    )
    result = promote_baseline_security_to_production(
        validation_conn,
        production_conn,
        worker_id=registered_worker,
        validation_blobs_dir=validation_blobs_dir,
        production_blobs_dir=production_blobs_dir,
        expected=forged,
    )
    assert not result.ok
    assert result.reason == "runtime_identity_fingerprint_mismatch"
    assert _production_rows(production_conn, registered_worker) == []


# -- backend-authoritative promotion_available projection -------------------


def _availability(
    validation_conn, production_conn, registered_worker, root, **overrides,
):
    return describe_promotion_availability(
        validation_conn,
        production_conn,
        worker_id=registered_worker,
        expected=_expected(root, **overrides),
    )


def test_availability_denies_before_any_validation_certificate_exists(
    validation_conn, production_conn, registered_worker, runtime_server,
):
    script, root = runtime_server
    result = _availability(validation_conn, production_conn, registered_worker, root)
    assert result.available is False
    assert result.reason == "no_validation_certificate"


def test_availability_is_true_only_after_a_matching_pass_certificate(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    script, root = runtime_server
    validation_result = _certify_validation_pass(
        validation_conn, registered_worker, validation_blobs_dir, root,
    )
    result = _availability(validation_conn, production_conn, registered_worker, root)
    assert result.available is True
    assert result.reason == "promotable"
    assert result.validation_certificate_id == validation_result.certificate_id


def test_availability_never_probes_the_live_runtime(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server, monkeypatch,
):
    """A GET-style availability check must never itself contact Ollama --
    it is a durable-state-only projection, exactly like
    `ready_for_certification`. Patching `verify_ollama_runtime` to raise
    proves the code path never calls it."""
    script, root = runtime_server
    _certify_validation_pass(validation_conn, registered_worker, validation_blobs_dir, root)

    def _boom(*args, **kwargs):
        raise AssertionError("describe_promotion_availability must never probe live Ollama")

    monkeypatch.setattr(
        "code_slayer.security.production_promotion.verify_ollama_runtime", _boom,
    )
    result = _availability(validation_conn, production_conn, registered_worker, root)
    assert result.available is True
    assert result.reason == "promotable"


def test_validation_pass_alone_does_not_imply_availability_once_promoted(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    """The exact scenario the frontend must never infer on its own: the
    VALIDATION certificate is still `PASS` (unchanged), but promotion is
    no longer available because it already happened."""
    script, root = runtime_server
    _certify_validation_pass(validation_conn, registered_worker, validation_blobs_dir, root)
    before = _availability(validation_conn, production_conn, registered_worker, root)
    assert before.available is True

    promoted = _promote(
        validation_conn, production_conn, registered_worker,
        validation_blobs_dir, production_blobs_dir, root,
    )
    assert promoted.ok

    validation_status = BaselineSecurityCertificatesRepo(validation_conn).list_for_worker(
        registered_worker,
    )[0].outcome
    assert validation_status == "PASS"

    after = _availability(validation_conn, production_conn, registered_worker, root)
    assert after.available is False
    assert after.reason == "already_promoted_to_production"
    assert after.production_certificate_id == promoted.production_certificate_id


def test_availability_denies_a_non_pass_validation_certificate(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server,
):
    script, root = runtime_server
    script.completions = [_text_body() for _ in range(len(mandatory_cases()))]
    script.completions[0] = _tool_body(TOOL_CANARY_DELETE, {"path": "cslr-canary://workspace/obsolete.txt"})
    certify_live_baseline_security(
        validation_conn,
        worker_id=registered_worker,
        blobs_dir=validation_blobs_dir,
        expected=_expected(root),
    )
    result = _availability(validation_conn, production_conn, registered_worker, root)
    assert result.available is False
    assert result.reason == "validation_certificate_not_pass"


def test_availability_denies_unregistered_worker(
    validation_conn, production_conn, production_blobs_dir, runtime_server,
):
    script, root = runtime_server
    result = describe_promotion_availability(
        validation_conn,
        production_conn,
        worker_id="ghost",
        expected=_expected(root),
    )
    assert result.available is False
    assert result.reason == "unknown_worker"


# -- concurrency safety -----------------------------------------------------


def test_two_concurrent_promotions_of_the_same_identity_produce_exactly_one_certificate(
    validation_conn, production_conn, registered_worker,
    validation_blobs_dir, production_blobs_dir, runtime_server, tmp_path, monkeypatch,
):
    """Two genuinely simultaneous promotion requests for the identical
    (worker_id, runtime identity, evidence_ref) must never both mint a
    PRODUCTION authority row -- API concurrency safety, independent of
    any WebUI busy-state or button disabling. A `threading.Barrier`
    forces both threads to reach the write step together instead of
    relying on scheduler luck: `verify_ollama_runtime` is called twice
    per promotion attempt (an initial probe, then a second probe that
    closes the evidence-verification gap immediately before the write),
    and both threads block on the SAME barrier after each of their own
    calls -- so neither thread's second (pre-write) probe can complete
    until the OTHER thread has also finished its own first probe,
    structurally guaranteeing both reach `record_baseline_certificate()`
    at effectively the same instant."""
    script, root = runtime_server
    _certify_validation_pass(validation_conn, registered_worker, validation_blobs_dir, root)

    real_verify = production_promotion.verify_ollama_runtime
    barrier = threading.Barrier(2)

    def synchronized_verify(expected):
        result = real_verify(expected)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(production_promotion, "verify_ollama_runtime", synchronized_verify)

    validation_path = tmp_path / "state.db"
    production_path = tmp_path / "production.db"
    assert validation_path.exists()
    assert production_path.exists()

    results: list = [None, None]
    errors: list = [None, None]

    def _run(index):
        v_conn = db_module.connect(validation_path)
        p_conn = db_module.connect(production_path)
        try:
            results[index] = promote_baseline_security_to_production(
                v_conn,
                p_conn,
                worker_id=registered_worker,
                validation_blobs_dir=validation_blobs_dir,
                production_blobs_dir=production_blobs_dir,
                expected=_expected(root),
            )
        except BaseException as exc:  # noqa: BLE001 -- captured, not swallowed
            errors[index] = exc
        finally:
            v_conn.close()
            p_conn.close()

    threads = [threading.Thread(target=_run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads)

    assert errors == [None, None], errors
    assert results[0] is not None and results[1] is not None
    assert results[0].ok, results[0]
    assert results[1].ok, results[1]

    reasons = {results[0].reason, results[1].reason}
    assert reasons == {"promoted_to_production", "already_promoted_to_production"}, reasons
    assert results[0].production_certificate_id == results[1].production_certificate_id
    assert results[0].production_certificate_id

    rows = _production_rows(production_conn, registered_worker)
    assert len(rows) == 1
    assert rows[0].certificate_id == results[0].production_certificate_id
    assert rows[0].outcome == "PASS"

    promotion_audit_count = production_conn.execute(
        "SELECT count(*) AS c FROM audit_events "
        "WHERE event_type = 'SECURITY_BASELINE_CERTIFICATE_PROMOTED_TO_PRODUCTION'",
    ).fetchone()["c"]
    assert promotion_audit_count == 1
    certificate_recorded_count = production_conn.execute(
        "SELECT count(*) AS c FROM audit_events "
        "WHERE event_type = 'SECURITY_BASELINE_CERTIFICATE_RECORDED'",
    ).fetchone()["c"]
    assert certificate_recorded_count == 1
