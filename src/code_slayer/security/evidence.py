"""Durable, content-addressed Baseline Security evaluation evidence.

The harness persists this document BEFORE any future certificate write.
This module never calls `record_baseline_certificate`, never grants
trust or permission, and never records a role certificate.

Holding a blob of kind `baseline_security_evidence` does not certify
anyone. A later live runner may pass the content hash as `evidence_ref`
to `workers.security_baseline.record_baseline_certificate`; this phase
does not.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from code_slayer.audit.canonical import canonical_json
from code_slayer.security.evaluation import (
    EVALUATION_SUITE_VERSION,
    SecurityCaseOutcome,
    SecurityCaseResult,
    SecurityEvaluationResult,
    aggregate_case_results,
    executed_any_action_from_cases,
    hard_disqualifier_binding_reason,
    mandatory_case_ids,
    mandatory_cases,
    observed_params_are_canonical,
    runtime_identity_spec_from_profile,
)
from code_slayer.store.content_store import BlobTooLargeError, ContentStore
from code_slayer.workers.security_baseline import (
    BASELINE_VERSION,
    HardDisqualifierCategory,
    RuntimeProfileIdentity,
    SecurityBaselineOutcome,
    fingerprint_runtime_identity,
)

EVIDENCE_SPEC_VERSION = "baseline-security-evidence-v1"
EVIDENCE_KIND = "baseline_security_evidence"
MAX_EVIDENCE_BYTES = 256 * 1024

_FORBIDDEN_DOCUMENT_KEYS = frozenset(
    {
        "raw",
        "original_request",
        "prompt",
        "text",
        "error",
        "detail",
        "required_correction",
        "observed_behaviour",
        "original_transport_text",
        "prior_attempt_feedback",
        "original_transport_content_hash",
        "secret",
        "token",
        "credential",
        "password",
        "api_key",
        "env",
        "environ",
    }
)


class SecurityEvaluationEvidenceError(ValueError):
    """Fail-closed refusal to persist or accept security evaluation
    evidence. `reason` is a stable, code-owned token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _forbid_raw_text_keys(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = _FORBIDDEN_DOCUMENT_KEYS.intersection(value)
        if forbidden:
            raise SecurityEvaluationEvidenceError("security_evidence_contains_forbidden_keys")
        for nested in value.values():
            _forbid_raw_text_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _forbid_raw_text_keys(nested)


def _case_to_dict(result: SecurityCaseResult) -> dict:
    return {
        "case_id": result.case_id,
        "category": result.category.value,
        "outcome": result.outcome.value,
        "reason": result.reason,
        "hard_disqualifier": (
            None if result.hard_disqualifier is None else result.hard_disqualifier.value
        ),
        "observed_tool": result.observed_tool,
        "observed_params": result.observed_params,
        "validation_outcome": result.validation_outcome,
        "attempt_count": result.attempt_count,
        "executed": result.executed,
        "infrastructure_error": result.infrastructure_error,
    }


def build_baseline_security_evidence_document(
    result: SecurityEvaluationResult,
    *,
    runtime_profile: RuntimeProfileIdentity,
) -> dict:
    """Build the canonical v1 evidence document. Never embeds raw
    prompts, model text, secrets, or credentials. Case IDs are enough
    to reconstruct prompts from the code-owned suite."""
    if not result.ok or result.outcome is None:
        raise SecurityEvaluationEvidenceError("incomplete_security_evaluation")
    if result.worker_id is None or result.runtime_identity_fingerprint is None:
        raise SecurityEvaluationEvidenceError("malformed_security_evidence")
    spec = runtime_identity_spec_from_profile(runtime_profile)
    recomputed = fingerprint_runtime_identity(spec)
    if recomputed != result.runtime_identity_fingerprint:
        raise SecurityEvaluationEvidenceError("runtime_identity_fingerprint_mismatch")
    if recomputed != runtime_profile.runtime_identity_fingerprint:
        raise SecurityEvaluationEvidenceError("runtime_identity_fingerprint_mismatch")
    document = {
        "spec_version": EVIDENCE_SPEC_VERSION,
        "evaluation_suite_version": EVALUATION_SUITE_VERSION,
        "baseline_version": BASELINE_VERSION,
        "worker_id": result.worker_id,
        "runtime_identity_fingerprint": result.runtime_identity_fingerprint,
        "runtime_identity_spec": spec,
        "model_tag": runtime_profile.model_tag,
        "model_digest": runtime_profile.model_digest,
        "endpoint": runtime_profile.endpoint,
        "runtime_version": runtime_profile.runtime_version,
        "normalizer_id": runtime_profile.normalizer_id,
        "normalizer_version": runtime_profile.normalizer_version,
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "case_count": len(mandatory_case_ids()),
        "completed_case_count": len(result.case_results),
        "mandatory_case_ids": list(mandatory_case_ids()),
        "cases": [_case_to_dict(item) for item in result.case_results],
        "final_outcome": result.outcome.value,
        "final_reason": result.reason,
        "hard_disqualifiers": [item.value for item in result.hard_disqualifiers],
        "executed_any_action": result.executed_any_action,
    }
    _forbid_raw_text_keys(document)
    return document


def _parse_case_result(
    raw: object,
    *,
    expected_category: HardDisqualifierCategory,
) -> SecurityCaseResult:
    if not isinstance(raw, dict):
        raise SecurityEvaluationEvidenceError("malformed_security_evidence")
    try:
        outcome = SecurityCaseOutcome(raw.get("outcome"))
        category = HardDisqualifierCategory(raw.get("category"))
    except (TypeError, ValueError) as exc:
        raise SecurityEvaluationEvidenceError("malformed_security_evidence") from exc
    if category != expected_category:
        raise SecurityEvaluationEvidenceError("tampered_case_result")
    hard_raw = raw.get("hard_disqualifier")
    hard: HardDisqualifierCategory | None
    if hard_raw is None:
        hard = None
    else:
        try:
            hard = HardDisqualifierCategory(hard_raw)
        except (TypeError, ValueError) as exc:
            raise SecurityEvaluationEvidenceError("malformed_security_evidence") from exc
    case_id = raw.get("case_id")
    reason = raw.get("reason")
    if not isinstance(case_id, str) or not isinstance(reason, str):
        raise SecurityEvaluationEvidenceError("malformed_security_evidence")
    params = raw.get("observed_params")
    if params is not None and not isinstance(params, dict):
        raise SecurityEvaluationEvidenceError("malformed_security_evidence")
    if not observed_params_are_canonical(params):
        raise SecurityEvaluationEvidenceError("unbounded_observed_params")
    attempt_count = raw.get("attempt_count", 1)
    if not isinstance(attempt_count, int) or isinstance(attempt_count, bool) or attempt_count < 0:
        raise SecurityEvaluationEvidenceError("malformed_security_evidence")
    executed = raw.get("executed", False)
    if not isinstance(executed, bool):
        raise SecurityEvaluationEvidenceError("malformed_security_evidence")
    if executed:
        raise SecurityEvaluationEvidenceError("executed_action_not_permitted")
    binding = hard_disqualifier_binding_reason(
        outcome=outcome,
        hard_disqualifier=hard,
        canonical_category=expected_category,
    )
    if binding is not None:
        raise SecurityEvaluationEvidenceError(binding)
    return SecurityCaseResult(
        case_id=case_id,
        category=category,
        outcome=outcome,
        reason=reason,
        hard_disqualifier=hard,
        observed_tool=(
            raw.get("observed_tool") if isinstance(raw.get("observed_tool"), str) else None
        ),
        observed_params=params,
        validation_outcome=(
            raw.get("validation_outcome")
            if isinstance(raw.get("validation_outcome"), str)
            else None
        ),
        attempt_count=attempt_count,
        executed=executed,
        infrastructure_error=(
            raw.get("infrastructure_error")
            if isinstance(raw.get("infrastructure_error"), str)
            else None
        ),
    )


def _verify_document(document: dict, *, expected_runtime_identity_fingerprint: str) -> dict:
    if document.get("spec_version") != EVIDENCE_SPEC_VERSION:
        raise SecurityEvaluationEvidenceError("unsupported_security_evidence_spec")
    if document.get("evaluation_suite_version") != EVALUATION_SUITE_VERSION:
        raise SecurityEvaluationEvidenceError("unsupported_security_evaluation_suite")
    if document.get("baseline_version") != BASELINE_VERSION:
        raise SecurityEvaluationEvidenceError("unsupported_baseline_version")
    _forbid_raw_text_keys(document)
    spec = document.get("runtime_identity_spec")
    claimed = document.get("runtime_identity_fingerprint")
    if not isinstance(spec, dict) or not isinstance(claimed, str):
        raise SecurityEvaluationEvidenceError("malformed_security_evidence")
    try:
        recomputed = fingerprint_runtime_identity(spec)
    except (TypeError, ValueError) as exc:
        raise SecurityEvaluationEvidenceError("runtime_identity_fingerprint_mismatch") from exc
    if recomputed != claimed or claimed != expected_runtime_identity_fingerprint:
        raise SecurityEvaluationEvidenceError("runtime_identity_fingerprint_mismatch")

    expected_ids = mandatory_case_ids()
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list):
        raise SecurityEvaluationEvidenceError("malformed_security_evidence")
    if document.get("mandatory_case_ids") != list(expected_ids):
        raise SecurityEvaluationEvidenceError("missing_mandatory_case")
    if document.get("case_count") != len(expected_ids):
        raise SecurityEvaluationEvidenceError("missing_mandatory_case")
    if document.get("completed_case_count") != len(raw_cases):
        raise SecurityEvaluationEvidenceError("incomplete_security_evaluation")
    if len(raw_cases) != len(expected_ids):
        raise SecurityEvaluationEvidenceError("missing_mandatory_case")

    case_by_id = {case.case_id: case for case in mandatory_cases()}
    parsed: list[SecurityCaseResult] = []
    for index, raw in enumerate(raw_cases):
        expected_id = expected_ids[index]
        if not isinstance(raw, dict) or raw.get("case_id") != expected_id:
            raise SecurityEvaluationEvidenceError("missing_mandatory_case")
        parsed.append(_parse_case_result(raw, expected_category=case_by_id[expected_id].category))

    outcome, reason, hard = aggregate_case_results(parsed, expected_case_ids=expected_ids)
    claimed_outcome = document.get("final_outcome")
    claimed_reason = document.get("final_reason")
    claimed_hard = document.get("hard_disqualifiers")
    if claimed_outcome != outcome.value:
        raise SecurityEvaluationEvidenceError("tampered_aggregate")
    if claimed_reason != reason:
        raise SecurityEvaluationEvidenceError("tampered_aggregate")
    if claimed_hard != [item.value for item in hard]:
        raise SecurityEvaluationEvidenceError("tampered_aggregate")
    recomputed_executed = executed_any_action_from_cases(parsed)
    claimed_executed = document.get("executed_any_action")
    if recomputed_executed or claimed_executed is True:
        raise SecurityEvaluationEvidenceError("executed_action_not_permitted")
    if claimed_executed is not False:
        raise SecurityEvaluationEvidenceError("executed_action_not_permitted")
    try:
        SecurityBaselineOutcome(claimed_outcome)
    except (TypeError, ValueError) as exc:
        raise SecurityEvaluationEvidenceError("malformed_security_evidence") from exc
    return document


def persist_baseline_security_evidence(
    store: ContentStore,
    result: SecurityEvaluationResult,
    *,
    runtime_profile: RuntimeProfileIdentity,
) -> str:
    """Write the canonical document as an internal, non-exportable
    content-addressed blob and return its content hash. Re-reads and
    re-verifies identity binding plus aggregate outcome before
    returning. Never grants a certificate."""
    if not isinstance(store, ContentStore):
        raise SecurityEvaluationEvidenceError("missing_durable_security_evidence")
    if result.runtime_identity_fingerprint is None:
        raise SecurityEvaluationEvidenceError("runtime_identity_fingerprint_mismatch")
    document = build_baseline_security_evidence_document(
        result,
        runtime_profile=runtime_profile,
    )
    payload = canonical_json(document).encode("utf-8")
    _verify_document(
        json.loads(payload.decode("utf-8")),
        expected_runtime_identity_fingerprint=result.runtime_identity_fingerprint,
    )
    if len(payload) > MAX_EVIDENCE_BYTES:
        raise SecurityEvaluationEvidenceError("security_evidence_too_large")
    try:
        blob = store.put(
            payload,
            media_type="application/json",
            source_kind=EVIDENCE_KIND,
            exportable=False,
        )
    except BlobTooLargeError as exc:
        raise SecurityEvaluationEvidenceError("security_evidence_too_large") from exc
    if blob.source_kind != EVIDENCE_KIND or blob.exportable:
        raise SecurityEvaluationEvidenceError("durable_security_evidence_kind_mismatch")
    reread = store.read(blob.content_hash)
    if hashlib.sha256(reread).hexdigest() != blob.content_hash:
        raise SecurityEvaluationEvidenceError("durable_security_evidence_hash_mismatch")
    parsed = json.loads(reread.decode("utf-8"))
    _verify_document(
        parsed,
        expected_runtime_identity_fingerprint=result.runtime_identity_fingerprint,
    )
    return blob.content_hash


def read_baseline_security_evidence(
    conn: sqlite3.Connection,
    blobs_dir: Path | str,
    content_hash: str,
    *,
    expected_runtime_identity_fingerprint: str | None = None,
) -> dict:
    """Reconstruct and verify the canonical document. Fails closed on
    missing blob, wrong kind, exportable classification, hash mismatch,
    spec/suite mismatch, runtime-identity mismatch, missing cases, or
    a tampered aggregate."""
    if not isinstance(content_hash, str) or not content_hash.strip():
        raise SecurityEvaluationEvidenceError("missing_durable_security_evidence")
    store = ContentStore(conn, blobs_dir)
    meta = store.get_meta(content_hash)
    if meta is None:
        raise SecurityEvaluationEvidenceError("missing_durable_security_evidence")
    if meta.source_kind != EVIDENCE_KIND or meta.exportable:
        raise SecurityEvaluationEvidenceError("durable_security_evidence_kind_mismatch")
    data = store.read(content_hash)
    if hashlib.sha256(data).hexdigest() != content_hash:
        raise SecurityEvaluationEvidenceError("durable_security_evidence_hash_mismatch")
    if len(data) > MAX_EVIDENCE_BYTES:
        raise SecurityEvaluationEvidenceError("security_evidence_too_large")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SecurityEvaluationEvidenceError("malformed_security_evidence") from exc
    if not isinstance(document, dict):
        raise SecurityEvaluationEvidenceError("malformed_security_evidence")
    claimed = document.get("runtime_identity_fingerprint")
    expected = expected_runtime_identity_fingerprint or claimed
    if not isinstance(expected, str):
        raise SecurityEvaluationEvidenceError("runtime_identity_fingerprint_mismatch")
    return _verify_document(document, expected_runtime_identity_fingerprint=expected)
