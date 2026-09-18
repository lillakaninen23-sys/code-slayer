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

from code_slayer.security.live_certification import LiveOllamaRuntimeExpectation
from code_slayer.security.live_planner_certification import (
    certify_live_planner_role,
)
from code_slayer.store.role_certificates_repo import RoleCertificatesRepo
from code_slayer.store.workers_repo import WorkersRepo
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
POLICY_VERSION = "planner-certification-v1"


class _Script:
    def __init__(self) -> None:
        self.version = "0.16.1"
        self.models: list[dict] = [{"name": "test-coder:1", "digest": "sha256:abc"}]
        self.completions: list[bytes] | None = None
        self.completion_status = 200
        self.completion_posts = 0
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


def _text_body(text: str = "I cannot help with that.") -> bytes:
    return json.dumps(
        {"choices": [{"message": {"role": "assistant", "content": text}}]},
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


def _certify(conn, blobs_dir, root, **overrides):
    return certify_live_planner_role(
        conn,
        worker_id=WORKER,
        blobs_dir=blobs_dir,
        expected=_expected(root, **overrides),
        output_token_budget=OUTPUT_TOKEN_BUDGET,
        tool_choice_enforcement=TOOL_CHOICE_ENFORCEMENT,
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
    # Exactly the fixed live suite -- two tasks, one repetition each.
    assert script.completion_posts == 2


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
    policy_version=POLICY_VERSION,
    identity=None,
):
    identity = identity if identity is not None else _identity(root)
    role_eval = role_evaluation_identity_from_config(
        role=ProductionRole.PLANNER,
        runtime_identity_fingerprint=identity.runtime_identity_fingerprint,
        output_token_budget=output_token_budget,
        tool_choice_enforcement=tool_choice_enforcement,
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
    assert script.completion_posts == 2
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
    assert script.completion_posts == 2


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
    assert script.completion_posts == 2


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
    assert script.completion_posts == 4  # 2 for each of the two runs
    rows = RoleCertificatesRepo(production_conn).list_for_worker_role(WORKER, "PLANNER")
    assert len(rows) == 2
    assert all(r.outcome == "PASS" for r in rows)
