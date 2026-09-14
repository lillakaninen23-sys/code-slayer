"""Question Gate: deterministic SUPPRESS/ASK decisions from actual
evidence only (Phase 7.6).

Covers: explicit-answer and authoritative-evidence suppression, ASK for
unresolved material/destructive/external-side-effect ambiguity, routine
ambiguity resolved by a conservative default rather than a question,
that model/analyst agreement is never itself evidence, that conflicting
analyst opinions never decide the gate, that only unresolved material
questions are ever returned, that the gate cannot touch trust/tool
authority, that a malformed analyst result fails closed, and that an
analysis produced for a different prompt is never evaluated as if it
applied to this one.
"""

from __future__ import annotations

from code_slayer.workers.fake_prompt_analyst import FakePromptAnalyst
from code_slayer.workers.prompt_analysis import (
    Ambiguity,
    AmbiguityRiskClass,
    EvidenceItem,
    EvidenceSource,
    PromptAnalysis,
    hash_original_prompt,
)
from code_slayer.workers.question_gate import GateDecision, QuestionGate, QuestionGateResult

PROMPT = "Add a script that deletes all files older than 30 days in /var/log."


def _gate():
    return QuestionGate()


def _analysis(prompt: str, ambiguities: tuple[Ambiguity, ...]) -> PromptAnalysis:
    return PromptAnalysis(original_prompt=prompt, ambiguities=ambiguities)


# --- 1/4. explicit user answer suppresses ------------------------------------

def test_explicit_user_answer_suppresses_duplicate_question():
    prompt = "Delete log files under /var/log older than 30 days, dry-run first."
    ambiguity = Ambiguity(
        id="dry-run",
        question="Should this run as a dry run first?",
        rationale="Deletion is irreversible; running for real without confirmation is risky.",
        risk_class=AmbiguityRiskClass.DESTRUCTIVE,
        resolved_by_prompt_substring="dry-run first",
    )
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), evidence={},
    )
    assert result.decision == GateDecision.SUPPRESS
    assert result.questions == ()
    assert any("dry-run" in ref for ref in result.evidence_refs)


def test_prompt_substring_claim_is_independently_reverified_not_trusted():
    """The analyst's claim that a substring occurs in the prompt is
    checked against the REAL prompt text -- a false claim must not
    suppress anything."""
    prompt = "Delete log files under /var/log older than 30 days."  # no "dry-run" mention
    ambiguity = Ambiguity(
        id="dry-run",
        question="Should this run as a dry run first?",
        rationale="Deletion is irreversible.",
        risk_class=AmbiguityRiskClass.DESTRUCTIVE,
        resolved_by_prompt_substring="dry-run first",  # false claim
    )
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), evidence={},
    )
    assert result.decision == GateDecision.ASK
    assert result.questions == ("Should this run as a dry run first?",)


# --- 5. authoritative repo/runtime evidence suppresses -----------------------

def test_authoritative_repository_evidence_suppresses_deterministic_question():
    prompt = "Add a script for the project's package manager."
    ambiguity = Ambiguity(
        id="package-manager",
        question="Which package manager should the script use?",
        rationale="The wrong package manager could install duplicate/conflicting deps.",
        risk_class=AmbiguityRiskClass.MATERIAL,
        evidence_keys=("repo:package_manager",),
    )
    evidence = {
        "repo:package_manager": EvidenceItem(
            source=EvidenceSource.REPOSITORY, value="npm", detail="package-lock.json present",
        ),
    }
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), evidence=evidence,
    )
    assert result.decision == GateDecision.SUPPRESS
    assert result.evidence_refs == ("repository:repo:package_manager",)


def test_authoritative_runtime_evidence_suppresses():
    prompt = "Open a PR against the current branch."
    ambiguity = Ambiguity(
        id="target-branch",
        question="Which branch is currently checked out?",
        rationale="Opening a PR against the wrong branch could target the wrong history.",
        risk_class=AmbiguityRiskClass.MATERIAL,
        evidence_keys=("runtime:current_branch",),
    )
    evidence = {
        "runtime:current_branch": EvidenceItem(
            source=EvidenceSource.RUNTIME, value="feature/health-check",
        ),
    }
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), evidence=evidence,
    )
    assert result.decision == GateDecision.SUPPRESS


# --- 6. missing authoritative evidence -> ASK for material ambiguity --------

def test_missing_authoritative_evidence_asks_for_material_ambiguity():
    prompt = "Add a script for the project's package manager."
    ambiguity = Ambiguity(
        id="package-manager",
        question="Which package manager should the script use?",
        rationale="The wrong package manager could install duplicate/conflicting deps.",
        risk_class=AmbiguityRiskClass.MATERIAL,
        evidence_keys=("repo:package_manager",),
    )
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), evidence={},
    )
    assert result.decision == GateDecision.ASK
    assert result.questions == ("Which package manager should the script use?",)


def test_analyst_thinking_answer_is_obvious_does_not_suppress():
    """No evidence_keys, no resolved_by_prompt_substring -- an analyst
    simply asserting confidence via rationale text changes nothing."""
    prompt = "Refactor the payment module."
    ambiguity = Ambiguity(
        id="obvious-guess",
        question="Should currency rounding use banker's rounding?",
        rationale="The analyst believes this is probably what's intended, though unstated.",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), evidence={},
    )
    assert result.decision == GateDecision.ASK


# --- 7/A. harmless deterministic choice does not unnecessarily ASK ---------

def test_routine_ambiguity_with_safe_default_does_not_ask():
    prompt = "Add a helper function to format dates."
    ambiguity = Ambiguity(
        id="date-format",
        question="Should dates be zero-padded?",
        rationale="Purely cosmetic; either choice is easily reversible.",
        risk_class=AmbiguityRiskClass.ROUTINE,
    )
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), evidence={},
    )
    assert result.decision == GateDecision.SUPPRESS
    assert result.questions == ()
    assert any("routine_default_applied" in r for r in result.reasons)


# --- 8/C. destructive ambiguity -> ASK --------------------------------------

def test_destructive_ambiguity_without_authorization_asks():
    prompt = "Clean up old files in /var/log."
    ambiguity = Ambiguity(
        id="delete-scope",
        question="Should this permanently delete files, or move them to a trash location first?",
        rationale="Permanent deletion is irreversible and could destroy needed logs.",
        risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), evidence={},
    )
    assert result.decision == GateDecision.ASK
    assert result.questions == (ambiguity.question,)


# --- 9. external-side-effect ambiguity -> ASK -------------------------------

def test_external_side_effect_ambiguity_without_authorization_asks():
    prompt = "Notify the team when the build finishes."
    ambiguity = Ambiguity(
        id="notify-channel",
        question="Should this send a real notification to an external service now?",
        rationale="Sending externally has consequences outside Code Slayer's own state.",
        risk_class=AmbiguityRiskClass.EXTERNAL_SIDE_EFFECT,
    )
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), evidence={},
    )
    assert result.decision == GateDecision.ASK


# --- D. explicitly answered in original prompt -> SUPPRESS ------------------

def test_explicitly_answered_destructive_ambiguity_suppresses():
    prompt = "Permanently delete files in /var/log older than 30 days; this is authorized."
    ambiguity = Ambiguity(
        id="delete-scope",
        question="Should this permanently delete files?",
        rationale="Permanent deletion is irreversible.",
        risk_class=AmbiguityRiskClass.DESTRUCTIVE,
        resolved_by_prompt_substring="Permanently delete files in /var/log older than 30 days",
    )
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), evidence={},
    )
    assert result.decision == GateDecision.SUPPRESS


# --- 10. LLM/model agreement alone is not evidence --------------------------

def test_two_analysts_agreeing_does_not_manufacture_evidence():
    """Analyst A and analyst B both claim the same evidence key resolves
    the ambiguity -- but neither the analyst's claim, nor two of them
    agreeing, ever puts anything into the real EvidenceContext. With no
    actual entry at that key, the gate must still ASK for both."""
    prompt = "Add a script for the project's package manager."
    ambiguity = Ambiguity(
        id="package-manager",
        question="Which package manager should the script use?",
        rationale="Wrong choice could install conflicting deps.",
        risk_class=AmbiguityRiskClass.MATERIAL,
        evidence_keys=("repo:package_manager",),  # both analysts propose the same key
    )
    fake_a = FakePromptAnalyst([_analysis(prompt, (ambiguity,))])
    fake_b = FakePromptAnalyst([_analysis(prompt, (ambiguity,))])
    analysis_a = fake_a.analyze(prompt, {})
    analysis_b = fake_b.analyze(prompt, {})

    real_evidence: dict = {}  # the actual, authoritative context: empty
    result_a = _gate().evaluate(original_prompt=prompt, analysis=analysis_a, evidence=real_evidence)
    result_b = _gate().evaluate(original_prompt=prompt, analysis=analysis_b, evidence=real_evidence)

    assert result_a.decision == GateDecision.ASK
    assert result_b.decision == GateDecision.ASK
    # Agreement between the two "analysts" produced nothing resolvable.
    assert result_a.evidence_refs == ()
    assert result_b.evidence_refs == ()


def test_agreement_still_irrelevant_once_real_evidence_exists():
    """The gate's decision tracks the REAL evidence context, not whether
    analysts agree: with a genuine repository fact present, both
    analysts' identically-labeled ambiguity now resolves -- proving it
    was the evidence, not the agreement, that mattered."""
    prompt = "Add a script for the project's package manager."
    ambiguity = Ambiguity(
        id="package-manager", question="Which package manager?",
        rationale="r", risk_class=AmbiguityRiskClass.MATERIAL,
        evidence_keys=("repo:package_manager",),
    )
    real_evidence = {
        "repo:package_manager": EvidenceItem(source=EvidenceSource.REPOSITORY, value="npm"),
    }
    result_a = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), evidence=real_evidence,
    )
    result_b = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), evidence=real_evidence,
    )
    assert result_a.decision == result_b.decision == GateDecision.SUPPRESS


# --- 11. conflicting analyst opinions do not decide the gate ----------------

def test_conflicting_analyst_risk_classifications_are_each_judged_on_their_own_evidence():
    """Two analysts disagree about the SAME nominal question's risk --
    one calls it routine, the other destructive. The gate has no
    mechanism to vote between them: each analysis is evaluated strictly
    on its own declared risk_class and its own actual evidence, and nei
    ther analysis's classification is influenced by, or overridden by,
    the other's disagreement."""
    prompt = "Clean up temporary files."
    routine_take = Ambiguity(
        id="cleanup-scope", question="Which temp directory?", rationale="r",
        risk_class=AmbiguityRiskClass.ROUTINE,
    )
    destructive_take = Ambiguity(
        id="cleanup-scope", question="Which temp directory?", rationale="r",
        risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    result_routine = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (routine_take,)), evidence={},
    )
    result_destructive = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (destructive_take,)), evidence={},
    )
    # Disagreement did not average out to some compromise -- each
    # analysis's own risk_class and evidence alone drove its own result.
    assert result_routine.decision == GateDecision.SUPPRESS
    assert result_destructive.decision == GateDecision.ASK


# --- 12. only unresolved material questions returned ------------------------

def test_only_unresolved_material_questions_are_returned():
    prompt = "Add logging; use the existing logger from utils/log.py; delete old logs."
    resolved = Ambiguity(
        id="logger", question="Which logger should be used?",
        rationale="r", risk_class=AmbiguityRiskClass.MATERIAL,
        resolved_by_prompt_substring="use the existing logger from utils/log.py",
    )
    routine = Ambiguity(
        id="log-level", question="What default log level?",
        rationale="r", risk_class=AmbiguityRiskClass.ROUTINE,
    )
    unresolved_destructive = Ambiguity(
        id="delete-old-logs", question="Should old logs be permanently deleted?",
        rationale="Irreversible.", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    result = _gate().evaluate(
        original_prompt=prompt,
        analysis=_analysis(prompt, (resolved, routine, unresolved_destructive)),
        evidence={},
    )
    assert result.decision == GateDecision.ASK
    assert result.questions == ("Should old logs be permanently deleted?",)


# --- 13. gate cannot grant trust/tool authority ------------------------------

def test_evaluate_signature_has_no_capability_bearing_parameters():
    import inspect

    params = set(inspect.signature(QuestionGate.evaluate).parameters) - {"self"}
    assert params == {"original_prompt", "analysis", "evidence"}


def test_evaluate_does_not_touch_any_database(db_conn):
    """`QuestionGate.evaluate()` takes no connection at all; calling it
    must leave every table in a freshly-migrated database completely
    untouched."""
    tables = ("tasks", "worker_leases", "worker_trust_events", "tool_operations", "checkpoints")
    before = {t: [dict(r) for r in db_conn.execute(f"SELECT * FROM {t}")] for t in tables}

    ambiguity = Ambiguity(
        id="x", question="q?", rationale="r", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)), evidence={},
    )

    after = {t: [dict(r) for r in db_conn.execute(f"SELECT * FROM {t}")] for t in tables}
    assert after == before


# --- 14. malformed analyst result fails closed ------------------------------

def test_none_analysis_fails_closed():
    result = _gate().evaluate(original_prompt=PROMPT, analysis=None, evidence={})
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("malformed_prompt_analysis",)


def test_plain_dict_analysis_fails_closed():
    fake_analysis = {"original_prompt": PROMPT, "ambiguities": ()}
    result = _gate().evaluate(original_prompt=PROMPT, analysis=fake_analysis, evidence={})
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("malformed_prompt_analysis",)


def test_fake_analyst_returning_malformed_result_is_caught_by_the_gate():
    analyst = FakePromptAnalyst([{"not": "a PromptAnalysis"}])
    produced = analyst.analyze(PROMPT, {})
    result = _gate().evaluate(original_prompt=PROMPT, analysis=produced, evidence={})
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("malformed_prompt_analysis",)


def test_ambiguities_not_a_tuple_fails_closed():
    analysis = _analysis(PROMPT, ())
    # Bypass the frozen dataclass to simulate a non-conformant analyst
    # implementation handing back something structurally wrong.
    object.__setattr__(analysis, "ambiguities", [1, 2, 3])
    result = _gate().evaluate(original_prompt=PROMPT, analysis=analysis, evidence={})
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("malformed_prompt_analysis",)


def test_non_mapping_evidence_fails_closed():
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, ()), evidence=["not", "a", "mapping"],
    )
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("malformed_evidence_context",)


def test_malformed_risk_class_fails_closed():
    analysis = _analysis(PROMPT, ())
    bad_ambiguity = Ambiguity(
        id="x", question="q?", rationale="r", risk_class=AmbiguityRiskClass.ROUTINE,
    )
    object.__setattr__(bad_ambiguity, "risk_class", "not_a_real_risk_class")
    object.__setattr__(analysis, "ambiguities", (bad_ambiguity,))
    result = _gate().evaluate(original_prompt=PROMPT, analysis=analysis, evidence={})
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("x:malformed_risk_class",)


# --- prompt-identity mismatch: analysis cannot replace the original prompt -

def test_analysis_for_a_different_prompt_is_never_evaluated_as_this_prompt():
    other_prompt = "Completely different task: rewrite the billing system."
    ambiguity = Ambiguity(
        id="x", question="q?", rationale="r", risk_class=AmbiguityRiskClass.ROUTINE,
    )
    mismatched_analysis = _analysis(other_prompt, (ambiguity,))
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=mismatched_analysis, evidence={},
    )
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("prompt_identity_mismatch",)


def test_hash_mismatch_is_detected_even_with_no_ambiguities():
    analysis = _analysis("prompt A", ())
    result = _gate().evaluate(original_prompt="prompt B", analysis=analysis, evidence={})
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("prompt_identity_mismatch",)
    assert analysis.original_prompt_hash == hash_original_prompt("prompt A")


# --- structural sanity -------------------------------------------------------

def test_suppress_result_has_no_questions():
    ambiguity = Ambiguity(
        id="x", question="q?", rationale="r", risk_class=AmbiguityRiskClass.ROUTINE,
    )
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)), evidence={},
    )
    assert isinstance(result, QuestionGateResult)
    assert result.decision == GateDecision.SUPPRESS
    assert result.questions == ()


def test_empty_ambiguities_suppress_trivially():
    result = _gate().evaluate(original_prompt=PROMPT, analysis=_analysis(PROMPT, ()), evidence={})
    assert result.decision == GateDecision.SUPPRESS
    assert result.questions == ()
    assert result.reasons == ()
