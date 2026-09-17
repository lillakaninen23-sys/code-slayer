"""Baseline Security Certification foundation: certificate
representation/recording (`workers.security_baseline`).

`workers.production_eligibility`'s own tests (combining this with Role
Qualification Certificates) live in
`tests/unit/test_production_eligibility.py`."""

from __future__ import annotations

import json
import sqlite3

import pytest

from code_slayer.audit.events import EventType
from code_slayer.audit.verify import verify_chain
from code_slayer.store.baseline_security_certificates_repo import (
    BaselineSecurityCertificatesRepo,
)
from code_slayer.store.db import connect, migrate
from code_slayer.store.workers_repo import WorkersRepo
from code_slayer.workers.promotion import promote_from_conformance
from code_slayer.workers.security_baseline import (
    BASELINE_VERSION,
    RUNTIME_CONFIG_SPEC_VERSION,
    HardDisqualifierCategory,
    RuntimeProfileIdentity,
    SecurityBaselineOutcome,
    SecurityCertificationResult,
    canonical_runtime_config_spec,
    fingerprint_runtime_config,
    record_baseline_certificate,
    runtime_profile_identity_from_config,
)
from code_slayer.workers.trust import TrustLevel, WorkerTrustManager


@pytest.fixture
def registered_worker(db_conn) -> str:
    WorkersRepo(db_conn).register(worker_id="w1", kind="fake", network_class="local")
    return "w1"


@pytest.fixture
def profile() -> RuntimeProfileIdentity:
    return RuntimeProfileIdentity(model_tag="devstral:24b", endpoint="http://local:11436/v1")


def _pass(conn, worker_id, profile, *, evidence_ref="evidence-1", now_fn=None, reason="ok"):
    kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    return record_baseline_certificate(
        conn,
        worker_id=worker_id,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref=evidence_ref,
        reason=reason,
        **kwargs,
    )


def _fail(conn, worker_id, profile, *, evidence_ref="evidence-1", now_fn=None, reason="unsafe"):
    kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    return record_baseline_certificate(
        conn,
        worker_id=worker_id,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.FAIL,
        evidence_ref=evidence_ref,
        reason=reason,
        **kwargs,
    )


def _hard_disqualified(conn, worker_id, profile, *, evidence_ref="evidence-1", now_fn=None):
    kwargs = {"now_fn": now_fn} if now_fn is not None else {}
    return record_baseline_certificate(
        conn,
        worker_id=worker_id,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.HARD_DISQUALIFIED,
        evidence_ref=evidence_ref,
        reason="attempted_policy_bypass",
        hard_disqualifiers=(HardDisqualifierCategory.POLICY_OR_GATE_BYPASS_ATTEMPT,),
        **kwargs,
    )


def _passing_conformance_responses():
    from code_slayer.workers.protocol import WorkerResponse, WorkerResponseKind, WorkerToolCall

    return [
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="hi there"),
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="clean output"),
        WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="read_file", params={"path": "README.md"}),
        ),
        WorkerResponse(kind=WorkerResponseKind.TEXT, text="continuing"),
        WorkerResponse(
            kind=WorkerResponseKind.TOOL_CALL,
            tool_call=WorkerToolCall(tool="read_file", params={"path": "README.md"}),
        ),
    ]


# -- RuntimeProfileIdentity: exact matching, no wildcards --------------------


def test_runtime_profile_identity_requires_nonempty_model_tag():
    with pytest.raises(ValueError):
        RuntimeProfileIdentity(model_tag="")


def test_runtime_profile_identity_matches_require_every_field_equal():
    a = RuntimeProfileIdentity(model_tag="m", model_digest="d1", endpoint="e", runtime_version="v")
    b = RuntimeProfileIdentity(model_tag="m", model_digest="d1", endpoint="e", runtime_version="v")
    c = RuntimeProfileIdentity(model_tag="m", model_digest="d2", endpoint="e", runtime_version="v")
    assert a.matches(b)
    assert not a.matches(c)


def test_runtime_profile_identity_none_never_matches_a_set_value():
    a = RuntimeProfileIdentity(model_tag="m")
    b = RuntimeProfileIdentity(model_tag="m", model_digest="d1")
    assert not a.matches(b)
    assert not b.matches(a)


def test_runtime_profile_identity_is_fully_specified_requires_every_field():
    assert not RuntimeProfileIdentity(model_tag="m").is_fully_specified
    assert not RuntimeProfileIdentity(model_tag="m", model_digest="d").is_fully_specified
    assert not RuntimeProfileIdentity(
        model_tag="m",
        model_digest="d",
        endpoint="e",
        runtime_version="v",
    ).is_fully_specified
    full = runtime_profile_identity_from_config(
        model_tag="m",
        model_digest="d",
        endpoint="e",
        runtime_version="v",
        effective_context_tokens=16384,
        output_token_budget=4096,
        temperature=0.0,
        tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
    )
    assert full.is_fully_specified
    # Native-only (normalizer None/None) is a complete compatibility-layer
    # identity, not a missing field -- but a fingerprint is still required.
    assert full.normalizer_id is None
    assert full.normalizer_version is None
    normalized = runtime_profile_identity_from_config(
        model_tag="m",
        model_digest="d",
        endpoint="e",
        runtime_version="v",
        effective_context_tokens=16384,
        output_token_budget=4096,
        temperature=0.0,
        tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
    )
    assert normalized.is_fully_specified
    assert not full.matches(normalized)


# -- record_baseline_certificate(): fail-closed validation -------------------


def test_pass_certificate_requires_evidence_reference(db_conn, registered_worker, profile):
    result = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="   ",
        reason="ok",
    )
    assert result == SecurityCertificationResult(False, "missing_evidence_reference")


def test_fail_certificate_also_requires_evidence_reference(db_conn, registered_worker, profile):
    """A certificate is never a bare boolean -- FAIL needs provenance
    exactly as much as PASS does."""
    result = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.FAIL,
        evidence_ref="",
        reason="unsafe",
    )
    assert not result.ok
    assert result.reason == "missing_evidence_reference"


def test_hard_disqualified_requires_at_least_one_disqualifier(db_conn, registered_worker, profile):
    result = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.HARD_DISQUALIFIED,
        evidence_ref="ev",
        reason="bad",
        hard_disqualifiers=(),
    )
    assert result == SecurityCertificationResult(
        False,
        "hard_disqualified_requires_at_least_one_disqualifier",
    )


def test_pass_outcome_rejects_disqualifiers_being_attached(db_conn, registered_worker, profile):
    """A hard disqualifier can never be silently attached to a PASS --
    that inconsistency is refused outright, never coerced."""
    result = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=profile,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="ev",
        reason="ok",
        hard_disqualifiers=(HardDisqualifierCategory.DESTRUCTIVE_BEHAVIOR,),
    )
    assert result == SecurityCertificationResult(
        False,
        "hard_disqualifiers_only_valid_for_hard_disqualified_outcome",
    )


def test_certificate_for_unregistered_worker_is_refused(db_conn, profile):
    result = _pass(db_conn, "never-registered", profile)
    assert result == SecurityCertificationResult(False, "unknown_worker")
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker("never-registered") == []


def test_malformed_outcome_type_is_refused(db_conn, registered_worker, profile):
    result = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile=profile,
        outcome="PASS",  # a plain string, not SecurityBaselineOutcome.PASS
        evidence_ref="ev",
        reason="ok",
    )
    assert result == SecurityCertificationResult(False, "malformed_certificate_request")


def test_malformed_runtime_profile_type_is_refused(db_conn, registered_worker):
    result = record_baseline_certificate(
        db_conn,
        worker_id=registered_worker,
        runtime_profile="devstral:24b",
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="ev",
        reason="ok",
    )
    assert result == SecurityCertificationResult(False, "malformed_certificate_request")


# -- persistence round-trip -----------------------------------------------


def test_certificate_round_trip_across_reopen(tmp_path):
    path = tmp_path / "state.db"
    conn = connect(path)
    migrate(conn)
    WorkersRepo(conn).register(worker_id="w1", kind="fake", network_class="local")
    profile_ = RuntimeProfileIdentity(model_tag="devstral:24b", runtime_version="0.1.0")
    result = record_baseline_certificate(
        conn,
        worker_id="w1",
        runtime_profile=profile_,
        outcome=SecurityBaselineOutcome.PASS,
        evidence_ref="conformance-run-abc",
        reason="baseline_checks_passed",
    )
    assert result.ok
    certificate_id = result.certificate.certificate_id
    conn.close()

    reopened = connect(path)
    migrate(reopened)
    fetched = BaselineSecurityCertificatesRepo(reopened).get(certificate_id)
    assert fetched is not None
    assert fetched.worker_id == "w1"
    assert fetched.model_tag == "devstral:24b"
    assert fetched.runtime_version == "0.1.0"
    assert fetched.model_digest is None
    assert fetched.runtime_config_fingerprint is None
    assert fetched.outcome == "PASS"
    assert fetched.evidence_ref == "conformance-run-abc"
    assert fetched.baseline_version == BASELINE_VERSION
    reopened.close()


# -- append-only schema -------------------------------------------------


def test_certificate_table_is_append_only(db_conn, registered_worker, profile):
    result = _pass(db_conn, registered_worker, profile)
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "UPDATE worker_baseline_security_certificates SET outcome = 'FAIL' "
            "WHERE certificate_id = ?",
            (result.certificate.certificate_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        db_conn.execute(
            "DELETE FROM worker_baseline_security_certificates WHERE certificate_id = ?",
            (result.certificate.certificate_id,),
        )


# -- audit/provenance -----------------------------------------------------


def test_recording_a_certificate_is_audited(db_conn, registered_worker, profile):
    result = _pass(db_conn, registered_worker, profile, reason="baseline_checks_passed")
    rows = [
        dict(r)
        for r in db_conn.execute(
            "SELECT event_type, payload_json FROM audit_events WHERE task_id IS NULL ORDER BY seq",
        )
    ]
    matching = [r for r in rows if r["event_type"] == "SECURITY_BASELINE_CERTIFICATE_RECORDED"]
    assert len(matching) == 1
    payload = json.loads(matching[0]["payload_json"])
    assert payload["certificate_id"] == result.certificate.certificate_id
    assert payload["worker_id"] == registered_worker
    assert payload["outcome"] == "PASS"
    assert payload["evidence_ref"] == "evidence-1"
    assert "runtime_config_fingerprint" in payload
    assert payload["runtime_config_fingerprint"] is None
    assert verify_chain(db_conn, task_id=None).ok


def test_hard_disqualified_certificate_is_also_audited_with_its_categories(
    db_conn,
    registered_worker,
    profile,
):
    _hard_disqualified(db_conn, registered_worker, profile)
    rows = [
        dict(r)
        for r in db_conn.execute(
            "SELECT payload_json FROM audit_events WHERE event_type = ?",
            (EventType.SECURITY_BASELINE_CERTIFICATE_RECORDED.value,),
        )
    ]
    payload = json.loads(rows[0]["payload_json"])
    assert payload["outcome"] == "HARD_DISQUALIFIED"
    assert payload["hard_disqualifiers"] == ["POLICY_OR_GATE_BYPASS_ATTEMPT"]


# -- registration/trust never fabricate a certificate ------------------------


def test_worker_registration_creates_no_security_certificate(db_conn):
    WorkersRepo(db_conn).register(worker_id="fresh-worker", kind="fake", network_class="local")
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker("fresh-worker") == []


def test_trust_promotion_creates_no_security_certificate(db_conn, registered_worker):
    from code_slayer.workers.conformance import run_conformance_suite
    from code_slayer.workers.fake_adapter import FakeWorkerAdapter

    responses = _passing_conformance_responses()
    suite = run_conformance_suite(
        db_conn,
        FakeWorkerAdapter(responses),
        worker_id=registered_worker,
        role="coder",
    )
    assert suite.ok and suite.status == "PASSED"
    promotion = promote_from_conformance(
        db_conn,
        worker_id=registered_worker,
        role="coder",
        capability="read_file",
        run_id=suite.run_id,
    )
    assert promotion.ok
    assert BaselineSecurityCertificatesRepo(db_conn).list_for_worker(registered_worker) == []


# -- certificate never mutates trust -----------------------------------------


def test_recording_any_certificate_outcome_never_changes_trust(db_conn, registered_worker, profile):
    manager = WorkerTrustManager(db_conn)
    before = manager.current_trust(registered_worker, "coder", "read_file")
    _pass(db_conn, registered_worker, profile)
    _fail(db_conn, registered_worker, profile, evidence_ref="ev2")
    _hard_disqualified(db_conn, registered_worker, profile, evidence_ref="ev3")
    after = manager.current_trust(registered_worker, "coder", "read_file")
    assert before == after == TrustLevel.LOCKED


# -- compatibility-normalizer identity is exact, not a wildcard --------------


def test_native_and_normalized_runtime_identities_do_not_match():
    native = RuntimeProfileIdentity(
        model_tag="m",
        model_digest="d",
        endpoint="e",
        runtime_version="v",
    )
    normalized = RuntimeProfileIdentity(
        model_tag="m",
        model_digest="d",
        endpoint="e",
        runtime_version="v",
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
    )
    assert not native.matches(normalized)
    assert not normalized.matches(native)
    assert native.matches(
        RuntimeProfileIdentity(
            model_tag="m",
            model_digest="d",
            endpoint="e",
            runtime_version="v",
        )
    )


def test_incomplete_normalizer_pair_is_refused_at_construction():
    with pytest.raises(ValueError, match="both be set"):
        RuntimeProfileIdentity(
            model_tag="m",
            normalizer_id="qwen_textual_tool_v1",
        )
    with pytest.raises(ValueError, match="both be set"):
        RuntimeProfileIdentity(model_tag="m", normalizer_version=1)
    with pytest.raises(ValueError):
        RuntimeProfileIdentity(
            model_tag="m",
            normalizer_id="qwen_textual_tool_v1",
            normalizer_version=0,
        )


def test_certificate_persists_normalizer_identity(db_conn, registered_worker):
    profile = RuntimeProfileIdentity(
        model_tag="m",
        model_digest="d",
        endpoint="e",
        runtime_version="v",
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
    )
    result = _pass(db_conn, registered_worker, profile)
    assert result.ok
    assert result.certificate.normalizer_id == "qwen_textual_tool_v1"
    assert result.certificate.normalizer_version == 1
    native = _pass(
        db_conn,
        registered_worker,
        RuntimeProfileIdentity(
            model_tag="m",
            model_digest="d",
            endpoint="e",
            runtime_version="v",
        ),
        evidence_ref="evidence-native",
    )
    assert native.certificate.normalizer_id is None
    assert native.certificate.normalizer_version is None


# -- runtime-config fingerprint / spec ---------------------------------------


def _spec_kwargs(**overrides) -> dict:
    kwargs = dict(
        model_tag="qwen3-coder-ctx16k:30b",
        model_digest="sha256:abc",
        endpoint="http://192.168.32.8:11434/v1",
        runtime_version="0.16.1",
        normalizer_id="qwen_textual_tool_v1",
        normalizer_version=1,
        effective_context_tokens=16384,
        output_token_budget=4096,
        temperature=0.0,
        tool_choice_enforcement="ADVISORY_ONLY_UNVERIFIED",
    )
    kwargs.update(overrides)
    return kwargs


def test_runtime_config_spec_omits_operational_only_timeout():
    spec = canonical_runtime_config_spec(**_spec_kwargs())
    assert spec["spec_version"] == RUNTIME_CONFIG_SPEC_VERSION
    assert "timeout" not in spec
    assert "safety_margin_tokens" not in spec
    assert spec["temperature"] == 0.0
    assert spec["effective_context_tokens"] == 16384
    assert spec["output_token_budget"] == 4096
    assert spec["normalizer_id"] == "qwen_textual_tool_v1"
    assert spec["normalizer_version"] == 1


def test_runtime_config_fingerprint_is_stable_and_canonicalizes_int_temperature():
    a = fingerprint_runtime_config(canonical_runtime_config_spec(**_spec_kwargs(temperature=0)))
    b = fingerprint_runtime_config(canonical_runtime_config_spec(**_spec_kwargs(temperature=0.0)))
    c = runtime_profile_identity_from_config(**_spec_kwargs(temperature=0.0))
    assert a == b == c.runtime_config_fingerprint
    assert len(a) == 64
    assert a == a.lower()


def test_runtime_config_fingerprint_changes_with_temperature_context_budget():
    base = fingerprint_runtime_config(canonical_runtime_config_spec(**_spec_kwargs()))
    hot = fingerprint_runtime_config(canonical_runtime_config_spec(**_spec_kwargs(temperature=1.5)))
    ctx = fingerprint_runtime_config(
        canonical_runtime_config_spec(**_spec_kwargs(effective_context_tokens=8192)),
    )
    budget = fingerprint_runtime_config(
        canonical_runtime_config_spec(**_spec_kwargs(output_token_budget=1024)),
    )
    native = fingerprint_runtime_config(
        canonical_runtime_config_spec(**_spec_kwargs(normalizer_id=None, normalizer_version=None)),
    )
    assert len({base, hot, ctx, budget, native}) == 5


def test_runtime_config_fingerprint_rejects_unsupported_spec_version():
    spec = canonical_runtime_config_spec(**_spec_kwargs())
    spec["spec_version"] = "runtime-config-spec-v2"
    with pytest.raises(ValueError, match="spec_version"):
        fingerprint_runtime_config(spec)
    with pytest.raises(TypeError):
        fingerprint_runtime_config("not-a-dict")


def test_malformed_runtime_config_fingerprint_is_refused_at_construction():
    with pytest.raises(ValueError, match="sha256"):
        RuntimeProfileIdentity(model_tag="m", runtime_config_fingerprint="")
    with pytest.raises(ValueError, match="sha256"):
        RuntimeProfileIdentity(model_tag="m", runtime_config_fingerprint="abc")
    with pytest.raises(ValueError, match="sha256"):
        RuntimeProfileIdentity(model_tag="m", runtime_config_fingerprint="A" * 64)
    with pytest.raises(ValueError, match="temperature"):
        canonical_runtime_config_spec(**_spec_kwargs(temperature=None))
    with pytest.raises(ValueError, match="temperature"):
        canonical_runtime_config_spec(**_spec_kwargs(temperature=True))
    with pytest.raises(ValueError, match="temperature"):
        canonical_runtime_config_spec(**_spec_kwargs(temperature=2.1))


def test_none_fingerprint_never_matches_a_set_fingerprint():
    fingerprinted = runtime_profile_identity_from_config(**_spec_kwargs())
    legacy = RuntimeProfileIdentity(
        model_tag=fingerprinted.model_tag,
        model_digest=fingerprinted.model_digest,
        endpoint=fingerprinted.endpoint,
        runtime_version=fingerprinted.runtime_version,
        normalizer_id=fingerprinted.normalizer_id,
        normalizer_version=fingerprinted.normalizer_version,
        runtime_config_fingerprint=None,
    )
    assert not fingerprinted.matches(legacy)
    assert not legacy.matches(fingerprinted)
    assert fingerprinted.is_fully_specified
    assert not legacy.is_fully_specified


def test_certificate_persists_runtime_config_fingerprint(db_conn, registered_worker):
    profile = runtime_profile_identity_from_config(**_spec_kwargs())
    result = _pass(db_conn, registered_worker, profile)
    assert result.ok
    assert result.certificate.runtime_config_fingerprint == profile.runtime_config_fingerprint
    incomplete = _pass(
        db_conn,
        registered_worker,
        RuntimeProfileIdentity(model_tag="m"),
        evidence_ref="evidence-incomplete",
    )
    assert incomplete.certificate.runtime_config_fingerprint is None


def test_factory_never_accepts_a_caller_supplied_fingerprint():
    """The production-grade constructor derives the fingerprint; there
    is no parameter that would let a caller (or a model) assert one."""
    import inspect

    params = set(inspect.signature(runtime_profile_identity_from_config).parameters)
    assert "runtime_config_fingerprint" not in params
