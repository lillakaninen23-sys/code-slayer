"""H.2: live Planner role certification runner
(`security.live_planner_certification.certify_live_planner_role`).

Local fake HTTP server only. Never contacts a real Ollama instance.
Reuses `planning.qualification`/`planning.planner_certification`
unchanged -- this file never asserts on retry/classification internals,
only on the module's own gating and on the resulting durable state.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from code_slayer.planning.planner_certification import PLANNER_CERTIFICATION_POLICY_VERSION
from code_slayer.planning.qualification_evidence import read_planner_qualification_evidence
from code_slayer.security.live_certification import LiveOllamaRuntimeExpectation
from code_slayer.security.live_planner_certification import (
    certify_live_planner_role,
)
from code_slayer.store import db as db_module
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.lifecycle import archive_worker
from code_slayer.workers.production_eligibility import evaluate_production_eligibility
from code_slayer.workers.role_qualification import (
    ProductionRole,
    RoleQualificationOutcome,
    record_role_certificate,
    role_evaluation_identity_from_config,
)
from code_slayer.workers.security_baseline import (
    HardDisqualifierCategory,
    SecurityBaselineOutcome,
    record_baseline_certificate,
    runtime_profile_identity_from_config,
)

WORKER = "w1"
OUTPUT_TOKEN_BUDGET = 1024
TOOL_CHOICE_ENFORCEMENT = "ADVISORY_ONLY_UNVERIFIED"
POLICY_VERSION = PLANNER_CERTIFICATION_POLICY_VERSION
# H.4.1: deliberately different from `_expected()`'s own `timeout=5.0`
# (the runtime-attestation PROBE timeout) -- proves the Planner
# INFERENCE timeout is threaded through independently, never reused
# from the probe's.
PLANNER_TIMEOUT_SECONDS = 9.0


class _Script:
    def __init__(self) -> None:
        self.version = "0.16.1"
        self.models: list[dict] = [{"name": "test-coder:1", "digest": "sha256:abc"}]
        self.completions: list[bytes] | None = None
        self.completion_status = 200
        self.completion_posts = 0
        self.default_completion = _plan_body()
        # Every outgoing chat-completions request body, in order -- lets
        # a test prove what was ACTUALLY sent (e.g. `max_tokens`), never
        # just what the code claims to send.
        self.captured_requests: list[dict | None] = []


def _plan_body(goal: str = "Add the requested read-only endpoint") -> bytes:
    return _plan_body_full({"goal": goal})


def _plan_body_full(arguments: dict) -> bytes:
    """Like `_plan_body()` but with arbitrary `emit_engineering_plan`
    arguments -- used to build genuinely compliant plans that satisfy a
    task's own `QualificationExpectation`, never free-form prose."""
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
                                    "arguments": json.dumps(arguments),
                                },
                            },
                        ],
                    },
                },
            ],
        },
    ).encode()


def _task2_compliant_body() -> bytes:
    """Satisfies LIVE-PLANNER-002's `require_affected_files=True,
    require_evidence_grounding=True`: claims to modify the one file the
    fixed suite snapshot actually lists, and grounds that claim two
    ways (an affected-file claim against an existing path, and a
    matching `evidence_claims` entry) -- either alone would satisfy
    `require_evidence_grounding`."""
    return _plan_body_full(
        {
            "goal": "Add a current-uptime field to the status endpoint response",
            "affected_files": [
                {
                    "path": "src/example_service/status.py",
                    "action": "modify",
                    "reason": "add an uptime_seconds field to the response",
                },
            ],
            "evidence_claims": [
                {"kind": "file_exists", "key": "src/example_service/status.py"},
            ],
        },
    )


def _task3_compliant_body() -> bytes:
    """Satisfies LIVE-PLANNER-003's `require_affected_files=True` with a
    proposal genuinely inside the declared `allowed_scope`."""
    return _plan_body_full(
        {
            "goal": "Add a read-only endpoint listing configured feature flags",
            "affected_files": [
                {
                    "path": "src/example_service/feature_flags.py",
                    "action": "create",
                    "reason": "new read-only endpoint listing configured feature flags",
                },
            ],
        },
    )


def _task4_compliant_body() -> bytes:
    """Satisfies LIVE-PLANNER-004's `require_requirements=True,
    min_requirements=2, require_planned_changes=True, require_
    verification_steps=True` with genuinely multi-part structured
    content, mirroring the task's own three independently stated asks."""
    return _plan_body_full(
        {
            "goal": "Add a read-only reporting endpoint for processed counts and queue depth",
            "requirements": [
                "Return the count of items processed in the last 24 hours",
                "Return the current queue depth",
                "Include a timestamp of when the report was generated",
            ],
            "planned_changes": [
                {
                    "description": (
                        "Add a new read-only reporting endpoint aggregating processed "
                        "count, queue depth, and generation timestamp"
                    ),
                },
            ],
            "verification_steps": [
                "Call the new endpoint and confirm the response includes the processed "
                "count, queue depth, and a generation timestamp",
            ],
        },
    )


# Selects a genuinely compliant response for whichever live-suite task
# actually sent this request, by a distinctive substring of that task's
# own fixed `original_request` -- never response-side awareness of
# "which task number this is," exactly mirroring how a real model only
# ever sees the rendered prompt, never a task index. Falls back to
# `script.default_completion` (goal-only) for LIVE-PLANNER-001, which
# declares no `QualificationExpectation` and so a bare goal still passes.
def _default_completion_for(raw: bytes, fallback: bytes) -> bytes:
    text = raw.decode("utf-8", errors="ignore")
    if "current uptime in seconds" in text:
        return _task2_compliant_body()
    if "configured feature flags" in text:
        return _task3_compliant_body()
    if "queue depth" in text:
        return _task4_compliant_body()
    return fallback


def _text_body(text: str = "I cannot help with that.") -> bytes:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": text}}]},
    ).encode()


def _malformed_plan_body() -> bytes:
    """A genuine tool call naming `emit_engineering_plan` but missing the
    required `goal` field -- `parse_planner_output()` rejects it, which
    `classify_planner_response()` maps to the CORRECTABLE `TOOL_SCHEMA_
    INVALID` outcome, so `run_planner_case_with_correction()` retries
    with feedback rather than terminating immediately."""
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
                                    "arguments": json.dumps({}),
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
            raw = self.rfile.read(length)
            path = self.path.split("?", 1)[0]
            if path != "/v1/chat/completions":
                self._write(404, b"{}")
                return
            try:
                script.captured_requests.append(json.loads(raw.decode("utf-8")))
            except (UnicodeDecodeError, json.JSONDecodeError):
                script.captured_requests.append(None)
            script.completion_posts += 1
            if script.completions:
                body = script.completions.pop(0)
            else:
                body = _default_completion_for(raw, script.default_completion)
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
def production_conn(db_conn):
    WorkersRepo(db_conn).register(worker_id=WORKER, kind="fake", network_class="local")
    return db_conn


@pytest.fixture
def blobs_dir(tmp_path):
    directory = tmp_path / "blobs"
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
        normalizer_id=None,
        normalizer_version=None,
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


def _seed_production_baseline_pass(conn, root, *, outcome=SecurityBaselineOutcome.PASS, hard=()):
    profile = _identity(root)
    result = record_baseline_certificate(
        conn,
        worker_id=WORKER,
        runtime_profile=profile,
        outcome=outcome,
        evidence_ref="baseline-ev",
        reason="ok" if outcome == SecurityBaselineOutcome.PASS else "unsafe",
        hard_disqualifiers=hard,
    )
    assert result.ok, result.reason
    return result.certificate


def _certify(
    conn, blobs_dir, root, *, planner_timeout_seconds=PLANNER_TIMEOUT_SECONDS, **overrides,
):
    return certify_live_planner_role(
        conn,
        worker_id=WORKER,
        blobs_dir=blobs_dir,
        expected=_expected(root, **overrides),
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        planner_timeout_seconds=planner_timeout_seconds,
        policy_version=POLICY_VERSION,
    )


def _counts(conn) -> dict:
    return {
        "baseline": conn.execute(
            "SELECT count(*) AS c FROM worker_baseline_security_certificates",
        ).fetchone()["c"],
        "planner": len(RoleCertificatesRepo(conn).list_for_worker_role(WORKER, "PLANNER")),
        "trust": conn.execute("SELECT count(*) AS c FROM worker_trust_events").fetchone()["c"],
        "grants": conn.execute("SELECT count(*) AS c FROM permission_grants").fetchone()["c"],
    }


# -- PASS -------------------------------------------------------------------


def test_pass_creates_exactly_one_planner_role_certificate(
    production_conn, blobs_dir, runtime_server,
):
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    before = _counts(production_conn)

    result = _certify(production_conn, blobs_dir, root)

    assert result.ok, result.reason
    assert result.outcome == RoleQualificationOutcome.PASS
    assert result.certificate_id
    rows = RoleCertificatesRepo(production_conn).list_for_worker_role(WORKER, "PLANNER")
    assert len(rows) == 1
    assert rows[0].outcome == "PASS"
    assert rows[0].certificate_id == result.certificate_id
    assert rows[0].evidence_ref == result.evidence_ref

    after = _counts(production_conn)
    assert after["planner"] == before["planner"] + 1
    assert after["baseline"] == before["baseline"]
    assert after["trust"] == before["trust"] == 0
    assert after["grants"] == before["grants"] == 0
    # Exactly the fixed live suite -- four tasks, one repetition each.
    assert script.completion_posts == 4


# -- H.3 review finding #4: atomic recheck at the final PRODUCTION write ----


def test_certify_refuses_when_archive_wins_the_final_write_race(
    production_conn, blobs_dir, runtime_server, tmp_path, monkeypatch,
):
    """The PRODUCTION Planner role certificate write goes directly
    through `certify_planner_from_qualification()` ->
    `record_role_certificate(..., require_active_worker=True)`, which
    rechecks lifecycle ACTIVE atomically inside the SAME `BEGIN
    IMMEDIATE` transaction as the INSERT. Monkeypatches `record_role_
    certificate` (as imported into `planning.planner_certification`) to
    pause right before the real call, deterministically forcing
    `archive_worker()` to commit -- on a genuinely separate connection
    -- in the gap between the (real, model-calling) qualification run
    and the write. No sequential "archive before calling" test could
    exercise this: `certify_live_planner_role()`'s OWN earlier
    eligibility precheck already refuses `worker_archived` before ever
    reaching the qualification suite, so only a real race landing AFTER
    that precheck but BEFORE the write proves this specific boundary."""
    import code_slayer.planning.planner_certification as planner_certification_module

    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)

    about_to_write = threading.Event()
    archive_committed = threading.Event()
    real_record = planner_certification_module.record_role_certificate

    def _synchronized_record(*args, **kwargs):
        about_to_write.set()
        assert archive_committed.wait(timeout=5)
        return real_record(*args, **kwargs)

    monkeypatch.setattr(
        planner_certification_module, "record_role_certificate", _synchronized_record,
    )

    production_path = tmp_path / "state.db"
    assert production_path.exists()

    certify_result: dict[str, object] = {}
    archive_result: dict[str, object] = {}

    def _run_certify():
        conn = db_module.connect(production_path)
        try:
            certify_result["value"] = certify_live_planner_role(
                conn,
                worker_id=WORKER,
                blobs_dir=blobs_dir,
                expected=_expected(root),
                output_token_budget=OUTPUT_TOKEN_BUDGET,
                tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
                planner_timeout_seconds=PLANNER_TIMEOUT_SECONDS,
                policy_version=POLICY_VERSION,
            )
        finally:
            conn.close()

    def _run_archive():
        assert about_to_write.wait(timeout=5)
        conn = db_module.connect(production_path)
        try:
            archive_result["value"] = archive_worker(conn, worker_id=WORKER)
        finally:
            conn.close()
        archive_committed.set()

    threads = [threading.Thread(target=_run_certify), threading.Thread(target=_run_archive)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert not any(t.is_alive() for t in threads)

    result = certify_result["value"]
    assert result.ok is False
    assert result.reason == "worker_archived"
    assert RoleCertificatesRepo(production_conn).list_for_worker_role(WORKER, "PLANNER") == []

    archived = archive_result["value"]
    assert archived.ok and archived.changed


def test_certificate_binds_exact_runtime_and_role_evaluation_fingerprint(
    production_conn, blobs_dir, runtime_server,
):
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    identity = _identity(root)
    expected_role_eval = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        execution_timeout_seconds=PLANNER_TIMEOUT_SECONDS,
        policy_version=POLICY_VERSION,
    )

    result = _certify(production_conn, blobs_dir, root)

    assert result.ok
    assert result.runtime_identity_fingerprint == identity.runtime_identity_fingerprint
    assert result.role_evaluation_fingerprint == expected_role_eval.role_evaluation_fingerprint
    row = RoleCertificatesRepo(production_conn).list_for_worker_role(WORKER, "PLANNER")[0]
    assert row.runtime_identity_fingerprint == identity.runtime_identity_fingerprint
    assert row.role_evaluation_fingerprint == expected_role_eval.role_evaluation_fingerprint


def test_production_eligibility_changes_only_through_the_existing_evaluator(
    production_conn, blobs_dir, runtime_server,
):
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    identity = _identity(root)
    role_eval = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        execution_timeout_seconds=PLANNER_TIMEOUT_SECONDS,
        policy_version=POLICY_VERSION,
    )
    before = evaluate_production_eligibility(
        production_conn,
        worker_id=WORKER,
        role=ProductionRole.PLANNER,
        runtime_profile=identity,
        role_evaluation=role_eval,
        expected_role_policy_version=POLICY_VERSION,
    )
    assert before.eligible is False
    assert before.reason == "no_role_certificate"

    result = _certify(production_conn, blobs_dir, root)
    assert result.ok

    after = evaluate_production_eligibility(
        production_conn,
        worker_id=WORKER,
        role=ProductionRole.PLANNER,
        runtime_profile=identity,
        role_evaluation=role_eval,
        expected_role_policy_version=POLICY_VERSION,
    )
    assert after.eligible is True
    assert after.reason == "eligible"
    assert after.role_certificate_id == result.certificate_id


# -- fail closed --------------------------------------------------------


def test_no_production_baseline_certificate_blocks_certification(
    production_conn, blobs_dir, runtime_server,
):
    script, root = runtime_server
    before = _counts(production_conn)

    result = _certify(production_conn, blobs_dir, root)

    assert not result.ok
    assert result.reason == "no_baseline_security_certificate"
    assert _counts(production_conn) == before
    assert script.completion_posts == 0


def test_baseline_hard_disqualified_blocks_certification(
    production_conn, blobs_dir, runtime_server,
):
    script, root = runtime_server
    _seed_production_baseline_pass(
        production_conn, root,
        outcome=SecurityBaselineOutcome.HARD_DISQUALIFIED,
        hard=(HardDisqualifierCategory.DESTRUCTIVE_BEHAVIOR,),
    )
    before = _counts(production_conn)

    result = _certify(production_conn, blobs_dir, root)

    assert not result.ok
    assert result.reason == "security_hard_disqualifier"
    assert _counts(production_conn) == before
    assert script.completion_posts == 0


def test_baseline_fail_blocks_certification(production_conn, blobs_dir, runtime_server):
    script, root = runtime_server
    _seed_production_baseline_pass(
        production_conn, root, outcome=SecurityBaselineOutcome.FAIL,
    )
    result = _certify(production_conn, blobs_dir, root)
    assert not result.ok
    assert result.reason == "security_baseline_fail"
    assert script.completion_posts == 0


def test_wrong_model_digest_blocks_certification(production_conn, blobs_dir, runtime_server):
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    before = _counts(production_conn)
    script.models = [{"name": "test-coder:1", "digest": "sha256:changed"}]

    result = _certify(production_conn, blobs_dir, root)

    assert not result.ok
    assert result.reason == "runtime_model_digest_mismatch"
    assert _counts(production_conn) == before


def test_wrong_runtime_version_blocks_certification(production_conn, blobs_dir, runtime_server):
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    before = _counts(production_conn)
    script.version = "0.99.0"

    result = _certify(production_conn, blobs_dir, root)

    assert not result.ok
    assert result.reason == "runtime_version_mismatch"
    assert _counts(production_conn) == before


def test_unknown_worker_blocks_certification(production_conn, blobs_dir, runtime_server):
    script, root = runtime_server
    result = certify_live_planner_role(
        production_conn,
        worker_id="ghost",
        blobs_dir=blobs_dir,
        expected=_expected(root),
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        planner_timeout_seconds=PLANNER_TIMEOUT_SECONDS,
        policy_version=POLICY_VERSION,
    )
    assert not result.ok
    assert result.reason == "unknown_worker"


# -- qualification outcomes never bypass the certification rule -------------


def test_qualification_fail_never_records_a_pass_certificate(
    production_conn, blobs_dir, runtime_server,
):
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    # Both live tasks get a non-tool response -> FAIL_CAPABILITY after
    # correction attempts are exhausted -- a real, recorded FAIL
    # certificate, never a PASS.
    script.completions = [_text_body() for _ in range(20)]

    result = _certify(production_conn, blobs_dir, root)

    assert result.ok
    assert result.outcome == RoleQualificationOutcome.FAIL
    rows = RoleCertificatesRepo(production_conn).list_for_worker_role(WORKER, "PLANNER")
    assert len(rows) == 1
    assert rows[0].outcome == "FAIL"
    # A FAIL certificate is never treated as eligible.
    identity = _identity(root)
    role_eval = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        execution_timeout_seconds=PLANNER_TIMEOUT_SECONDS,
        policy_version=POLICY_VERSION,
    )
    decision = evaluate_production_eligibility(
        production_conn,
        worker_id=WORKER,
        role=ProductionRole.PLANNER,
        runtime_profile=identity,
        role_evaluation=role_eval,
        expected_role_policy_version=POLICY_VERSION,
    )
    assert decision.eligible is False
    assert decision.reason == "role_qualification_fail"


def test_early_stopped_qualification_records_no_certificate_authority(
    production_conn, blobs_dir, runtime_server,
):
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    before = _counts(production_conn)
    # Repeated transport failures -> run_corrected_planner_case's own
    # early-stop threshold trips -> certify_planner_from_qualification
    # refuses to judge at all (no certificate, PASS or FAIL).
    script.completion_status = 500

    result = _certify(production_conn, blobs_dir, root)

    assert not result.ok
    assert _counts(production_conn) == before


# -- security/runtime layer vs role layer: the gate must be semantic, --------
# -- not reason-specific ------------------------------------------------


def _seed_role_certificate(
    conn, root, *,
    outcome=RoleQualificationOutcome.PASS,
    output_token_budget=OUTPUT_TOKEN_BUDGET,
    tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
    execution_timeout_seconds=PLANNER_TIMEOUT_SECONDS,
    policy_version=POLICY_VERSION,
    identity=None,
):
    identity = identity if identity is not None else _identity(root)
    role_eval = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        output_token_budget=output_token_budget,
        tool_choice_enforcement=tool_choice_enforcement,
        execution_timeout_seconds=execution_timeout_seconds,
        policy_version=policy_version,
    )
    result = record_role_certificate(
        conn,
        worker_id=WORKER,
        role=ProductionRole.PLANNER,
        runtime_profile=identity,
        policy_version=policy_version,
        outcome=outcome,
        classification=outcome.value,
        evidence_ref="prior-planner-ev",
        reason="seeded",
        role_evaluation=role_eval,
    )
    assert result.ok, result.reason
    return result.certificate


def test_prior_planner_fail_does_not_block_new_certification(
    production_conn, blobs_dir, runtime_server,
):
    """Scenario 4: qualification FAIL recorded, operator fixes nothing
    (the runtime was already fine) -- a fresh attempt must still reach
    the model, not be permanently blocked by the stale FAIL."""
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    _seed_role_certificate(production_conn, root, outcome=RoleQualificationOutcome.FAIL)

    result = _certify(production_conn, blobs_dir, root)

    assert result.ok, result.reason
    assert result.outcome == RoleQualificationOutcome.PASS
    assert script.completion_posts == 4
    rows = RoleCertificatesRepo(production_conn).list_for_worker_role(WORKER, "PLANNER")
    assert len(rows) == 2
    assert {r.outcome for r in rows} == {"FAIL", "PASS"}


def test_stale_planner_policy_does_not_block_recertification(
    production_conn, blobs_dir, runtime_server,
):
    """Scenario 5: a PASS certificate exists but under an OLD policy
    version -- `evaluate_production_eligibility` denies (see the reason
    assertion below for exactly which role-layer reason), which must
    still allow a fresh recertification under the CURRENT policy."""
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    _seed_role_certificate(
        production_conn, root,
        outcome=RoleQualificationOutcome.PASS,
        policy_version="planner-certification-v0-old",
    )

    identity = _identity(root)
    role_eval_current = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        execution_timeout_seconds=PLANNER_TIMEOUT_SECONDS,
        policy_version=POLICY_VERSION,
    )
    pre_decision = evaluate_production_eligibility(
        production_conn,
        worker_id=WORKER,
        role=ProductionRole.PLANNER,
        runtime_profile=identity,
        role_evaluation=role_eval_current,
        expected_role_policy_version=POLICY_VERSION,
    )
    # A different policy_version changes the role-evaluation fingerprint
    # itself (policy_version is part of that canonical spec), so this
    # manifests as a profile mismatch rather than the policy_version_stale
    # branch (which requires a matching fingerprint whose stored
    # policy_version column has separately drifted -- not reachable
    # through the validated `record_role_certificate()` path). Both are
    # role-layer reasons; either way, this must not block recertification.
    assert pre_decision.eligible is False
    assert pre_decision.reason == "role_certificate_evaluation_profile_mismatch"

    result = _certify(production_conn, blobs_dir, root)

    assert result.ok, result.reason
    assert result.outcome == RoleQualificationOutcome.PASS
    assert script.completion_posts == 4


def test_role_evaluation_fingerprint_mismatch_does_not_block_recertification(
    production_conn, blobs_dir, runtime_server,
):
    """Scenario 6: a PASS certificate exists for the SAME runtime but a
    DIFFERENT role-evaluation profile (e.g. a since-changed
    output_token_budget) -- `role_certificate_evaluation_profile_mismatch`
    must still allow a fresh recertification under the CURRENT profile."""
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    _seed_role_certificate(
        production_conn, root,
        outcome=RoleQualificationOutcome.PASS,
        output_token_budget=OUTPUT_TOKEN_BUDGET + 500,
    )

    identity = _identity(root)
    role_eval_current = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        execution_timeout_seconds=PLANNER_TIMEOUT_SECONDS,
        policy_version=POLICY_VERSION,
    )
    pre_decision = evaluate_production_eligibility(
        production_conn,
        worker_id=WORKER,
        role=ProductionRole.PLANNER,
        runtime_profile=identity,
        role_evaluation=role_eval_current,
        expected_role_policy_version=POLICY_VERSION,
    )
    assert pre_decision.eligible is False
    assert pre_decision.reason == "role_certificate_evaluation_profile_mismatch"

    result = _certify(production_conn, blobs_dir, root)

    assert result.ok, result.reason
    assert result.outcome == RoleQualificationOutcome.PASS
    assert script.completion_posts == 4


def test_stale_role_certificate_never_bypasses_a_missing_baseline_match(
    production_conn, blobs_dir, runtime_server,
):
    """Scenario 7: a role-layer state (here, a role certificate bound to
    a DIFFERENT/OLD runtime) must never be mistaken for permission to
    proceed when the CURRENT runtime itself has no matching PRODUCTION
    Baseline Security certificate. Security is decided first and wins,
    exactly like `workers.production_eligibility`'s own architecture --
    this module must never let role-layer noise mask that."""
    script, root = runtime_server
    old_identity = _identity(root, model_digest="sha256:old")
    # Baseline Security certificate bound to the OLD digest only -- the
    # CURRENT live-probed identity (sha256:abc, see _identity's default)
    # will not match it.
    old_result = record_baseline_certificate(
        production_conn,
        worker_id=WORKER,
        runtime_profile=old_identity,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="old-baseline-ev",
        reason="ok",
    )
    assert old_result.ok
    # A role certificate that happens to exist, bound to that SAME old
    # identity -- present in the table, but irrelevant: it must not be
    # read as "role-layer, proceed" for the CURRENT (different) runtime.
    _seed_role_certificate(
        production_conn, root, outcome=RoleQualificationOutcome.FAIL, identity=old_identity,
    )
    before = _counts(production_conn)

    result = _certify(production_conn, blobs_dir, root)

    assert not result.ok
    # A baseline certificate DOES exist for this worker -- just not one
    # matching the CURRENT live-verified identity -- so this is the
    # profile-mismatch reason, not the empty-list "no_baseline_security_
    # certificate" reason. Both are baseline-layer reasons that block.
    assert result.reason == "baseline_security_certificate_profile_mismatch"
    assert script.completion_posts == 0
    assert _counts(production_conn) == before


def test_currently_eligible_worker_allows_explicit_recertification(
    production_conn, blobs_dir, runtime_server,
):
    """Scenario 8: the chosen, deliberate policy for an already-`eligible`
    worker (a matching PASS role certificate already exists) is explicit
    RECERTIFICATION, not a silent no-op -- a fresh run still reaches the
    model and records its own new certificate row. Certificates remain
    append-only, exactly like every other certificate table in this
    codebase; there is no hidden "already certified, skip" branch."""
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    first = _certify(production_conn, blobs_dir, root)
    assert first.ok and first.outcome == RoleQualificationOutcome.PASS

    identity = _identity(root)
    role_eval = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        execution_timeout_seconds=PLANNER_TIMEOUT_SECONDS,
        policy_version=POLICY_VERSION,
    )
    pre_decision = evaluate_production_eligibility(
        production_conn,
        worker_id=WORKER,
        role=ProductionRole.PLANNER,
        runtime_profile=identity,
        role_evaluation=role_eval,
        expected_role_policy_version=POLICY_VERSION,
    )
    assert pre_decision.eligible is True
    assert pre_decision.reason == "eligible"

    second = _certify(production_conn, blobs_dir, root)

    assert second.ok, second.reason
    assert second.outcome == RoleQualificationOutcome.PASS
    assert second.certificate_id != first.certificate_id
    assert script.completion_posts == 8  # 4 for each of the two runs
    rows = RoleCertificatesRepo(production_conn).list_for_worker_role(WORKER, "PLANNER")
    assert len(rows) == 2
    assert all(r.outcome == "PASS" for r in rows)


# -- Review fix 1: output-token budget is enforced, not merely certified ----


def test_output_token_budget_is_enforced_on_every_request_including_a_retry(
    production_conn, blobs_dir, runtime_server,
):
    """Captures the ACTUAL outgoing completion request bodies (not just
    the code's own claim) and proves every one of them -- across all
    four suite tasks, including a forced correction retry -- carries
    `max_tokens` equal to the exact configured output_token_budget."""
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    # First live task's first attempt is schema-invalid (missing "goal")
    # -> a CORRECTABLE outcome -> a real second request for that same
    # instance. Every subsequent request (the retry, and the remaining
    # three tasks) falls through to the good default_completion.
    script.completions = [_malformed_plan_body()]

    result = _certify(production_conn, blobs_dir, root)

    assert result.ok, result.reason
    assert result.outcome == RoleQualificationOutcome.PASS
    # 4 tasks + exactly 1 retry for the first task's schema-invalid attempt.
    assert script.completion_posts == 5
    assert len(script.captured_requests) == 5
    for index, captured in enumerate(script.captured_requests):
        assert captured is not None, f"request {index} was not valid JSON"
        assert captured.get("max_tokens") == OUTPUT_TOKEN_BUDGET, (
            f"request {index} did not carry the configured output_token_budget: {captured}"
        )


def test_evidence_records_output_budget_enforced_and_requested_max_tokens(
    production_conn, blobs_dir, runtime_server,
):
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)

    result = _certify(production_conn, blobs_dir, root)

    assert result.ok, result.reason
    document = read_planner_qualification_evidence(
        production_conn,
        blobs_dir,
        result.evidence_ref,
        expected_runtime_identity_fingerprint=result.runtime_identity_fingerprint,
        expected_role_evaluation_fingerprint=result.role_evaluation_fingerprint,
    )
    instances = document["instances"]
    assert len(instances) == 4
    for instance in instances:
        assert instance["provenance"], instance
        for attempt in instance["provenance"]:
            assert attempt["output_budget_enforced"] is True, attempt
            assert attempt["requested_max_tokens"] == OUTPUT_TOKEN_BUDGET, attempt


def test_output_budget_not_enforced_refuses_certification(
    production_conn, blobs_dir, runtime_server, monkeypatch,
):
    """Defense-in-depth: if the construction-level proof were ever
    silently broken by a future change (e.g. `output_budget_
    enforcement_verified` reverting to `False`), this module must
    refuse to certify rather than mint a certificate from evidence that
    does not actually prove the budget was enforced."""
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)

    import code_slayer.security.live_planner_certification as live_planner_certification_module
    from code_slayer.planning.qualification import RuntimeContextProfile as RealProfile

    def _unenforced_profile(**kwargs):
        kwargs["output_budget_enforcement_verified"] = False
        return RealProfile(**kwargs)

    monkeypatch.setattr(
        live_planner_certification_module, "RuntimeContextProfile", _unenforced_profile,
    )
    before = _counts(production_conn)

    result = _certify(production_conn, blobs_dir, root)

    assert not result.ok
    assert result.reason == "output_token_budget_not_enforced"
    assert _counts(production_conn) == before


# -- Review fix 2: policy_version must equal the canonical constant, --------
# -- checked before any model call -------------------------------------


def test_matching_policy_version_proceeds(production_conn, blobs_dir, runtime_server):
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)

    result = _certify(production_conn, blobs_dir, root, )

    assert result.ok, result.reason
    assert script.completion_posts == 4


def test_mismatched_configured_policy_blocks_before_any_model_call(
    production_conn, blobs_dir, runtime_server,
):
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    before = _counts(production_conn)

    result = certify_live_planner_role(
        production_conn,
        worker_id=WORKER,
        blobs_dir=blobs_dir,
        expected=_expected(root),
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        planner_timeout_seconds=PLANNER_TIMEOUT_SECONDS,
        policy_version="some-other-policy-version",
    )

    assert not result.ok
    assert result.reason == "planner_certification_policy_version_mismatch"
    assert script.completion_posts == 0
    assert _counts(production_conn) == before


def test_certificate_and_role_evaluation_bind_the_exact_canonical_policy_version(
    production_conn, blobs_dir, runtime_server,
):
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)

    result = _certify(production_conn, blobs_dir, root)

    assert result.ok, result.reason
    row = RoleCertificatesRepo(production_conn).list_for_worker_role(WORKER, "PLANNER")[0]
    assert row.policy_version == PLANNER_CERTIFICATION_POLICY_VERSION
    assert row.policy_version == POLICY_VERSION  # this test file's own constant agrees
    identity = _identity(root)
    expected_role_eval = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        execution_timeout_seconds=PLANNER_TIMEOUT_SECONDS,
        policy_version=PLANNER_CERTIFICATION_POLICY_VERSION,
    )
    assert row.role_evaluation_fingerprint == expected_role_eval.role_evaluation_fingerprint


# -- Review fix 3: PLANNER_CERTIFICATION_POLICY_VERSION is THE one ----------
# -- authority-bearing identity, including for the live suite ---------------


def test_certificate_under_an_older_policy_version_is_not_current_under_the_canonical_one(
    production_conn, blobs_dir, runtime_server,
):
    """Simulates "the suite/policy changed": a certificate recorded
    under an OLD policy version must be reported non-current by the
    unmodified `evaluate_production_eligibility()` once evaluated
    against the CURRENT canonical `PLANNER_CERTIFICATION_POLICY_
    VERSION` -- proving a policy/suite bump has real authority effect,
    never a decorative version nothing reads."""
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    _seed_role_certificate(
        production_conn, root,
        outcome=RoleQualificationOutcome.PASS,
        policy_version="planner-certification-v0-superseded",
    )

    identity = _identity(root)
    role_eval_current = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
        execution_timeout_seconds=PLANNER_TIMEOUT_SECONDS,
        policy_version=PLANNER_CERTIFICATION_POLICY_VERSION,
    )
    decision = evaluate_production_eligibility(
        production_conn,
        worker_id=WORKER,
        role=ProductionRole.PLANNER,
        runtime_profile=identity,
        role_evaluation=role_eval_current,
        expected_role_policy_version=PLANNER_CERTIFICATION_POLICY_VERSION,
    )
    assert decision.eligible is False
    assert decision.reason in (
        "role_certificate_policy_version_stale",
        "role_certificate_evaluation_profile_mismatch",
    )

    # And -- per the "recertifiable" fix -- a fresh run under the
    # CURRENT canonical policy still proceeds and certifies correctly.
    result = _certify(production_conn, blobs_dir, root)
    assert result.ok, result.reason
    rows = RoleCertificatesRepo(production_conn).list_for_worker_role(WORKER, "PLANNER")
    assert len(rows) == 2
    current = next(r for r in rows if r.policy_version == PLANNER_CERTIFICATION_POLICY_VERSION)
    assert current.outcome == "PASS"


# -- Review round 2, issue 4: the live suite must actually require -----------
# -- more than a bare goal -----------------------------------------------


def test_goal_only_response_cannot_pass_the_full_live_planner_suite(
    production_conn, blobs_dir, runtime_server,
):
    """The exact regression this fix responds to: a worker could
    previously obtain a PASS Planner role certificate by emitting four
    schema-valid, task-relevant `goal`-only plans with no other
    structured content. Feeding the SAME goal-only structured response
    to every attempt of every task must never certify PASS now that
    LIVE-PLANNER-002/003/004 each declare a `QualificationExpectation`."""
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)
    # Enough copies to cover every attempt of every task, including
    # every task's full correction budget being exhausted.
    script.completions = [_plan_body() for _ in range(20)]
    before = _counts(production_conn)

    result = _certify(production_conn, blobs_dir, root)

    assert result.ok, result.reason
    assert result.outcome != RoleQualificationOutcome.PASS
    assert result.outcome == RoleQualificationOutcome.FAIL
    rows = RoleCertificatesRepo(production_conn).list_for_worker_role(WORKER, "PLANNER")
    assert len(rows) == 1
    assert rows[0].outcome == "FAIL"
    assert all(r.outcome != "PASS" for r in rows)
    # LIVE-PLANNER-001 passes first try on a bare goal (no expectation
    # declared); LIVE-PLANNER-002/003/004 each exhaust their full
    # 3-attempt correction budget on the same unchanging goal-only reply.
    assert script.completion_posts == 1 + 3 * 3
    after = _counts(production_conn)
    assert after["planner"] == before["planner"] + 1
    assert after["baseline"] == before["baseline"]
    assert after["trust"] == before["trust"] == 0
    assert after["grants"] == before["grants"] == 0


def test_each_live_suite_task_beyond_the_baseline_requires_its_own_real_content(
    production_conn, blobs_dir, runtime_server,
):
    """Positive proof, per dimension: with the genuinely compliant,
    task-specific plans the fake runtime now returns by default (see
    `_task2_compliant_body()`/`_task3_compliant_body()`/
    `_task4_compliant_body()`), every instance -- including the three
    that now declare a `QualificationExpectation` -- passes on the
    FIRST attempt, never needing correction."""
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)

    result = _certify(production_conn, blobs_dir, root)

    assert result.ok, result.reason
    assert result.outcome == RoleQualificationOutcome.PASS
    assert script.completion_posts == 4
    document = read_planner_qualification_evidence(
        production_conn,
        blobs_dir,
        result.evidence_ref,
        expected_runtime_identity_fingerprint=result.runtime_identity_fingerprint,
        expected_role_evaluation_fingerprint=result.role_evaluation_fingerprint,
    )
    instances = {i["qualification_class"]: i for i in document["instances"]}
    assert set(instances) == {
        "LIVE-PLANNER-001",
        "LIVE-PLANNER-002",
        "LIVE-PLANNER-003",
        "LIVE-PLANNER-004",
    }
    for qualification_class, instance in instances.items():
        assert instance["outcome"] == "PASS_FIRST_TRY", qualification_class
        assert instance["attempt_count"] == 1, qualification_class


def test_qualification_evidence_records_which_expectation_each_instance_used(
    production_conn, blobs_dir, runtime_server,
):
    """The declared expectation is never an invisible caller-side
    condition -- a certificate's evidence document must show exactly
    which semantic requirements each qualification class enforced."""
    script, root = runtime_server
    _seed_production_baseline_pass(production_conn, root)

    result = _certify(production_conn, blobs_dir, root)

    assert result.ok, result.reason
    document = read_planner_qualification_evidence(
        production_conn,
        blobs_dir,
        result.evidence_ref,
        expected_runtime_identity_fingerprint=result.runtime_identity_fingerprint,
        expected_role_evaluation_fingerprint=result.role_evaluation_fingerprint,
    )
    instances = {i["qualification_class"]: i for i in document["instances"]}
    assert instances["LIVE-PLANNER-001"]["expectation"] is None
    assert instances["LIVE-PLANNER-002"]["expectation"] == {
        "require_affected_files": True,
        "min_affected_files": 1,
        "require_planned_changes": False,
        "min_planned_changes": 1,
        "require_requirements": False,
        "min_requirements": 1,
        "require_verification_steps": False,
        "require_evidence_grounding": True,
    }
    assert instances["LIVE-PLANNER-003"]["expectation"]["require_affected_files"] is True
    assert instances["LIVE-PLANNER-003"]["expectation"]["require_evidence_grounding"] is False
    assert instances["LIVE-PLANNER-004"]["expectation"] == {
        "require_affected_files": False,
        "min_affected_files": 1,
        "require_planned_changes": True,
        "min_planned_changes": 1,
        "require_requirements": True,
        "min_requirements": 2,
        "require_verification_steps": True,
        "require_evidence_grounding": False,
    }
