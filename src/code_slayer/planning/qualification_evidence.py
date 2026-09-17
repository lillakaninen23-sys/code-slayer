"""Durable, content-addressed Planner qualification evidence.

## Purpose: reconstructable evidence, never authority

`planning.qualification` is intentionally non-durable: it never writes
Code Slayer's control-plane database. A one-way
`runtime_config_fingerprint` on a role certificate is enough for exact
matching, but not enough to later answer *which canonical runtime-config
spec produced that fingerprint*, or *which qualification attempts
supported the certificate*, without process memory, logs, or an
external report.

This module is the missing forensic record. It persists one bounded,
canonical JSON document in the existing `ContentStore` and returns its
content hash. `planning.planner_certification` writes that document
BEFORE calling `workers.role_qualification.record_role_certificate()`,
and stores the content hash as the certificate's existing `evidence_ref`.
That is the smallest clean extension: no new certificate column, no
rewrite of already-applied migrations, no change to parser acceptance,
permissions, trust, or Baseline Security certificates.

The document never grants authority itself. Holding a blob of kind
`planner_qualification_evidence` does not certify anyone, does not
widen permission, and does not substitute for a role certificate. A
model cannot supply runtime-identity fields: every identity value
folded in here is copied from caller-verified
`planning.qualification.AttemptProvenance` (which itself copies
`RuntimeContextProfile`), never from `PlannerResponse` identity claims.

## What is persisted, and what is not

Persisted (canonical JSON, content-addressed, internal, non-exportable):

- the canonical `runtime-config-spec-v1` document and its spec version
- the SHA-256 `runtime_config_fingerprint` of that spec
- model tag / digest / endpoint / runtime version
- compatibility-normalizer id/version (`None`/`None` = native-only)
- effective context tokens, temperature, output-token budget,
  tool-choice enforcement
- per-instance qualification class, instance outcome, attempt count,
  correction usage, and each attempt's bounded `AttemptProvenance`
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

The recorded fingerprint MUST be recomputable from the persisted spec
and MUST match the certificate's `runtime_config_fingerprint` before
issuance. A missing store, a spec/fingerprint mismatch, a wrong
`source_kind`, an exportable blob, a hash mismatch on re-read, or a
document that contains a forbidden raw-text key is a certification-
boundary refusal — no certificate is recorded.

No schema migration: `content_blobs` (schema v1) already stores the
bytes; `worker_role_certificates.evidence_ref` (schema v12) already
references them. Existing certificates are never rewritten.
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
    QualificationOutcome,
)
from code_slayer.store.content_store import BlobTooLargeError, ContentStore
from code_slayer.workers.security_baseline import (
    canonical_runtime_config_spec,
    fingerprint_runtime_config,
)

QUALIFICATION_EVIDENCE_SPEC_VERSION = "planner-qualification-evidence-v1"
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
    """Rebuild the canonical `runtime-config-spec-v1` document from one
    bounded attempt's caller-verified provenance. Never reads a model
    response. Raises `QualificationEvidenceError` if the attempt did not
    establish qualification-relevant inference configuration."""
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


def agreed_runtime_config_spec(
    results: tuple[QualificationAttemptResult, ...],
) -> dict:
    """The one canonical spec every attempt in every instance agrees on.
    Disagreement, or an attempt that cannot produce a spec, fails
    closed — never resolved by picking one."""
    specs: list[dict] = []
    for result in results:
        if not result.provenance:
            raise QualificationEvidenceError("runtime_config_spec_not_established")
        for attempt in result.provenance:
            specs.append(runtime_config_spec_from_provenance(attempt))
    if not specs:
        raise QualificationEvidenceError("runtime_config_spec_not_established")
    first = specs[0]
    if any(spec != first for spec in specs):
        raise QualificationEvidenceError("runtime_config_spec_mismatch")
    return first


def verify_runtime_config_fingerprint(spec: dict, expected_fingerprint: str) -> str:
    """Recompute the fingerprint from `spec` and require it to equal
    `expected_fingerprint`. Returns the recomputed digest. Mismatch or
    an unsupported spec fails closed."""
    if not isinstance(expected_fingerprint, str) or not expected_fingerprint:
        raise QualificationEvidenceError("runtime_config_fingerprint_mismatch")
    try:
        recomputed = fingerprint_runtime_config(spec)
    except (TypeError, ValueError) as exc:
        raise QualificationEvidenceError("runtime_config_fingerprint_mismatch") from exc
    if recomputed != expected_fingerprint:
        raise QualificationEvidenceError("runtime_config_fingerprint_mismatch")
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
    }


def build_planner_qualification_evidence_document(
    *,
    results: tuple[QualificationAttemptResult, ...],
    runtime_config_spec: dict,
    runtime_config_fingerprint: str,
    policy_version: str,
    classification: str,
) -> dict:
    """Pure. Builds the canonical document; does not write anything.
    Fingerprint mismatch fails closed before a caller can persist."""
    if not isinstance(results, tuple) or not results:
        raise QualificationEvidenceError("empty_qualification_evidence")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise QualificationEvidenceError("malformed_qualification_evidence")
    if not isinstance(classification, str) or not classification.strip():
        raise QualificationEvidenceError("malformed_qualification_evidence")
    verify_runtime_config_fingerprint(runtime_config_spec, runtime_config_fingerprint)
    agreed = agreed_runtime_config_spec(results)
    if agreed != runtime_config_spec:
        raise QualificationEvidenceError("runtime_config_spec_mismatch")
    verify_runtime_config_fingerprint(agreed, runtime_config_fingerprint)
    for result in results:
        for attempt in result.provenance:
            if attempt.runtime_config_fingerprint != runtime_config_fingerprint:
                raise QualificationEvidenceError("runtime_config_fingerprint_mismatch")

    total_attempts = sum(result.attempt_count for result in results)
    correction_used = any(
        result.outcome == QualificationOutcome.PASS_AFTER_FEEDBACK or result.attempt_count > 1
        for result in results
    )
    document = {
        "spec_version": QUALIFICATION_EVIDENCE_SPEC_VERSION,
        "role": "PLANNER",
        "policy_version": policy_version,
        "runtime_config_spec": runtime_config_spec,
        "runtime_config_fingerprint": runtime_config_fingerprint,
        "model_tag": runtime_config_spec["model_tag"],
        "model_digest": runtime_config_spec["model_digest"],
        "endpoint": runtime_config_spec["endpoint"],
        "runtime_version": runtime_config_spec["runtime_version"],
        "normalizer_id": runtime_config_spec["normalizer_id"],
        "normalizer_version": runtime_config_spec["normalizer_version"],
        "effective_context_tokens": runtime_config_spec["effective_context_tokens"],
        "temperature": runtime_config_spec["temperature"],
        "output_token_budget": runtime_config_spec["output_token_budget"],
        "tool_choice_enforcement": runtime_config_spec["tool_choice_enforcement"],
        "final_classification": classification,
        "instance_count": len(results),
        "attempt_count": total_attempts,
        "correction_used": correction_used,
        "instances": [_instance_to_dict(result) for result in results],
    }
    _forbid_raw_text_keys(document)
    return document


def _verify_document_bytes(
    data: bytes,
    *,
    expected_runtime_config_fingerprint: str,
) -> dict:
    if len(data) > MAX_QUALIFICATION_EVIDENCE_BYTES:
        raise QualificationEvidenceError("qualification_evidence_too_large")
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationEvidenceError("malformed_qualification_evidence") from exc
    if not isinstance(document, dict):
        raise QualificationEvidenceError("malformed_qualification_evidence")
    if document.get("spec_version") != QUALIFICATION_EVIDENCE_SPEC_VERSION:
        raise QualificationEvidenceError("unsupported_qualification_evidence_spec")
    _forbid_raw_text_keys(document)
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


def persist_planner_qualification_evidence(
    store: ContentStore,
    document: dict,
    *,
    expected_runtime_config_fingerprint: str,
) -> str:
    """Write the canonical document as an internal, non-exportable
    content-addressed blob and return its content hash. Re-reads and
    re-verifies the fingerprint from the persisted spec before
    returning. Never grants a certificate."""
    if not isinstance(store, ContentStore):
        raise QualificationEvidenceError("missing_durable_qualification_evidence")
    _forbid_raw_text_keys(document)
    payload = canonical_json(document).encode("utf-8")
    _verify_document_bytes(
        payload,
        expected_runtime_config_fingerprint=expected_runtime_config_fingerprint,
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
        expected_runtime_config_fingerprint=expected_runtime_config_fingerprint,
    )
    return blob.content_hash


def read_planner_qualification_evidence(
    conn: sqlite3.Connection,
    blobs_dir: Path | str,
    content_hash: str,
    *,
    expected_runtime_config_fingerprint: str | None = None,
) -> dict:
    """Reconstruct the canonical document from durable state. Fails
    closed on missing blob, wrong kind, exportable classification, hash
    mismatch, or (when supplied) fingerprint mismatch. Survives process
    restart because it only reads `content_blobs` plus the blob file."""
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
    document = json.loads(data.decode("utf-8"))
    if not isinstance(document, dict):
        raise QualificationEvidenceError("malformed_qualification_evidence")
    claimed = document.get("runtime_config_fingerprint")
    expected = expected_runtime_config_fingerprint or claimed
    if not isinstance(expected, str):
        raise QualificationEvidenceError("runtime_config_fingerprint_mismatch")
    return _verify_document_bytes(data, expected_runtime_config_fingerprint=expected)
