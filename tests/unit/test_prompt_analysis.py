"""Prompt Analyst types: original-prompt preservation, deterministic
identity, and structural immutability (Phase 7.6)."""

from __future__ import annotations

import dataclasses
import hashlib

import pytest

from code_slayer.workers.prompt_analysis import (
    Ambiguity,
    AmbiguityRiskClass,
    EvidenceItem,
    EvidenceSource,
    PromptAnalysis,
    hash_original_prompt,
)

PROMPT = "Add a health check endpoint to the API."


def test_exact_original_prompt_preserved():
    analysis = PromptAnalysis(original_prompt=PROMPT)
    assert analysis.original_prompt == PROMPT
    assert analysis.original_prompt is PROMPT  # not copied/rewritten into a new string object


def test_original_prompt_preserved_verbatim_including_whitespace_and_case():
    """No normalization of any kind -- exact bytes in, exact bytes out."""
    messy = "  Please   Fix the   Bug\t\nin main.py.  \n\n"
    analysis = PromptAnalysis(original_prompt=messy)
    assert analysis.original_prompt == messy


def test_original_prompt_hash_deterministic():
    first = hash_original_prompt(PROMPT)
    second = hash_original_prompt(PROMPT)
    assert first == second
    assert first == hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()


def test_original_prompt_hash_differs_for_different_prompts():
    assert hash_original_prompt(PROMPT) != hash_original_prompt(PROMPT + " ")


def test_original_prompt_hash_not_normalized():
    """The project has no existing canonical prompt format, so none is
    invented here -- whitespace differences must change the hash."""
    assert hash_original_prompt("fix bug") != hash_original_prompt("fix  bug")
    assert hash_original_prompt("Fix Bug") != hash_original_prompt("fix bug")


def test_analysis_original_prompt_hash_matches_exact_bytes():
    analysis = PromptAnalysis(original_prompt=PROMPT)
    assert analysis.original_prompt_hash == hash_original_prompt(PROMPT)
    assert analysis.original_prompt_hash == hashlib.sha256(PROMPT.encode("utf-8")).hexdigest()


def test_hash_original_prompt_rejects_non_string():
    with pytest.raises(TypeError):
        hash_original_prompt(None)  # type: ignore[arg-type]


def test_analysis_is_frozen_and_cannot_replace_original_prompt():
    analysis = PromptAnalysis(original_prompt=PROMPT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        analysis.original_prompt = "a completely different, analyst-authored prompt"  # type: ignore[misc]


def test_analysis_hash_field_is_not_a_constructor_parameter():
    """The Prompt Analyst cannot alter the hash: it is not even settable
    at construction time, only ever derived from `original_prompt`."""
    with pytest.raises(TypeError):
        PromptAnalysis(  # type: ignore[call-arg]
            original_prompt=PROMPT, original_prompt_hash="0" * 64,
        )


def test_forged_hash_field_cannot_be_assigned_after_construction():
    analysis = PromptAnalysis(original_prompt=PROMPT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        analysis.original_prompt_hash = "0" * 64  # type: ignore[misc]


def test_supplemental_fields_never_read_as_the_original_prompt():
    """Goals/requirements/constraints/etc. are supplemental analysis --
    nothing about this object conflates them with the authoritative
    original prompt itself."""
    analysis = PromptAnalysis(
        original_prompt=PROMPT,
        goals=("expose a healthcheck route",),
        explicit_requirements=("must return 200 when healthy",),
        constraints=("no new dependencies",),
        already_answered=("framework: existing Flask app",),
        risk_points=("could conflict with an existing /health route",),
    )
    assert analysis.original_prompt == PROMPT
    assert PROMPT not in analysis.goals
    assert PROMPT not in analysis.explicit_requirements


def test_ambiguity_structure_carries_deterministic_gating_fields():
    ambiguity = Ambiguity(
        id="target-branch",
        question="Which branch should this change target?",
        rationale="No branch was named and the wrong one could be destructive to merge into.",
        risk_class=AmbiguityRiskClass.DESTRUCTIVE,
        evidence_keys=("runtime:current_branch",),
        resolved_by_prompt_substring=None,
    )
    assert ambiguity.id == "target-branch"
    assert ambiguity.risk_class == AmbiguityRiskClass.DESTRUCTIVE
    assert "runtime:current_branch" in ambiguity.evidence_keys


def test_ambiguity_is_frozen():
    ambiguity = Ambiguity(
        id="x", question="q?", rationale="r", risk_class=AmbiguityRiskClass.ROUTINE,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        ambiguity.question = "different question"  # type: ignore[misc]


def test_evidence_item_never_carries_a_model_opinion_source():
    """`EvidenceSource` has no member meaning "a model/analyst said so" --
    the type system itself makes model consensus impossible to represent
    as evidence."""
    sources = {member.value for member in EvidenceSource}
    assert sources == {"ORIGINAL_PROMPT", "REPOSITORY", "RUNTIME", "DURABLE_TASK_EVIDENCE"}
    assert "ANALYST_CLAIM" not in sources
    assert "MODEL_CONSENSUS" not in sources


def test_evidence_item_is_frozen():
    item = EvidenceItem(source=EvidenceSource.REPOSITORY, value="npm", detail="package.json")
    with pytest.raises(dataclasses.FrozenInstanceError):
        item.value = "yarn"  # type: ignore[misc]
