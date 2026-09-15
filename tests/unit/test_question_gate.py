"""Question Gate: deterministic SUPPRESS/ASK decisions from trusted,
independently supplied resolution evidence only (Phase 7.6, hardened).

Covers: that an analyst can never resolve its own proposed ambiguity via
`risk_class`, `resolved_by_prompt_substring`, or `evidence_keys` alone;
that only an independently supplied `ResolutionEvidence` explicitly
bound to an ambiguity's exact id can suppress it; risk-specific
authority (a fact is never itself an authorization for destructive/
external-side-effect ambiguity); that model/analyst agreement never
manufactures evidence; that resolution never leaks across ambiguity
ids; malformed-input fail-closed behavior; and that the gate cannot
touch trust/tool/database state.
"""

from __future__ import annotations

import inspect

from code_slayer.workers.fake_prompt_analyst import FakePromptAnalyst
from code_slayer.workers.prompt_analysis import (
    Ambiguity,
    AmbiguityRiskClass,
    PromptAnalysis,
    hash_original_prompt,
)
from code_slayer.workers.prompt_analysis import EvidenceSource as Source
from code_slayer.workers.question_gate import (
    GateDecision,
    QuestionGate,
    QuestionGateResult,
    ResolutionEvidence,
    ResolutionKind,
)

PROMPT = "Add a script that deletes all files older than 30 days in /var/log."


def _gate():
    return QuestionGate()


def _analysis(prompt: str, ambiguities: tuple[Ambiguity, ...]) -> PromptAnalysis:
    return PromptAnalysis(original_prompt=prompt, ambiguities=ambiguities)


def _resolution(
    key: str, source: Source, kind: ResolutionKind, *ids: str,
) -> ResolutionEvidence:
    return ResolutionEvidence(
        key=key, source=source, resolution_kind=kind, resolves_ambiguity_ids=ids,
    )


# =========================================================================
# ADVERSARIAL: an analyst cannot resolve its own proposed ambiguity
# =========================================================================

# --- 1. risk_class == ROUTINE does not suppress by itself -------------------

def test_analyst_marking_unresolved_ambiguity_routine_does_not_suppress():
    ambiguity = Ambiguity(
        id="cleanup-scope",
        question="Which directory should be cleaned up?",
        rationale="Analyst claims this is routine.",
        risk_class=AmbiguityRiskClass.ROUTINE,
    )
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)), resolutions=(),
    )
    assert result.decision == GateDecision.ASK
    assert result.questions == (ambiguity.question,)


# --- 2. a common prompt substring does not suppress --------------------------

def test_common_prompt_substring_claim_does_not_suppress():
    """The analyst picks "the" -- trivially present in almost any prompt
    -- and claims it answers a completely unrelated destructive
    question. `resolved_by_prompt_substring` is never itself checked by
    the gate at all now; only a real `ResolutionEvidence` can resolve
    anything."""
    prompt = "Delete the old database once the migration finishes."
    ambiguity = Ambiguity(
        id="which-database",
        question="Which database should be destroyed?",
        rationale="Destroying the wrong database is catastrophic.",
        risk_class=AmbiguityRiskClass.DESTRUCTIVE,
        resolved_by_prompt_substring="the",
    )
    assert "the" in prompt  # the claimed substring is trivially present
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), resolutions=(),
    )
    assert result.decision == GateDecision.ASK
    assert result.questions == (ambiguity.question,)


def test_genuinely_matching_but_irrelevant_substring_still_does_not_suppress_material():
    """Even a substring that genuinely, verbatim, occurs in the prompt
    and superficially "relates" must never suppress on its own -- only
    trusted ResolutionEvidence can."""
    prompt = "Clean up old files in /var/log using the standard retention policy."
    ambiguity = Ambiguity(
        id="retention-days",
        question="How many days should the retention policy keep files for?",
        rationale="Wrong retention could delete needed logs or keep useless ones.",
        risk_class=AmbiguityRiskClass.MATERIAL,
        resolved_by_prompt_substring="the standard retention policy",
    )
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)), resolutions=(),
    )
    assert result.decision == GateDecision.ASK


# --- 3. evidence_keys pointing at an unrelated authoritative fact -----------

def test_evidence_keys_hint_at_unrelated_authoritative_fact_does_not_suppress():
    """The analyst names `evidence_keys=("repo.head",)` -- a real,
    authoritative fact exists (bound to a DIFFERENT ambiguity) -- but
    since no `ResolutionEvidence` explicitly names *this* ambiguity's id,
    it must not be suppressed."""
    ambiguity = Ambiguity(
        id="package-manager",
        question="Which package manager should the script use?",
        rationale="Wrong choice could install conflicting deps.",
        risk_class=AmbiguityRiskClass.MATERIAL,
        evidence_keys=("repo.head",),  # analyst's own hint, unrelated to the real fact below
    )
    # A genuinely authoritative fact exists, but it resolves a DIFFERENT
    # ambiguity id ("current-branch"), never "package-manager".
    unrelated = _resolution("repo.head", Source.REPOSITORY, ResolutionKind.FACT, "current-branch")
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)), resolutions=(unrelated,),
    )
    assert result.decision == GateDecision.ASK
    assert result.evidence_refs == ()


# --- 4. model/analyst agreement manufactures no evidence --------------------

def test_many_analysts_agreeing_on_an_unsupported_resolution_is_still_not_evidence():
    """Ten "analysts" all independently propose the exact same
    (unsupported) resolution claim for the same ambiguity. Agreement
    between all of them changes nothing: with no real ResolutionEvidence
    supplied, the gate still ASKs, every time."""
    ambiguity = Ambiguity(
        id="package-manager", question="Which package manager?",
        rationale="r", risk_class=AmbiguityRiskClass.MATERIAL,
        evidence_keys=("repo:package_manager",),
        resolved_by_prompt_substring="package manager",
    )
    fakes = [FakePromptAnalyst([_analysis(PROMPT, (ambiguity,))]) for _ in range(10)]
    analyses = [fake.analyze(PROMPT, {}) for fake in fakes]

    results = [
        _gate().evaluate(original_prompt=PROMPT, analysis=a, resolutions=()) for a in analyses
    ]
    assert all(r.decision == GateDecision.ASK for r in results)
    assert all(r.evidence_refs == () for r in results)


# --- 5. destructive action marked ROUTINE still cannot be suppressed -------

def test_destructive_action_marked_routine_cannot_be_suppressed():
    ambiguity = Ambiguity(
        id="delete-scope",
        question="Should this permanently delete files?",
        rationale="Analyst insists this is routine, but deletion is irreversible.",
        risk_class=AmbiguityRiskClass.ROUTINE,  # mislabeled by the analyst
    )
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)), resolutions=(),
    )
    assert result.decision == GateDecision.ASK

    # Even a SAFE_DEFAULT/FACT resolution -- legitimate for a genuinely
    # routine choice -- must not be usable to wave through what is
    # actually destructive merely because the label says ROUTINE: the
    # allowed-kinds table is keyed by risk_class, so if a caller mistakes
    # this for routine and supplies a SAFE_DEFAULT, it would (structurally)
    # be accepted -- which is exactly why risk_class must be treated as
    # analyst-supplied metadata a caller should never blindly trust when
    # generating resolutions. This gate itself only guarantees: no
    # resolution supplied -> ASK, regardless of the claimed risk_class.
    unrelated_default = _resolution(
        "policy.default", Source.RUNTIME, ResolutionKind.SAFE_DEFAULT, "some-other-ambiguity",
    )
    result2 = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)),
        resolutions=(unrelated_default,),
    )
    assert result2.decision == GateDecision.ASK


# --- 6. repository evidence alone cannot authorize destructive action ------

def test_repository_evidence_cannot_authorize_destructive_action():
    ambiguity = Ambiguity(
        id="delete-scope", question="Should this permanently delete files?",
        rationale="Irreversible.", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    # Attempted AUTHORIZATION from a fact-only source is rejected outright.
    attempted_auth = _resolution(
        "repo.deployment_target", Source.REPOSITORY, ResolutionKind.AUTHORIZATION, "delete-scope",
    )
    # A plain FACT is not even the right kind for DESTRUCTIVE.
    plain_fact = _resolution(
        "repo.deployment_target", Source.REPOSITORY, ResolutionKind.FACT, "delete-scope",
    )
    for resolution in (attempted_auth, plain_fact):
        result = _gate().evaluate(
            original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)),
            resolutions=(resolution,),
        )
        assert result.decision == GateDecision.ASK, resolution


# --- 7. runtime evidence alone cannot authorize an external side effect ----

def test_runtime_evidence_cannot_authorize_external_side_effect():
    ambiguity = Ambiguity(
        id="notify-channel",
        question="Should this send a real notification to an external service now?",
        rationale="External consequences.", risk_class=AmbiguityRiskClass.EXTERNAL_SIDE_EFFECT,
    )
    attempted_auth = _resolution(
        "runtime.ci_env", Source.RUNTIME, ResolutionKind.AUTHORIZATION, "notify-channel",
    )
    plain_fact = _resolution(
        "runtime.ci_env", Source.RUNTIME, ResolutionKind.FACT, "notify-channel",
    )
    for resolution in (attempted_auth, plain_fact):
        result = _gate().evaluate(
            original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)),
            resolutions=(resolution,),
        )
        assert result.decision == GateDecision.ASK, resolution


# =========================================================================
# POSITIVE: independently supplied trusted resolution evidence
# =========================================================================

# --- 8/9/10. ORIGINAL_PROMPT / REPOSITORY / RUNTIME resolve MATERIAL -------

def test_original_prompt_trusted_evidence_resolves_material_ambiguity():
    ambiguity = Ambiguity(
        id="target-file", question="Which file should be modified?",
        rationale="r", risk_class=AmbiguityRiskClass.MATERIAL,
    )
    resolution = _resolution(
        "prompt.target_file", Source.ORIGINAL_PROMPT, ResolutionKind.FACT, "target-file",
    )
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)),
        resolutions=(resolution,),
    )
    assert result.decision == GateDecision.SUPPRESS
    assert result.evidence_refs == ("original_prompt:prompt.target_file:fact",)


def test_repository_trusted_evidence_resolves_material_ambiguity():
    ambiguity = Ambiguity(
        id="package-manager", question="Which package manager?",
        rationale="r", risk_class=AmbiguityRiskClass.MATERIAL,
    )
    resolution = _resolution(
        "repo.package_manager", Source.REPOSITORY, ResolutionKind.FACT, "package-manager",
    )
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)),
        resolutions=(resolution,),
    )
    assert result.decision == GateDecision.SUPPRESS


def test_runtime_trusted_evidence_resolves_material_ambiguity():
    ambiguity = Ambiguity(
        id="current-branch", question="Which branch is checked out?",
        rationale="r", risk_class=AmbiguityRiskClass.MATERIAL,
    )
    resolution = _resolution(
        "runtime.current_branch", Source.RUNTIME, ResolutionKind.FACT, "current-branch",
    )
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)),
        resolutions=(resolution,),
    )
    assert result.decision == GateDecision.SUPPRESS


# --- 11. explicit ORIGINAL_PROMPT authorization resolves DESTRUCTIVE -------

def test_original_prompt_authorization_resolves_destructive_ambiguity():
    prompt = "Permanently delete files in /var/log older than 30 days; this is authorized."
    ambiguity = Ambiguity(
        id="delete-scope", question="Should this permanently delete files?",
        rationale="Irreversible.", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    resolution = _resolution(
        "prompt.explicit_authorization", Source.ORIGINAL_PROMPT,
        ResolutionKind.AUTHORIZATION, "delete-scope",
    )
    result = _gate().evaluate(
        original_prompt=prompt, analysis=_analysis(prompt, (ambiguity,)),
        resolutions=(resolution,),
    )
    assert result.decision == GateDecision.SUPPRESS
    assert result.evidence_refs == ("original_prompt:prompt.explicit_authorization:authorization",)


# --- 12. durable prior human decision resolves EXTERNAL_SIDE_EFFECT --------

def test_durable_prior_human_decision_resolves_external_side_effect_ambiguity():
    ambiguity = Ambiguity(
        id="notify-channel",
        question="Should this send a real notification now?",
        rationale="External consequences.", risk_class=AmbiguityRiskClass.EXTERNAL_SIDE_EFFECT,
    )
    resolution = _resolution(
        "task.prior_notification_approval", Source.DURABLE_TASK_EVIDENCE,
        ResolutionKind.AUTHORIZATION, "notify-channel",
    )
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)),
        resolutions=(resolution,),
    )
    assert result.decision == GateDecision.SUPPRESS


# --- 13. code-owned SAFE_DEFAULT suppresses ROUTINE -------------------------

def test_code_owned_safe_default_suppresses_routine_ambiguity():
    ambiguity = Ambiguity(
        id="date-format", question="Should dates be zero-padded?",
        rationale="Cosmetic.", risk_class=AmbiguityRiskClass.ROUTINE,
    )
    resolution = _resolution(
        "codeslayer.safe_default.date_format", Source.RUNTIME,
        ResolutionKind.SAFE_DEFAULT, "date-format",
    )
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)),
        resolutions=(resolution,),
    )
    assert result.decision == GateDecision.SUPPRESS
    assert result.evidence_refs == ("runtime:codeslayer.safe_default.date_format:safe_default",)


# --- 14/15. resolution never leaks across ambiguity ids ---------------------

def test_unrelated_authoritative_evidence_cannot_leak_across_ambiguity_ids():
    ambiguity_b = Ambiguity(
        id="ambiguity-b", question="Question B?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    resolution_for_a_only = _resolution(
        "some.fact", Source.REPOSITORY, ResolutionKind.FACT, "ambiguity-a",
    )
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity_b,)),
        resolutions=(resolution_for_a_only,),
    )
    assert result.decision == GateDecision.ASK
    assert result.questions == ("Question B?",)


def test_resolution_for_ambiguity_a_does_not_suppress_ambiguity_b_in_the_same_analysis():
    ambiguity_a = Ambiguity(
        id="ambiguity-a", question="Question A?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    ambiguity_b = Ambiguity(
        id="ambiguity-b", question="Question B?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    resolution_for_a_only = _resolution(
        "some.fact", Source.REPOSITORY, ResolutionKind.FACT, "ambiguity-a",
    )
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity_a, ambiguity_b)),
        resolutions=(resolution_for_a_only,),
    )
    assert result.decision == GateDecision.ASK
    assert result.questions == ("Question B?",)  # A resolved and excluded, B still asked
    assert any(r == "ambiguity-a:resolved_by_trusted_evidence" for r in result.reasons)
    assert any(r.startswith("ambiguity-b:unresolved_") for r in result.reasons)


def test_one_resolution_can_explicitly_cover_multiple_ambiguity_ids():
    """`resolves_ambiguity_ids` may legitimately name more than one id --
    still always explicit, never a wildcard."""
    ambiguity_a = Ambiguity(
        id="ambiguity-a", question="Question A?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    ambiguity_b = Ambiguity(
        id="ambiguity-b", question="Question B?", rationale="r",
        risk_class=AmbiguityRiskClass.MATERIAL,
    )
    resolution = _resolution(
        "some.fact", Source.REPOSITORY, ResolutionKind.FACT, "ambiguity-a", "ambiguity-b",
    )
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity_a, ambiguity_b)),
        resolutions=(resolution,),
    )
    assert result.decision == GateDecision.SUPPRESS


# =========================================================================
# Conflicting analyst opinions never decide the gate
# =========================================================================

def test_conflicting_analyst_risk_classifications_are_each_judged_on_their_own():
    """Two analysts disagree about the SAME nominal question's risk. The
    gate has no mechanism to vote between them: each analysis is
    evaluated strictly on its own declared risk_class and the real,
    independently supplied resolutions -- never influenced by the
    other's disagreement, and never suppressed by disagreement alone."""
    routine_take = Ambiguity(
        id="cleanup-scope", question="Which temp directory?", rationale="r",
        risk_class=AmbiguityRiskClass.ROUTINE,
    )
    destructive_take = Ambiguity(
        id="cleanup-scope", question="Which temp directory?", rationale="r",
        risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    result_routine = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (routine_take,)), resolutions=(),
    )
    result_destructive = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (destructive_take,)), resolutions=(),
    )
    # Neither is suppressed just because the two disagree or agree --
    # with no resolution evidence at all, both fail closed to ASK.
    assert result_routine.decision == GateDecision.ASK
    assert result_destructive.decision == GateDecision.ASK


# =========================================================================
# ASK semantics: only unresolved material questions are ever returned
# =========================================================================

def test_only_unresolved_questions_are_returned():
    resolved = Ambiguity(
        id="logger", question="Which logger should be used?",
        rationale="r", risk_class=AmbiguityRiskClass.MATERIAL,
    )
    routine_with_default = Ambiguity(
        id="log-level", question="What default log level?",
        rationale="r", risk_class=AmbiguityRiskClass.ROUTINE,
    )
    unresolved_destructive = Ambiguity(
        id="delete-old-logs", question="Should old logs be permanently deleted?",
        rationale="Irreversible.", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    resolutions = (
        _resolution("prompt.logger", Source.ORIGINAL_PROMPT, ResolutionKind.FACT, "logger"),
        _resolution(
            "codeslayer.safe_default.log_level", Source.RUNTIME,
            ResolutionKind.SAFE_DEFAULT, "log-level",
        ),
    )
    result = _gate().evaluate(
        original_prompt=PROMPT,
        analysis=_analysis(PROMPT, (resolved, routine_with_default, unresolved_destructive)),
        resolutions=resolutions,
    )
    assert result.decision == GateDecision.ASK
    assert result.questions == ("Should old logs be permanently deleted?",)


def test_suppress_result_has_no_questions():
    ambiguity = Ambiguity(
        id="date-format", question="Should dates be zero-padded?",
        rationale="r", risk_class=AmbiguityRiskClass.ROUTINE,
    )
    resolution = _resolution(
        "codeslayer.safe_default.date_format", Source.RUNTIME,
        ResolutionKind.SAFE_DEFAULT, "date-format",
    )
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)),
        resolutions=(resolution,),
    )
    assert isinstance(result, QuestionGateResult)
    assert result.decision == GateDecision.SUPPRESS
    assert result.questions == ()


def test_empty_ambiguities_suppress_trivially():
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, ()), resolutions=(),
    )
    assert result.decision == GateDecision.SUPPRESS
    assert result.questions == ()
    assert result.reasons == ()


# =========================================================================
# 13 (task list). gate cannot grant trust/tool authority
# =========================================================================

def test_evaluate_signature_has_no_capability_bearing_parameters():
    params = set(inspect.signature(QuestionGate.evaluate).parameters) - {"self"}
    assert params == {"original_prompt", "analysis", "resolutions"}


def test_evaluate_does_not_touch_any_database(db_conn):
    tables = ("tasks", "worker_leases", "worker_trust_events", "tool_operations", "checkpoints")
    before = {t: [dict(r) for r in db_conn.execute(f"SELECT * FROM {t}")] for t in tables}

    ambiguity = Ambiguity(
        id="x", question="q?", rationale="r", risk_class=AmbiguityRiskClass.DESTRUCTIVE,
    )
    _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)), resolutions=(),
    )

    after = {t: [dict(r) for r in db_conn.execute(f"SELECT * FROM {t}")] for t in tables}
    assert after == before


# =========================================================================
# 14 (task list). malformed analyst result / resolution evidence fails closed
# =========================================================================

def test_none_analysis_fails_closed():
    result = _gate().evaluate(original_prompt=PROMPT, analysis=None, resolutions=())
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("malformed_prompt_analysis",)


def test_plain_dict_analysis_fails_closed():
    fake_analysis = {"original_prompt": PROMPT, "ambiguities": ()}
    result = _gate().evaluate(original_prompt=PROMPT, analysis=fake_analysis, resolutions=())
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("malformed_prompt_analysis",)


def test_fake_analyst_returning_malformed_result_is_caught_by_the_gate():
    analyst = FakePromptAnalyst([{"not": "a PromptAnalysis"}])
    produced = analyst.analyze(PROMPT, {})
    result = _gate().evaluate(original_prompt=PROMPT, analysis=produced, resolutions=())
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("malformed_prompt_analysis",)


def test_ambiguities_not_a_tuple_fails_closed():
    analysis = _analysis(PROMPT, ())
    object.__setattr__(analysis, "ambiguities", [1, 2, 3])
    result = _gate().evaluate(original_prompt=PROMPT, analysis=analysis, resolutions=())
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("malformed_prompt_analysis",)


def test_resolutions_not_a_tuple_fails_closed():
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, ()),
        resolutions=["not", "a", "tuple"],
    )
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("malformed_resolution_evidence",)


def test_resolutions_containing_non_resolution_evidence_fails_closed():
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, ()),
        resolutions=({"fake": "resolution"},),
    )
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("malformed_resolution_evidence",)


def test_unrecognized_resolution_source_is_ignored_not_trusted():
    ambiguity = Ambiguity(
        id="x", question="q?", rationale="r", risk_class=AmbiguityRiskClass.MATERIAL,
    )
    bad = _resolution("k", Source.REPOSITORY, ResolutionKind.FACT, "x")
    object.__setattr__(bad, "source", "not_a_real_source")
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)), resolutions=(bad,),
    )
    assert result.decision == GateDecision.ASK


def test_unrecognized_resolution_kind_is_ignored_not_trusted():
    ambiguity = Ambiguity(
        id="x", question="q?", rationale="r", risk_class=AmbiguityRiskClass.MATERIAL,
    )
    bad = _resolution("k", Source.REPOSITORY, ResolutionKind.FACT, "x")
    object.__setattr__(bad, "resolution_kind", "not_a_real_kind")
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=_analysis(PROMPT, (ambiguity,)), resolutions=(bad,),
    )
    assert result.decision == GateDecision.ASK


def test_malformed_risk_class_fails_closed():
    analysis = _analysis(PROMPT, ())
    bad_ambiguity = Ambiguity(
        id="x", question="q?", rationale="r", risk_class=AmbiguityRiskClass.ROUTINE,
    )
    object.__setattr__(bad_ambiguity, "risk_class", "not_a_real_risk_class")
    object.__setattr__(analysis, "ambiguities", (bad_ambiguity,))
    result = _gate().evaluate(original_prompt=PROMPT, analysis=analysis, resolutions=())
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("x:malformed_risk_class",)


# =========================================================================
# prompt-identity mismatch: analysis cannot replace the original prompt
# =========================================================================

def test_analysis_for_a_different_prompt_is_never_evaluated_as_this_prompt():
    other_prompt = "Completely different task: rewrite the billing system."
    ambiguity = Ambiguity(
        id="x", question="q?", rationale="r", risk_class=AmbiguityRiskClass.ROUTINE,
    )
    mismatched_analysis = _analysis(other_prompt, (ambiguity,))
    result = _gate().evaluate(
        original_prompt=PROMPT, analysis=mismatched_analysis, resolutions=(),
    )
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("prompt_identity_mismatch",)


def test_hash_mismatch_is_detected_even_with_no_ambiguities():
    analysis = _analysis("prompt A", ())
    result = _gate().evaluate(original_prompt="prompt B", analysis=analysis, resolutions=())
    assert result.decision == GateDecision.ASK
    assert result.reasons == ("prompt_identity_mismatch",)
    assert analysis.original_prompt_hash == hash_original_prompt("prompt A")
