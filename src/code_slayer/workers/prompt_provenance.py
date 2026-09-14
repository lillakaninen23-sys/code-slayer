"""Durable provenance for Prompt Analyst / Question Gate decisions
(Phase 7.6 — `docs/CODE_SLAYER_VISION.md` §59 "Immutable task intent",
§60 "Audit / replay").

No parallel logging system, and no schema migration: `content_blobs` and
`audit_events` (schema v1) are already sufficient. The original prompt
and the full structured analysis are persisted exactly once each,
content-addressed, in the same `store.content_store.ContentStore` every
other piece of durable evidence in this codebase already lives in —
never dumped repeatedly into `audit_events` (`tools.models`: "File bytes
never form audit payloads" applies equally to a large prompt/analysis
document). `audit_events` carries only the small, structured facts a
later provenance query actually needs: content hashes, the gate's
decision, and the reasons/evidence references behind it — via the
existing `audit.writer.AuditWriter`, exactly the pattern `repo.
job_worktree`/`workers.execution` already established for their own new
event types.

This module performs no decision-making of its own — it durably records
a decision `workers.question_gate.QuestionGate` already made. It cannot
mutate files, execute tools, bypass `policy.engine.PolicyEngine`, grant
worker trust, change leases, or create checkpoints: it touches only
`ContentStore` and `AuditWriter`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from code_slayer.audit.canonical import canonical_json
from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import transaction
from code_slayer.workers.prompt_analysis import Ambiguity, PromptAnalysis
from code_slayer.workers.question_gate import QuestionGateResult

# The `ContentStore.put(source_kind=...)` classifications this module
# uses -- distinct from every other producer's own classification
# (`command_output`, `tool_read_output`, `rules_snapshot`, ...), so
# dedup never mislabels one kind of evidence as another.
PROMPT_EVIDENCE_KIND = "original_prompt"
ANALYSIS_EVIDENCE_KIND = "prompt_analysis"


@dataclass(frozen=True)
class PromptProvenance:
    """What was durably recorded for one Prompt Analyst / Question Gate
    decision — the smallest useful identity a later caller needs to
    retrieve the full prompt/analysis content or correlate it with the
    audit trail."""

    original_prompt_hash: str
    analysis_content_hash: str
    decision: str


def _serialize_analysis(analysis: PromptAnalysis) -> bytes:
    """A deterministic, canonical JSON document for the full analysis —
    everything durably retrievable by `analysis_content_hash`, never
    reconstructed from the audit trail alone."""
    def _ambiguity(a: Ambiguity) -> dict:
        return {
            "id": a.id, "question": a.question, "rationale": a.rationale,
            "risk_class": a.risk_class.value, "evidence_keys": list(a.evidence_keys),
            "resolved_by_prompt_substring": a.resolved_by_prompt_substring,
        }

    document = {
        "original_prompt_hash": analysis.original_prompt_hash,
        "goals": list(analysis.goals),
        "explicit_requirements": list(analysis.explicit_requirements),
        "constraints": list(analysis.constraints),
        "already_answered": list(analysis.already_answered),
        "ambiguities": [_ambiguity(a) for a in analysis.ambiguities],
        "risk_points": list(analysis.risk_points),
    }
    return canonical_json(document).encode("utf-8")


def record_prompt_analysis(
    conn: sqlite3.Connection, blobs_dir: Path | str, *,
    task_id: str | None, analysis: PromptAnalysis, gate_result: QuestionGateResult,
) -> PromptProvenance:
    """Durably persist the original prompt and the full structured
    analysis as content-addressed evidence, then append one
    `PROMPT_ANALYSIS_RECORDED` audit event referencing them, followed by
    one `QUESTION_GATE_DECISION` event recording `gate_result`.

    `task_id=None` is valid — a prompt may be analyzed before a task
    formally exists. A non-`None` `task_id` must already name a real row
    in `tasks` (the same foreign-key requirement every other audited
    event in this codebase already has); this function does not create
    one.

    Raises `TypeError` for a malformed `analysis`/`gate_result` — this
    function performs no decision-making of its own, only durable
    recording of a decision already made elsewhere.
    """
    if not isinstance(analysis, PromptAnalysis):
        raise TypeError("analysis must be a PromptAnalysis")
    if not isinstance(gate_result, QuestionGateResult):
        raise TypeError("gate_result must be a QuestionGateResult")

    store = ContentStore(conn, blobs_dir)
    with transaction(conn):
        prompt_blob = store.put(
            analysis.original_prompt.encode("utf-8"), media_type="text/plain",
            source_kind=PROMPT_EVIDENCE_KIND, exportable=False,
        )
        if prompt_blob.content_hash != analysis.original_prompt_hash:
            # Unreachable in practice -- ContentStore and
            # hash_original_prompt() both SHA-256 the exact same UTF-8
            # bytes -- but fail closed rather than trust a broken
            # invariant if that ever changes.
            raise RuntimeError(
                "original prompt blob hash disagrees with analysis.original_prompt_hash"
            )

        analysis_blob = store.put(
            _serialize_analysis(analysis), media_type="application/json",
            source_kind=ANALYSIS_EVIDENCE_KIND, exportable=False,
        )

        audit = AuditWriter(conn)
        common = {
            "original_prompt_hash": analysis.original_prompt_hash,
            "analysis_content_hash": analysis_blob.content_hash,
        }
        audit.append(
            task_id=task_id, event_type=EventType.PROMPT_ANALYSIS_RECORDED,
            actor_type="system", actor_id="prompt-analyst",
            payload={
                **common,
                "ambiguity_count": len(analysis.ambiguities),
                "goal_count": len(analysis.goals),
                "risk_point_count": len(analysis.risk_points),
            },
        )
        audit.append(
            task_id=task_id, event_type=EventType.QUESTION_GATE_DECISION,
            actor_type="system", actor_id="question-gate",
            payload={
                **common,
                "decision": gate_result.decision.value,
                "questions": list(gate_result.questions),
                "reasons": list(gate_result.reasons),
                "evidence_refs": list(gate_result.evidence_refs),
            },
        )

    return PromptProvenance(
        original_prompt_hash=analysis.original_prompt_hash,
        analysis_content_hash=analysis_blob.content_hash,
        decision=gate_result.decision.value,
    )
