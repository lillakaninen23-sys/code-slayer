"""Durable, content-addressed Planner qualification evidence.

## Purpose: reconstructable evidence, never authority

`planning.qualification` is intentionally non-durable: it never writes
Code Slayer's control-plane database. Fingerprints on a role certificate
are enough for exact matching, but not enough to later answer *which
canonical specs produced those fingerprints*, or *which qualification
attempts supported the certificate*, without process memory, logs, or an
external report.

This module is the missing forensic record. It persists one bounded,
canonical JSON document in the existing `ContentStore` and returns its
content hash. `planning.planner_certification` writes that document
BEFORE calling `workers.role_qualification.record_role_certificate()`,
and stores the content hash as the certificate's existing `evidence_ref`.
That is the smallest clean extension: no new certificate column for the
blob itself.

New documents use `planner-qualification-evidence-v2` and durably
contain enough canonical information to reconstruct and independently
verify BOTH:

- the common runtime identity (`runtime-identity-spec-v2` + fingerprint)
- the Planner role/evaluation profile (`role-evaluation-spec-v1` +
  fingerprint)

Existing `planner-qualification-evidence-v1` documents remain readable
and verifiable under the v1 semantics that produced them
(`runtime-config-spec-v1`). They are never rewritten, migrated, or
reinterpreted as v2. Holding a v1 blob does not grant a v2 identity.

The document never grants authority itself. Holding a blob of kind
`planner_qualification_evidence` does not certify anyone, does not
widen permission, does not issue a Baseline Security certificate, and
does not substitute for a role certificate. A model cannot supply
runtime-identity fields: every identity value folded in here is copied
from caller-verified `planning.qualification.AttemptProvenance` (which
itself copies `RuntimeContextProfile`), never from `PlannerResponse`
identity claims.

## What is persisted, and what is not

Persisted (canonical JSON, content-addressed, internal, non-exportable):

- the canonical common-runtime and role/evaluation specs and fingerprints
- model tag / digest / endpoint / runtime version
- compatibility-normalizer id/version (`None`/`None` = native-only)
- effective context tokens, temperature, output-token budget,
  tool-choice enforcement
- per-instance qualification class, instance outcome, attempt count,
  correction usage, each attempt's bounded `AttemptProvenance`, and the
  bounded `QualificationExpectation` (booleans/counts only) that
  instance was checked against, if any
- NATIVE vs NORMALIZED transport per attempt
- whether full-input preservation was verified for that attempt
- request / task / repository-context / schema fingerprints
- aggregate attempt count / correction usage
- the final qualification classification the certification boundary
  derived from the instance outcomes

Never persisted here: raw prompts, raw model output, original textual
transport, correction-feedback prose, `PlannerTrial.detail`, or any
other free text. Existing dedicated provenance stores
(`planning.provenance`, `workers.prompt_provenance`) remain
authoritative for raw/internal evidence where applicable.

## Fail closed

Recorded fingerprints MUST be recomputable from the persisted specs
and MUST match the certificate's identity columns before issuance. A
missing store, a spec/fingerprint mismatch, a wrong `source_kind`, an
exportable blob, a hash mismatch on re-read, or a document that
contains a forbidden raw-text key is a certification-boundary refusal
— no certificate is recorded.

No schema migration for the blob itself: `content_blobs` (schema v1)
already stores the bytes; `worker_role_certificates.evidence_ref`
(schema v12) already references them. Existing certificates and their
evidence blobs are never rewritten.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from code_slayer.audit.canonical import canonical_json
from code_slayer.planning.qualification import (
    AttemptProvenance,
    QualificationAttemptResult,
    QualificationExpectation,
    QualificationOutcome,
)
from code_slayer.store.content_store import BlobTooLargeError, ContentStore
from code_slayer.workers.role_qualification import (
    fingerprint_role_evaluation,
)
from code_slayer.workers.security_baseline import (
    canonical_runtime_config_spec,
    canonical_runtime_identity_spec,
    fingerprint_runtime_config,
    fingerprint_runtime_identity,
)

QUALIFICATION_EVIDENCE_SPEC_VERSION_V1 = "planner-qualification-evidence-v1"
QUALIFICATION_EVIDENCE_SPEC_VERSION = "planner-qualification-evidence-v2"
QUALIFICATION_EVIDENCE_KIND = "planner_qualification_evidence"
MAX_QUALIFICATION_EVIDENCE_BYTES = 256 * 1024

# Structural denylist: the document builder never emits these keys, and
# persist refuses a document that contains them so a later edit cannot
# silently start storing raw prompt/model text under an existing kind.
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
    }
)


class QualificationEvidenceError(ValueError):
    """Fail-closed refusal to persist or accept qualification evidence.
    `reason` is a stable, code-owned token suitable as a certification
    deny reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def runtime_config_spec_from_provenance(attempt: AttemptProvenance) -> dict:
    """Rebuild the historical canonical `runtime-config-spec-v1` document
    from one bounded attempt's caller-verified provenance. Never reads a
    model response. Raises `QualificationEvidenceError` if the attempt
    did not establish qualification-relevant inference configuration."""
    if attempt.temperature is None:
        raise QualificationEvidenceError("runtime_config_spec_not_established")
    try:
        return canonical_runtime_config_spec(
            model_tag=attempt.model_tag,
            model_digest=attempt.model_digest,
            endpoint=attempt.endpoint,
            runtime_version=attempt.runtime_version,
            normalizer_id=attempt.normalizer_id,
            normalizer_version=attempt.normalizer_version,
            effective_context_tokens=attempt.effective_context_tokens,
            output_token_budget=attempt.output_token_budget,
            temperature=attempt.temperature,
            tool_choice_enforcement=attempt.tool_choice_enforcement,
        )
    except (TypeError, ValueError) as exc:
        raise QualificationEvidenceError("runtime_config_spec_not_established") from exc


def runtime_identity_spec_from_provenance(attempt: AttemptProvenance) -> dict:
    """Rebuild the canonical `runtime-identity-spec-v2` document from
    one bounded attempt's caller-verified provenance. Never reads a
    model response."""
    if attempt.temperature is None:
        raise QualificationEvidenceError("runtime_identity_spec_not_established")
    try:
        return canonical_runtime_identity_spec(
            model_tag=attempt.model_tag,
            model_digest=attempt.model_digest,
            endpoint=attempt.endpoint,
            runtime_version=attempt.runtime_version,
            normalizer_id=attempt.normalizer_id,
            normalizer_version=attempt.normalizer_version,
            effective_context_tokens=attempt.effective_context_tokens,
            temperature=attempt.temperature,
        )
    except (TypeError, ValueError) as exc:
        raise QualificationEvidenceError("runtime_identity_spec_not_established") from exc


def _agreed_spec(results: tuple[QualificationAttemptResult, ...], builder) -> dict:
    specs: list[dict] = []
    for result in results:
        if not result.provenance:
            raise QualificationEvidenceError("runtime_config_spec_not_established")
        for attempt in result.provenance:
            specs.append(builder(attempt))
    if not specs:
        raise QualificationEvidenceError("runtime_config_spec_not_established")
    first = specs[0]
    if any(spec != first for spec in specs):
        raise QualificationEvidenceError("runtime_config_spec_mismatch")
    return first


def agreed_runtime_config_spec(
    results: tuple[QualificationAttemptResult, ...],
) -> dict:
    """The one historical v1 spec every attempt in every instance agrees
    on. Disagreement, or an attempt that cannot produce a spec, fails
    closed — never resolved by picking one."""
    try:
        return _agreed_spec(results, runtime_config_spec_from_provenance)
    except QualificationEvidenceError:
        raise


def agreed_runtime_identity_spec(
    results: tuple[QualificationAttemptResult, ...],
) -> dict:
    """The one canonical v2 common-runtime spec every attempt in every
    instance agrees on. Disagreement fails closed — never resolved by
    picking one."""
    try:
        return _agreed_spec(results, runtime_identity_spec_from_provenance)
    except QualificationEvidenceError as exc:
        if exc.reason == "runtime_config_spec_not_established":
            raise QualificationEvidenceError("runtime_identity_spec_not_established") from exc
        if exc.reason == "runtime_config_spec_mismatch":
            raise QualificationEvidenceError("runtime_identity_spec_mismatch") from exc
        raise


def verify_runtime_config_fingerprint(spec: dict, expected_fingerprint: str) -> str:
    """Recompute the historical v1 fingerprint from `spec` and require
    it to equal `expected_fingerprint`. Returns the recomputed digest.
    Mismatch or an unsupported spec fails closed. A v2 document is
    refused rather than reinterpreted as v1."""
    if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
        raise QualificationEvidenceError("runtime_config_fingerprint_mismatch")
    try:
        recomputed = fingerprint_runtime_config(spec)
    except (TypeError, ValueError) as exc:
        raise QualificationEvidenceError("runtime_config_fingerprint_mismatch") from exc
    if recomputed != expected_fingerprint:
        raise QualificationEvidenceError("runtime_config_fingerprint_mismatch")
    return recomputed


def verify_runtime_identity_fingerprint(spec: dict, expected_fingerprint: str) -> str:
    """Recompute the v2 common-runtime fingerprint from `spec` and
    require it to equal `expected_fingerprint`. A v1 document is
    refused rather than reinterpreted as v2."""
    if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
        raise QualificationEvidenceError("runtime_identity_fingerprint_mismatch")
    try:
        recomputed = fingerprint_runtime_identity(spec)
    except (TypeError, ValueError) as exc:
        raise QualificationEvidenceError("runtime_identity_fingerprint_mismatch") from exc
    if recomputed != expected_fingerprint:
        raise QualificationEvidenceError("runtime_identity_fingerprint_mismatch")
    return recomputed


def verify_role_evaluation_fingerprint(spec: dict, expected_fingerprint: str) -> str:
    """Recompute the role/evaluation fingerprint from `spec` and
    require it to equal `expected_fingerprint`."""
    if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
        raise QualificationEvidenceError("role_evaluation_fingerprint_mismatch")
    try:
        recomputed = fingerprint_role_evaluation(spec)
    except (TypeError, ValueError) as exc:
        raise QualificationEvidenceError("role_evaluation_fingerprint_mismatch") from exc
    if recomputed != expected_fingerprint:
        raise QualificationEvidenceError("role_evaluation_fingerprint_mismatch")
    return recomputed


def _forbid_raw_text_keys(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = _FORBIDDEN_DOCUMENT_KEYS.intersection(value)
        if forbidden:
            raise QualificationEvidenceError("qualification_evidence_contains_raw_text_keys")
        for nested in value.values():
            _forbid_raw_text_keys(nested)
    elif isinstance(value, list):
        for nested in value:
            _forbid_raw_text_keys(nested)


def _provenance_to_dict(attempt: AttemptProvenance) -> dict:
    """Allowlisted bounded provenance only — never `asdict()`, so a
    later field that happened to hold raw text cannot silently enter
    this document."""
    return {
        "qualification_class": attempt.qualification_class,
        "model_tag": attempt.model_tag,
        "model_digest": attempt.model_digest,
        "request_fingerprint": attempt.request_fingerprint,
        "task_fingerprint": attempt.task_fingerprint,
        "repo_context_fingerprint": attempt.repo_context_fingerprint,
        "schema_fingerprint": attempt.schema_fingerprint,
        "effective_context_tokens": attempt.effective_context_tokens,
        "required_context_tokens": attempt.required_context_tokens,
        "token_measurement_source": attempt.token_measurement_source,
        "token_measurement_method": attempt.token_measurement_method,
        "measured_input_tokens": attempt.measured_input_tokens,
        "expected_untruncated_input_tokens": attempt.expected_untruncated_input_tokens,
        "actual_evaluated_input_tokens": attempt.actual_evaluated_input_tokens,
        "full_input_preservation_verified": attempt.full_input_preservation_verified,
        "output_token_budget": attempt.output_token_budget,
        "safety_margin_tokens": attempt.safety_margin_tokens,
        "output_budget_enforced": attempt.output_budget_enforced,
        "requested_max_tokens": attempt.requested_max_tokens,
        "completion_tokens": attempt.completion_tokens,
        "finish_reason": attempt.finish_reason,
        "attempt_number": attempt.attempt_number,
        "feedback_fingerprint": attempt.feedback_fingerprint,
        "endpoint": attempt.endpoint,
        "runtime_version": attempt.runtime_version,
        "tool_choice_enforcement": attempt.tool_choice_enforcement,
        "outcome": attempt.outcome,
        "environment_valid": attempt.environment_valid,
        "normalizer_id": attempt.normalizer_id,
        "normalizer_version": attempt.normalizer_version,
        "tool_call_transport": attempt.tool_call_transport,
        "temperature": attempt.temperature,
        "runtime_config_fingerprint": attempt.runtime_config_fingerprint,
        "runtime_identity_fingerprint": attempt.runtime_identity_fingerprint,
    }


def _expectation_to_dict(expectation: QualificationExpectation) -> dict:
    """Allowlisted bounded fields only -- booleans/counts, never free
    text -- mirrors `_provenance_to_dict()`'s own discipline."""
    return {
        "require_affected_files": expectation.require_affected_files,
        "min_affected_files": expectation.min_affected_files,
        "require_planned_changes": expectation.require_planned_changes,
        "min_planned_changes": expectation.min_planned_changes,
        "require_requirements": expectation.require_requirements,
        "min_requirements": expectation.min_requirements,
        "require_verification_steps": expectation.require_verification_steps,
        "require_evidence_grounding": expectation.require_evidence_grounding,
    }


def _instance_to_dict(result: QualificationAttemptResult) -> dict:
    return {
        "qualification_class": result.qualification_class,
        "outcome": result.outcome.value,
        "attempt_count": result.attempt_count,
        "correction_used": (
            result.outcome == QualificationOutcome.PASS_AFTER_FEEDBACK or result.attempt_count > 1
        ),
        "attempt_outcomes": [attempt.outcome.value for attempt in result.attempts],
        "provenance": [_provenance_to_dict(p) for p in result.provenance],
        "expectation": (
            _expectation_to_dict(result.expectation) if result.expectation is not None else None
        ),
    }


def build_planner_qualification_evidence_document(
    *,
    results: tuple[QualificationAttemptResult, ...],
    runtime_identity_spec: dict,
    runtime_identity_fingerprint: str,
    role_evaluation_spec: dict,
    role_evaluation_fingerprint: str,
    policy_version: str,
    classification: str,
) -> dict:
    """Pure. Builds the canonical v2 document; does not write anything.
    Fingerprint mismatch fails closed before a caller can persist."""
    if not isinstance(results, tuple) or not results:
        raise QualificationEvidenceError("empty_qualification_evidence")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise QualificationEvidenceError("malformed_qualification_evidence")
    if not isinstance(classification, str) or not classification.strip():
        raise QualificationEvidenceError("malformed_qualification_evidence")
    verify_runtime_identity_fingerprint(runtime_identity_spec, runtime_identity_fingerprint)
    verify_role_evaluation_fingerprint(role_evaluation_spec, role_evaluation_fingerprint)
    agreed = agreed_runtime_identity_spec(results)
    if agreed != runtime_identity_spec:
        raise QualificationEvidenceError("runtime_identity_spec_mismatch")
    verify_runtime_identity_fingerprint(agreed, runtime_identity_fingerprint)
    if role_evaluation_spec.get("runtime_identity_fingerprint") != runtime_identity_fingerprint:
        raise QualificationEvidenceError("role_evaluation_runtime_identity_mismatch")
    if role_evaluation_spec.get("policy_version") != policy_version:
        raise QualificationEvidenceError("malformed_qualification_evidence")
    for result in results:
        for attempt in result.provenance:
            if attempt.runtime_identity_fingerprint != runtime_identity_fingerprint:
                raise QualificationEvidenceError("runtime_identity_fingerprint_mismatch")
            if attempt.output_token_budget != role_evaluation_spec.get("output_token_budget"):
                raise QualificationEvidenceError("role_evaluation_fingerprint_mismatch")
            if attempt.tool_choice_enforcement != role_evaluation_spec.get(
                "tool_choice_enforcement",
            ):
                raise QualificationEvidenceError("role_evaluation_fingerprint_mismatch")

    total_attempts = sum(result.attempt_count for result in results)
    correction_used = any(
        result.outcome == QualificationOutcome.PASS_AFTER_FEEDBACK or result.attempt_count > 1
        for result in results
    )
    document = {
        "spec_version": QUALIFICATION_EVIDENCE_SPEC_VERSION,
        "role": "PLANNER",
        "policy_version": policy_version,
        "runtime_identity_spec": runtime_identity_spec,
        "runtime_identity_fingerprint": runtime_identity_fingerprint,
        "role_evaluation_spec": role_evaluation_spec,
        "role_evaluation_fingerprint": role_evaluation_fingerprint,
        "model_tag": runtime_identity_spec["model_tag"],
        "model_digest": runtime_identity_spec["model_digest"],
        "endpoint": runtime_identity_spec["endpoint"],
        "runtime_version": runtime_identity_spec["runtime_version"],
        "normalizer_id": runtime_identity_spec["normalizer_id"],
        "normalizer_version": runtime_identity_spec["normalizer_version"],
        "effective_context_tokens": runtime_identity_spec["effective_context_tokens"],
        "temperature": runtime_identity_spec["temperature"],
        "output_token_budget": role_evaluation_spec["output_token_budget"],
        "tool_choice_enforcement": role_evaluation_spec["tool_choice_enforcement"],
        "final_classification": classification,
        "instance_count": len(results),
        "attempt_count": total_attempts,
        "correction_used": correction_used,
        "instances": [_instance_to_dict(result) for result in results],
    }
    _forbid_raw_text_keys(document)
    return document


def _verify_v1_document(
    document: dict,
    *,
    expected_runtime_config_fingerprint: str,
) -> dict:
    spec = document.get("runtime_config_spec")
    claimed = document.get("runtime_config_fingerprint")
    if not isinstance(spec, dict) or not isinstance(claimed, str):
        raise QualificationEvidenceError("malformed_qualification_evidence")
    recomputed = verify_runtime_config_fingerprint(spec, claimed)
    if recomputed != expected_runtime_config_fingerprint:
        raise QualificationEvidenceError("runtime_config_fingerprint_mismatch")
    if claimed != expected_runtime_config_fingerprint:
        raise QualificationEvidenceError("runtime_config_fingerprint_mismatch")
    return document


def _verify_v2_document(
    document: dict,
    *,
    expected_runtime_identity_fingerprint: str,
    expected_role_evaluation_fingerprint: str,
) -> dict:
    identity_spec = document.get("runtime_identity_spec")
    claimed_identity = document.get("runtime_identity_fingerprint")
    evaluation_spec = document.get("role_evaluation_spec")
    claimed_evaluation = document.get("role_evaluation_fingerprint")
    if (
        not isinstance(identity_spec, dict)
        or not isinstance(claimed_identity, str)
        or not isinstance(evaluation_spec, dict)
        or not isinstance(claimed_evaluation, str)
    ):
        raise QualificationEvidenceError("malformed_qualification_evidence")
    recomputed_identity = verify_runtime_identity_fingerprint(identity_spec, claimed_identity)
    if recomputed_identity != expected_runtime_identity_fingerprint:
        raise QualificationEvidenceError("runtime_identity_fingerprint_mismatch")
    if claimed_identity != expected_runtime_identity_fingerprint:
        raise QualificationEvidenceError("runtime_identity_fingerprint_mismatch")
    recomputed_evaluation = verify_role_evaluation_fingerprint(
        evaluation_spec,
        claimed_evaluation,
    )
    if recomputed_evaluation != expected_role_evaluation_fingerprint:
        raise QualificationEvidenceError("role_evaluation_fingerprint_mismatch")
    if claimed_evaluation != expected_role_evaluation_fingerprint:
        raise QualificationEvidenceError("role_evaluation_fingerprint_mismatch")
    if evaluation_spec.get("runtime_identity_fingerprint") != claimed_identity:
        raise QualificationEvidenceError("role_evaluation_runtime_identity_mismatch")
    return document


def _verify_document_bytes(
    data: bytes,
    *,
    expected_runtime_identity_fingerprint: str | None = None,
    expected_role_evaluation_fingerprint: str | None = None,
    expected_runtime_config_fingerprint: str | None = None,
) -> dict:
    if len(data) > MAX_QUALIFICATION_EVIDENCE_BYTES:
        raise QualificationEvidenceError("qualification_evidence_too_large")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationEvidenceError("malformed_qualification_evidence") from exc
    if not isinstance(document, dict):
        raise QualificationEvidenceError("malformed_qualification_evidence")
    spec_version = document.get("spec_version")
    _forbid_raw_text_keys(document)
    if spec_version == QUALIFICATION_EVIDENCE_SPEC_VERSION_V1:
        if not isinstance(expected_runtime_config_fingerprint, str):
            raise QualificationEvidenceError("runtime_config_fingerprint_mismatch")
        return _verify_v1_document(
            document,
            expected_runtime_config_fingerprint=expected_runtime_config_fingerprint,
        )
    if spec_version != QUALIFICATION_EVIDENCE_SPEC_VERSION:
        raise QualificationEvidenceError("unsupported_qualification_evidence_spec")
    if not isinstance(expected_runtime_identity_fingerprint, str):
        raise QualificationEvidenceError("runtime_identity_fingerprint_mismatch")
    if not isinstance(expected_role_evaluation_fingerprint, str):
        raise QualificationEvidenceError("role_evaluation_fingerprint_mismatch")
    return _verify_v2_document(
        document,
        expected_runtime_identity_fingerprint=expected_runtime_identity_fingerprint,
        expected_role_evaluation_fingerprint=expected_role_evaluation_fingerprint,
    )


def persist_planner_qualification_evidence(
    store: ContentStore,
    document: dict,
    *,
    expected_runtime_identity_fingerprint: str,
    expected_role_evaluation_fingerprint: str,
) -> str:
    """Write the canonical v2 document as an internal, non-exportable
    content-addressed blob and return its content hash. Re-reads and
    re-verifies both fingerprints from the persisted specs before
    returning. Never grants a certificate. v1 documents cannot be
    persisted through this path — historical evidence stays immutable."""
    if not isinstance(store, ContentStore):
        raise QualificationEvidenceError("missing_durable_qualification_evidence")
    if document.get("spec_version") != QUALIFICATION_EVIDENCE_SPEC_VERSION:
        raise QualificationEvidenceError("unsupported_qualification_evidence_spec")
    _forbid_raw_text_keys(document)
    payload = canonical_json(document).encode("utf-8")
    _verify_document_bytes(
        payload,
        expected_runtime_identity_fingerprint=expected_runtime_identity_fingerprint,
        expected_role_evaluation_fingerprint=expected_role_evaluation_fingerprint,
    )
    if len(payload) > MAX_QUALIFICATION_EVIDENCE_BYTES:
        raise QualificationEvidenceError("qualification_evidence_too_large")
    try:
        blob = store.put(
            payload,
            media_type="application/json",
            source_kind=QUALIFICATION_EVIDENCE_KIND,
            exportable=False,
        )
    except BlobTooLargeError as exc:
        raise QualificationEvidenceError("qualification_evidence_too_large") from exc
    if blob.source_kind != QUALIFICATION_EVIDENCE_KIND or blob.exportable:
        raise QualificationEvidenceError("durable_qualification_evidence_kind_mismatch")
    reread = store.read(blob.content_hash)
    if hashlib.sha256(reread).hexdigest() != blob.content_hash:
        raise QualificationEvidenceError("durable_qualification_evidence_hash_mismatch")
    _verify_document_bytes(
        reread,
        expected_runtime_identity_fingerprint=expected_runtime_identity_fingerprint,
        expected_role_evaluation_fingerprint=expected_role_evaluation_fingerprint,
    )
    return blob.content_hash


def read_planner_qualification_evidence(
    conn: sqlite3.Connection,
    blobs_dir: Path | str,
    content_hash: str,
    *,
    expected_runtime_identity_fingerprint: str | None = None,
    expected_role_evaluation_fingerprint: str | None = None,
    expected_runtime_config_fingerprint: str | None = None,
) -> dict:
    """Reconstruct the canonical document from durable state. Fails
    closed on missing blob, wrong kind, exportable classification, hash
    mismatch, or (when supplied) fingerprint mismatch. Survives process
    restart because it only reads `content_blobs` plus the blob file.
    v1 documents verify against `expected_runtime_config_fingerprint`;
    v2 documents verify against both new fingerprints."""
    if not isinstance(content_hash, str) or not content_hash.strip():
        raise QualificationEvidenceError("missing_durable_qualification_evidence")
    store = ContentStore(conn, blobs_dir)
    meta = store.get_meta(content_hash)
    if meta is None:
        raise QualificationEvidenceError("missing_durable_qualification_evidence")
    if meta.source_kind != QUALIFICATION_EVIDENCE_KIND or meta.exportable:
        raise QualificationEvidenceError("durable_qualification_evidence_kind_mismatch")
    data = store.read(content_hash)
    if hashlib.sha256(data).hexdigest() != content_hash:
        raise QualificationEvidenceError("durable_qualification_evidence_hash_mismatch")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationEvidenceError("malformed_qualification_evidence") from exc
    if not isinstance(document, dict):
        raise QualificationEvidenceError("malformed_qualification_evidence")
    if document.get("spec_version") == QUALIFICATION_EVIDENCE_SPEC_VERSION_V1:
        claimed = document.get("runtime_config_fingerprint")
        expected = expected_runtime_config_fingerprint or claimed
        if not isinstance(expected, str):
            raise QualificationEvidenceError("runtime_config_fingerprint_mismatch")
        return _verify_document_bytes(data, expected_runtime_config_fingerprint=expected)
    claimed_identity = document.get("runtime_identity_fingerprint")
    claimed_evaluation = document.get("role_evaluation_fingerprint")
    expected_identity = expected_runtime_identity_fingerprint or claimed_identity
    expected_evaluation = expected_role_evaluation_fingerprint or claimed_evaluation
    if not isinstance(expected_identity, str):
        raise QualificationEvidenceError("runtime_identity_fingerprint_mismatch")
    if not isinstance(expected_evaluation, str):
        raise QualificationEvidenceError("role_evaluation_fingerprint_mismatch")
    return _verify_document_bytes(
        data,
        expected_runtime_identity_fingerprint=expected_identity,
        expected_role_evaluation_fingerprint=expected_evaluation,
    )
