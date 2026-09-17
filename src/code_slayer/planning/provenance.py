"""Durable, content-addressed provenance for one planning attempt (Phase
8.2) — mirrors `workers.prompt_provenance`'s own pattern exactly, applied
to planning instead of prompt analysis.

No parallel logging system, no schema beyond `content_blobs`/
`audit_events` plus the small `engineering_plans` pointer row
(`store.migrations.0008_engineering_planning`). Every large document —
the original request, the bounded planner input, the planner's raw
structured output, the original textual transport of a `NORMALIZED`
turn (when one occurred), the evidence-validation result, the final
validated plan content — is persisted exactly once each, content-
addressed, in the same `store.content_store.ContentStore` every other
durable evidence record in this codebase already lives in.

This module performs no planning decisions of its own — only durable
recording of decisions `planning.service.EngineeringPlanningService`
already made. Training/evaluation provenance readiness (Phase 8.2's own
requirement) falls directly out of this: every one of the five
documents below is independently retrievable by content hash, so a
future export can reconstruct exactly what the user asked, what bounded
context the planner received, what it said, what evidence validation
did with that, and what final plan (if any) resulted — without ever
duplicating raw repository content into this module's own documents
(repository facts are cited by path/snapshot_id reference, never
re-embedded as bytes).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from code_slayer.planning.evidence import EvidenceValidationResult
from code_slayer.planning.models import (
    EngineeringPlanContent,
    plan_content_from_dict,
    plan_content_to_dict,
)
from code_slayer.planning.planner import (
    PlannerRequest,
    PlannerResponse,
    ToolCallTransport,
    render_bounded_context,
)
from code_slayer.store.content_store import ContentBlob, ContentStore
from code_slayer.workers.protocol import (
    WorkerSupplementalKind,
    WorkerSupplementalResolution,
    WorkerSupplementalSource,
)
from code_slayer.workers.protocol_normalization import MAX_NORMALIZER_INPUT_CHARS

REQUEST_EVIDENCE_KIND = "engineering_plan_request"
PLANNER_INPUT_EVIDENCE_KIND = "engineering_plan_planner_input"
PLANNER_OUTPUT_EVIDENCE_KIND = "engineering_plan_planner_output"
ORIGINAL_TRANSPORT_EVIDENCE_KIND = "engineering_plan_original_transport"
VALIDATION_EVIDENCE_KIND = "engineering_plan_validation"
PLAN_CONTENT_EVIDENCE_KIND = "engineering_plan_content"
PLAN_HUMAN_ANSWER_EVIDENCE_KIND = "engineering_plan_human_answer"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _read_verified(store: ContentStore, content_hash: str, expected_kind: str) -> bytes:
    meta = store.get_meta(content_hash)
    if meta is None or meta.source_kind != expected_kind:
        raise KeyError(f"no durable {expected_kind!r} evidence for {content_hash!r}")
    content = store.read(content_hash)
    if hashlib.sha256(content).hexdigest() != content_hash:
        raise RuntimeError(f"{expected_kind} blob content hash mismatch")
    return content


def store_request(store: ContentStore, original_request: str) -> ContentBlob:
    return store.put(
        original_request.encode("utf-8"),
        media_type="text/plain",
        source_kind=REQUEST_EVIDENCE_KIND,
        exportable=False,
    )


def read_request(conn: sqlite3.Connection, blobs_dir: Path | str, content_hash: str) -> str:
    store = ContentStore(conn, blobs_dir)
    return _read_verified(store, content_hash, REQUEST_EVIDENCE_KIND).decode("utf-8")


def _resolution_to_dict(resolution: WorkerSupplementalResolution) -> dict:
    return {
        "ambiguity_id": resolution.ambiguity_id,
        "kind": resolution.kind.value,
        "source": resolution.source.value,
        "content_hash": resolution.content_hash,
        # The verified answer text itself is never duplicated here -- a
        # reader already has `content_hash` to re-verify/re-read it from
        # ContentStore directly, exactly like every other reference in
        # this module.
    }


def store_planner_input(store: ContentStore, request: PlannerRequest) -> ContentBlob:
    """The bounded `PlannerRequest` actually sent, minus the (already
    separately hashed) original request text itself — this document
    exists so a future training/evaluation export can see exactly what
    repository evidence the planner was given, without re-embedding the
    user's own request a second time. Uses `planning.planner.
    render_bounded_context()` — the exact same view actually rendered
    into the model's prompt (Phase 8.2b) — so this durable record can
    never silently diverge from what the planner really received, and
    never duplicates `context_pack.projects`/`.commands` a second time
    alongside `repo_context`/`discovered_commands` (see that function's
    own docstring)."""
    document = {
        "original_request_length": len(request.original_request),
        **render_bounded_context(request),
        "supplemental_resolutions": [
            _resolution_to_dict(r) for r in request.supplemental_resolutions
        ],
    }
    return store.put(
        _canonical(document),
        media_type="application/json",
        source_kind=PLANNER_INPUT_EVIDENCE_KIND,
        exportable=False,
    )


def store_planner_output(store: ContentStore, response: PlannerResponse) -> ContentBlob:
    """The planner's own raw structured response, exactly as received —
    kept distinct from the evidence-validated `EngineeringPlanContent`
    so a later reader can always see what the model actually said versus
    what of that survived validation. Internal-only: `raw`/`error` are
    never surfaced through `planning.service.PlanRecord`/the HTTP API
    (Phase 8.2b) — only the coarse `failure_category` is (via
    `planning.service`'s durable `reason` field).

    For a `NORMALIZED` turn, the original provider/model textual
    payload is persisted as a *separate* internal-only blob
    (`ORIGINAL_TRANSPORT_EVIDENCE_KIND`) and referenced here by hash.
    `document["raw"]` remains the canonical structured params (or the
    coarse failure payload) — never replaced by the original text.
    Native turns do not grow this document and do not create a
    transport blob. This evidence never influences permissions, trust,
    certification, parser acceptance, or execution authority."""
    document = {
        "outcome": response.outcome.value,
        "raw": response.raw,
        "error": response.error,
        "failure_category": (
            response.failure_category.value if response.failure_category else None
        ),
        "tool_call_transport": (
            response.tool_call_transport.value if response.tool_call_transport else None
        ),
        "normalizer_id": response.normalizer_id,
        "normalizer_version": response.normalizer_version,
        "normalization_reason": response.normalization_reason,
    }
    if response.tool_call_transport == ToolCallTransport.NORMALIZED:
        if not isinstance(response.original_transport_text, str):
            raise ValueError("NORMALIZED planner output requires original_transport_text")
        transport_blob = store_original_transport(store, response.original_transport_text)
        document["original_transport_content_hash"] = transport_blob.content_hash
    elif response.original_transport_text is not None:
        raise ValueError("original_transport_text is only valid for NORMALIZED transport")
    return store.put(
        _canonical(document),
        media_type="application/json",
        source_kind=PLANNER_OUTPUT_EVIDENCE_KIND,
        exportable=False,
    )


def store_original_transport(store: ContentStore, text: str) -> ContentBlob:
    """Exact original provider/model textual payload for one NORMALIZED
    turn — internal-only, non-exportable, bounded to the compatibility
    layer's own maximum input size. Truncation is recorded on the blob
    (`truncated=True`) rather than silently dropped; a successful
    decoder path never produces a payload above this bound."""
    if not isinstance(text, str):
        raise TypeError("original transport text must be a str")
    truncated = len(text) > MAX_NORMALIZER_INPUT_CHARS
    bounded = text[:MAX_NORMALIZER_INPUT_CHARS]
    return store.put(
        bounded.encode("utf-8"),
        media_type="text/plain",
        source_kind=ORIGINAL_TRANSPORT_EVIDENCE_KIND,
        exportable=False,
        truncated=truncated,
    )


def read_original_transport(
    conn: sqlite3.Connection,
    blobs_dir: Path | str,
    content_hash: str,
) -> str:
    store = ContentStore(conn, blobs_dir)
    return _read_verified(store, content_hash, ORIGINAL_TRANSPORT_EVIDENCE_KIND).decode("utf-8")


def store_validation_result(store: ContentStore, result: EvidenceValidationResult) -> ContentBlob:
    document = {"blocking": result.blocking, "issues": list(result.issues)}
    return store.put(
        _canonical(document),
        media_type="application/json",
        source_kind=VALIDATION_EVIDENCE_KIND,
        exportable=False,
    )


def store_plan_content(store: ContentStore, content: EngineeringPlanContent) -> ContentBlob:
    return store.put(
        _canonical(plan_content_to_dict(content)),
        media_type="application/json",
        source_kind=PLAN_CONTENT_EVIDENCE_KIND,
        exportable=False,
    )


def read_plan_content(
    conn: sqlite3.Connection,
    blobs_dir: Path | str,
    content_hash: str,
) -> EngineeringPlanContent:
    store = ContentStore(conn, blobs_dir)
    document = json.loads(_read_verified(store, content_hash, PLAN_CONTENT_EVIDENCE_KIND))
    return plan_content_from_dict(document)


def read_supplemental_resolution(
    conn: sqlite3.Connection,
    blobs_dir: Path | str,
    *,
    ambiguity_id: str,
    source: str,
    resolution_kind: str,
    answer_content_hash: str,
) -> WorkerSupplementalResolution:
    """Read back one durable human-resolution answer and verify its
    content identity — mirrors `runner.local_worker_runner.
    LocalWorkerRunner._read_verified_human_answer()`, applied to plans."""
    store = ContentStore(conn, blobs_dir)
    content = _read_verified(store, answer_content_hash, PLAN_HUMAN_ANSWER_EVIDENCE_KIND)
    return WorkerSupplementalResolution(
        ambiguity_id=ambiguity_id,
        kind=WorkerSupplementalKind(resolution_kind),
        source=WorkerSupplementalSource(source),
        content=content.decode("utf-8"),
        content_hash=answer_content_hash,
    )
