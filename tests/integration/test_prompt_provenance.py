"""Durable provenance for Prompt Analyst / Question Gate decisions
(Phase 7.6): content-addressed evidence, the audit trail, and that
neither recording nor evaluating a decision grants any trust/tool
authority or disturbs task/state-machine semantics.
"""

from __future__ import annotations

import json

import pytest

from code_slayer.audit.verify import verify_chain
from code_slayer.store.content_store import ContentStore
from code_slayer.store.task_repo import TaskRepo
from code_slayer.workers.prompt_analysis import (
    Ambiguity,
    AmbiguityRiskClass,
    EvidenceSource,
    PromptAnalysis,
)
from code_slayer.workers.prompt_provenance import (
    ANALYSIS_EVIDENCE_KIND,
    PROMPT_EVIDENCE_KIND,
    PromptProvenance,
    record_prompt_analysis,
)
from code_slayer.workers.question_gate import (
    GateDecision,
    QuestionGate,
    QuestionGateResult,
    ResolutionEvidence,
    ResolutionKind,
)

PROMPT = "Add a health check endpoint to the API."


def _ask_analysis() -> PromptAnalysis:
    return PromptAnalysis(
        original_prompt=PROMPT,
        goals=("expose a health check route",),
        ambiguities=(
            Ambiguity(
                id="delete-old-route",
                question="Should the old /status route be removed?",
                rationale="Removing an existing route could break other callers.",
                risk_class=AmbiguityRiskClass.DESTRUCTIVE,
            ),
        ),
    )


def _suppress_analysis() -> PromptAnalysis:
    return PromptAnalysis(
        original_prompt=PROMPT,
        ambiguities=(
            Ambiguity(
                id="response-format",
                question="What format should the response body use?",
                rationale="Cosmetic; either choice is reversible.",
                risk_class=AmbiguityRiskClass.ROUTINE,
            ),
        ),
    )


@pytest.fixture
def blobs_dir(tmp_path):
    return tmp_path / "evidence"


def test_original_prompt_and_analysis_persisted_as_content_addressed_evidence(
    db_conn, blobs_dir,
):
    analysis = _suppress_analysis()
    gate_result = QuestionGate().evaluate(
        original_prompt=PROMPT, analysis=analysis, resolutions=(),
    )
    provenance = record_prompt_analysis(
        db_conn, blobs_dir, task_id=None, analysis=analysis, gate_result=gate_result,
    )

    store = ContentStore(db_conn, blobs_dir)
    prompt_meta = store.get_meta(provenance.original_prompt_hash)
    assert prompt_meta is not None
    assert prompt_meta.source_kind == PROMPT_EVIDENCE_KIND
    assert prompt_meta.exportable is False
    assert store.read(provenance.original_prompt_hash) == PROMPT.encode("utf-8")

    analysis_meta = store.get_meta(provenance.analysis_content_hash)
    assert analysis_meta is not None
    assert analysis_meta.source_kind == ANALYSIS_EVIDENCE_KIND
    assert analysis_meta.exportable is False
    document = json.loads(store.read(provenance.analysis_content_hash))
    assert document["original_prompt_hash"] == provenance.original_prompt_hash
    assert document["ambiguities"][0]["id"] == "response-format"

    # Two distinct blobs -- the prompt and the analysis are never
    # conflated into a single piece of evidence.
    assert provenance.original_prompt_hash != provenance.analysis_content_hash


def test_original_prompt_hash_matches_analysis_hash(db_conn, blobs_dir):
    analysis = _suppress_analysis()
    gate_result = QuestionGate().evaluate(original_prompt=PROMPT, analysis=analysis, resolutions=())
    provenance = record_prompt_analysis(
        db_conn, blobs_dir, task_id=None, analysis=analysis, gate_result=gate_result,
    )
    assert provenance.original_prompt_hash == analysis.original_prompt_hash


def test_audit_events_recorded_with_small_structured_payload_not_raw_prompt(db_conn, blobs_dir):
    analysis = _ask_analysis()
    gate_result = QuestionGate().evaluate(original_prompt=PROMPT, analysis=analysis, resolutions=())
    assert gate_result.decision == GateDecision.ASK
    provenance = record_prompt_analysis(
        db_conn, blobs_dir, task_id=None, analysis=analysis, gate_result=gate_result,
    )

    rows = [dict(r) for r in db_conn.execute(
        "SELECT * FROM audit_events WHERE event_type IN "
        "('PROMPT_ANALYSIS_RECORDED', 'QUESTION_GATE_DECISION') ORDER BY seq",
    )]
    assert [r["event_type"] for r in rows] == [
        "PROMPT_ANALYSIS_RECORDED", "QUESTION_GATE_DECISION",
    ]
    for row in rows:
        assert row["task_id"] is None
        payload = json.loads(row["payload_json"])
        assert payload["original_prompt_hash"] == provenance.original_prompt_hash
        # The raw prompt text itself never appears in the audit payload.
        assert PROMPT not in row["payload_json"]

    decision_payload = json.loads(rows[1]["payload_json"])
    assert decision_payload["decision"] == "ASK"
    assert decision_payload["questions"] == list(gate_result.questions)
    assert decision_payload["analysis_content_hash"] == provenance.analysis_content_hash

    assert verify_chain(db_conn, task_id=None).ok


def test_works_with_task_id_none(db_conn, blobs_dir):
    analysis = _suppress_analysis()
    gate_result = QuestionGate().evaluate(original_prompt=PROMPT, analysis=analysis, resolutions=())
    provenance = record_prompt_analysis(
        db_conn, blobs_dir, task_id=None, analysis=analysis, gate_result=gate_result,
    )
    assert isinstance(provenance, PromptProvenance)


def test_works_with_a_real_task_id(db_conn, blobs_dir):
    task = TaskRepo(db_conn).create(
        description="add health check", repo_root="/repo", repo_id="repo-1",
        worktree_id="wt-1",
    )
    analysis = _suppress_analysis()
    gate_result = QuestionGate().evaluate(original_prompt=PROMPT, analysis=analysis, resolutions=())
    record_prompt_analysis(
        db_conn, blobs_dir, task_id=task.task_id, analysis=analysis, gate_result=gate_result,
    )
    rows = [dict(r) for r in db_conn.execute(
        "SELECT * FROM audit_events WHERE task_id = ? ORDER BY seq", (task.task_id,),
    )]
    event_types = [r["event_type"] for r in rows]
    assert "PROMPT_ANALYSIS_RECORDED" in event_types
    assert "QUESTION_GATE_DECISION" in event_types
    assert verify_chain(db_conn, task_id=task.task_id).ok


def test_recording_does_not_change_task_state(db_conn, blobs_dir):
    """No state-machine redesign, no task-lifecycle side effect: recording
    a decision must never itself move a task's state or phase."""
    task = TaskRepo(db_conn).create(
        description="add health check", repo_root="/repo", repo_id="repo-1",
        worktree_id="wt-1",
    )
    before = TaskRepo(db_conn).get(task.task_id)
    analysis = _ask_analysis()
    gate_result = QuestionGate().evaluate(original_prompt=PROMPT, analysis=analysis, resolutions=())
    record_prompt_analysis(
        db_conn, blobs_dir, task_id=task.task_id, analysis=analysis, gate_result=gate_result,
    )
    after = TaskRepo(db_conn).get(task.task_id)
    assert after.state == before.state
    assert after.current_phase == before.current_phase
    assert after.config_json == before.config_json


def test_normal_state_transitions_still_work_after_recording(db_conn, blobs_dir):
    """No state-machine redesign: a task can still transition through its
    ordinary lifecycle normally after a prompt-analysis decision was
    recorded against it -- this module adds no new state and interferes
    with none of the existing transition graph."""
    from code_slayer.core import TaskState, TaskStateMachine

    task = TaskRepo(db_conn).create(
        description="add health check", repo_root="/repo", repo_id="repo-1",
        worktree_id="wt-1",
    )
    analysis = _suppress_analysis()
    gate_result = QuestionGate().evaluate(original_prompt=PROMPT, analysis=analysis, resolutions=())
    record_prompt_analysis(
        db_conn, blobs_dir, task_id=task.task_id, analysis=analysis, gate_result=gate_result,
    )

    updated = TaskStateMachine(db_conn).transition(
        task.task_id, expected_state=TaskState.CREATED, to_state=TaskState.INSPECTING,
        reason="inspection started",
    )
    assert updated.state == TaskState.INSPECTING.value


def test_recording_grants_no_trust_lease_or_checkpoint_authority(db_conn, blobs_dir):
    tables = ("worker_leases", "worker_trust_events", "tool_operations", "checkpoints")
    before = {t: [dict(r) for r in db_conn.execute(f"SELECT * FROM {t}")] for t in tables}

    analysis = _ask_analysis()
    gate_result = QuestionGate().evaluate(original_prompt=PROMPT, analysis=analysis, resolutions=())
    record_prompt_analysis(
        db_conn, blobs_dir, task_id=None, analysis=analysis, gate_result=gate_result,
    )

    after = {t: [dict(r) for r in db_conn.execute(f"SELECT * FROM {t}")] for t in tables}
    assert after == before


def test_record_prompt_analysis_rejects_malformed_analysis(db_conn, blobs_dir):
    gate_result = QuestionGateResult(GateDecision.SUPPRESS)
    with pytest.raises(TypeError):
        record_prompt_analysis(
            db_conn, blobs_dir, task_id=None,
            analysis={"not": "a PromptAnalysis"}, gate_result=gate_result,
        )


def test_record_prompt_analysis_rejects_malformed_gate_result(db_conn, blobs_dir):
    analysis = _suppress_analysis()
    with pytest.raises(TypeError):
        record_prompt_analysis(
            db_conn, blobs_dir, task_id=None,
            analysis=analysis, gate_result={"decision": "SUPPRESS"},
        )


def test_repeated_recording_of_identical_prompt_dedups_the_prompt_blob(db_conn, blobs_dir):
    """Two different analyses of the exact same original prompt text
    must not duplicate the prompt's own evidence blob -- content
    addressing already guarantees this, verified here end to end."""
    analysis_1 = _suppress_analysis()
    analysis_2 = _ask_analysis()
    assert analysis_1.original_prompt_hash == analysis_2.original_prompt_hash

    gate_1 = QuestionGate().evaluate(original_prompt=PROMPT, analysis=analysis_1, resolutions=())
    gate_2 = QuestionGate().evaluate(original_prompt=PROMPT, analysis=analysis_2, resolutions=())
    provenance_1 = record_prompt_analysis(
        db_conn, blobs_dir, task_id=None, analysis=analysis_1, gate_result=gate_1,
    )
    provenance_2 = record_prompt_analysis(
        db_conn, blobs_dir, task_id=None, analysis=analysis_2, gate_result=gate_2,
    )
    assert provenance_1.original_prompt_hash == provenance_2.original_prompt_hash
    # The two analyses genuinely differ, so their own blobs differ.
    assert provenance_1.analysis_content_hash != provenance_2.analysis_content_hash

    blob_count = db_conn.execute(
        "SELECT count(*) FROM content_blobs WHERE content_hash = ?",
        (provenance_1.original_prompt_hash,),
    ).fetchone()[0]
    assert blob_count == 1


def test_analyst_self_resolution_attempt_is_durably_recorded_as_ask(db_conn, blobs_dir):
    """An analyst that proposes an ambiguity AND proposes the very
    substring/evidence key that would resolve it, with no independently
    supplied `ResolutionEvidence`, must be durably recorded as ASK --
    never silently accepted as SUPPRESS."""
    prompt = "Delete the old database once the migration finishes."
    analysis = PromptAnalysis(
        original_prompt=prompt,
        ambiguities=(
            Ambiguity(
                id="which-database", question="Which database should be destroyed?",
                rationale="Destroying the wrong database is catastrophic.",
                risk_class=AmbiguityRiskClass.DESTRUCTIVE,
                resolved_by_prompt_substring="the",  # trivially present, proves nothing
                evidence_keys=("self-proposed-key",),
            ),
        ),
    )
    gate_result = QuestionGate().evaluate(
        original_prompt=prompt, analysis=analysis, resolutions=(),
    )
    assert gate_result.decision == GateDecision.ASK

    record_prompt_analysis(
        db_conn, blobs_dir, task_id=None, analysis=analysis, gate_result=gate_result,
    )
    decision_row = db_conn.execute(
        "SELECT payload_json FROM audit_events WHERE event_type = 'QUESTION_GATE_DECISION' "
        "ORDER BY seq DESC LIMIT 1",
    ).fetchone()
    payload = json.loads(decision_row["payload_json"])
    assert payload["decision"] == "ASK"
    assert payload["questions"] == ["Which database should be destroyed?"]
    assert payload["evidence_refs"] == []


def test_full_flow_with_authoritative_evidence_end_to_end(db_conn, blobs_dir):
    """analyst -> gate -> provenance, using real, independently supplied
    trusted resolution evidence -- never anything the analyst itself
    asserted (Phase 7.6 question-gate hardening)."""
    prompt = "Add a script for the project's package manager."
    analysis = PromptAnalysis(
        original_prompt=prompt,
        ambiguities=(
            Ambiguity(
                id="package-manager", question="Which package manager?",
                rationale="Wrong choice could conflict with the existing lockfile.",
                risk_class=AmbiguityRiskClass.MATERIAL,
                evidence_keys=("repo:package_manager",),  # analyst's own non-binding hint
            ),
        ),
    )
    # Independently supplied by the caller (Code Slayer's own repository
    # inspection) -- never derived from the analysis above, and
    # explicitly bound to the exact ambiguity id it resolves.
    resolutions = (
        ResolutionEvidence(
            key="repo.package_manager", source=EvidenceSource.REPOSITORY,
            resolution_kind=ResolutionKind.FACT, resolves_ambiguity_ids=("package-manager",),
            detail="package-lock.json present",
        ),
    )
    gate_result = QuestionGate().evaluate(
        original_prompt=prompt, analysis=analysis, resolutions=resolutions,
    )
    assert gate_result.decision == GateDecision.SUPPRESS

    provenance = record_prompt_analysis(
        db_conn, blobs_dir, task_id=None, analysis=analysis, gate_result=gate_result,
    )
    decision_row = db_conn.execute(
        "SELECT payload_json FROM audit_events WHERE event_type = 'QUESTION_GATE_DECISION' "
        "ORDER BY seq DESC LIMIT 1",
    ).fetchone()
    payload = json.loads(decision_row["payload_json"])
    assert payload["decision"] == "SUPPRESS"
    assert payload["evidence_refs"] == ["repository:repo.package_manager:fact"]
    assert payload["analysis_content_hash"] == provenance.analysis_content_hash
