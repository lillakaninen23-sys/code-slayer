"""Question Gate: SUPPRESS or ASK, decided from actual evidence only
(Phase 7.6 — `docs/CODE_SLAYER_VISION.md` §33 "Question Gate", §34
"Authority model", §35 "Risk-based autonomy").

## What this module is

The one place a proposed question — one `Ambiguity` from a
`workers.prompt_analysis.PromptAnalysis` — is turned into a deterministic
`SUPPRESS`/`ASK` decision. `QuestionGate.evaluate()` is pure: it holds no
database connection, lease, tool, or trust-management capability at all,
and the same `(original_prompt, analysis, evidence)` input always
produces the same `QuestionGateResult`. It never invents a requirement
that was not already present as an `Ambiguity`; it only filters and
resolves the ones it is given.

## SUPPRESS semantics

An ambiguity is suppressed only when:

1. its `resolved_by_prompt_substring` genuinely occurs, verbatim, in the
   real `original_prompt` passed to `evaluate()` — re-checked here, never
   taken on the analyst's word alone; or
2. one of its `evidence_keys` names a real entry in `evidence` whose
   `source` is an authoritative `EvidenceSource` (`ORIGINAL_PROMPT`,
   `REPOSITORY`, `RUNTIME`, `DURABLE_TASK_EVIDENCE`) — deterministically
   recoverable repository/runtime/task evidence, never an analyst's own
   claim; or
3. its `risk_class` is `AmbiguityRiskClass.ROUTINE` — a harmless
   implementation choice with a safe, reversible default, which Code
   Slayer applies automatically rather than interrupting the user for
   (`docs/CODE_SLAYER_VISION.md` §35).

Nothing else ever suppresses a question here. In particular: the analyst
merely *thinking* an answer is probably obvious, another model guessing
an answer, a majority of models agreeing, or a "convenient" default for
anything above `ROUTINE` are never suppression grounds — **LLM consensus
is not evidence** (§34). `EvidenceSource` has no member for "a model
said so" at all, so there is structurally no way to construct evidence
out of model agreement.

## ASK semantics

Every ambiguity that is not suppressed is asked, provided it is genuinely
present in the `PromptAnalysis` given to this call — this module invents
no ambiguities and no questions of its own. Missing/insufficient evidence
fails toward `ASK`, never toward inventing certainty: an ambiguity whose
`risk_class` is `MATERIAL`, `DESTRUCTIVE`, or `EXTERNAL_SIDE_EFFECT` and
that no authoritative evidence resolves is always asked.

## Fail-closed on malformed input

A non-`PromptAnalysis` analysis, a non-mapping `evidence`, a non-tuple/
non-`Ambiguity` ambiguities collection, or an `analysis.
original_prompt_hash` that disagrees with `hash_original_prompt(original_
prompt)` (the analysis was not actually produced from *this* exact
original prompt — never trusted as if it were) all resolve to `ASK` with
a diagnostic reason, exactly the same "never guess, never repair" posture
`policy.engine.PolicyEngine.evaluate()` and `lease.manager.LeaseManager`
already use for their own malformed input.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from code_slayer.workers.prompt_analysis import (
    Ambiguity,
    AmbiguityRiskClass,
    EvidenceContext,
    EvidenceItem,
    EvidenceSource,
    PromptAnalysis,
    hash_original_prompt,
)

# The complete set of `EvidenceSource` members `QuestionGate` ever trusts
# to resolve an ambiguity. Every member of `EvidenceSource` is listed
# here deliberately, by name, rather than "all of them" — a future
# addition to `EvidenceSource` must be a conscious decision about
# whether it belongs in this gate's authority model too, never an
# accidental inclusion.
_AUTHORITATIVE_SOURCES = frozenset({
    EvidenceSource.ORIGINAL_PROMPT, EvidenceSource.REPOSITORY,
    EvidenceSource.RUNTIME, EvidenceSource.DURABLE_TASK_EVIDENCE,
})


class GateDecision(StrEnum):
    SUPPRESS = "SUPPRESS"
    ASK = "ASK"


@dataclass(frozen=True)
class QuestionGateResult:
    """`decision` is the only thing a caller must act on. For `SUPPRESS`,
    `questions` is normally empty. For `ASK`, `questions` contains only
    the materially unresolved questions — never every ambiguity the
    analysis proposed. `reasons` and `evidence_refs` are diagnostic/audit
    detail: `reasons` names, per ambiguity id, why it was asked or
    suppressed; `evidence_refs` names, for each *resolved* ambiguity,
    which authoritative evidence resolved it."""

    decision: GateDecision
    questions: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


def _ask(reason: str) -> QuestionGateResult:
    return QuestionGateResult(GateDecision.ASK, reasons=(reason,))


def _resolution(
    ambiguity: Ambiguity, original_prompt: str, evidence: EvidenceContext,
) -> str | None:
    """A short evidence reference string if `ambiguity` is genuinely
    resolved by authoritative evidence, else `None`. Never trusts the
    analyst's own claim alone — re-checks the prompt substring against
    the real prompt text, and the evidence key against a real,
    authoritatively-sourced `EvidenceContext` entry."""
    substring = ambiguity.resolved_by_prompt_substring
    if isinstance(substring, str) and substring and substring in original_prompt:
        return f"original_prompt:{substring!r}"
    for key in ambiguity.evidence_keys:
        item = evidence.get(key) if isinstance(evidence, Mapping) else None
        if isinstance(item, EvidenceItem) and item.source in _AUTHORITATIVE_SOURCES:
            return f"{item.source.value.lower()}:{key}"
    return None


class QuestionGate:
    """Stateless; holds no database connection, lease, or tool access —
    see the module docstring. `evaluate()` is pure and deterministic."""

    def evaluate(
        self, *, original_prompt: str, analysis: PromptAnalysis, evidence: EvidenceContext,
    ) -> QuestionGateResult:
        if not isinstance(original_prompt, str):
            return _ask("malformed_original_prompt")
        if not isinstance(analysis, PromptAnalysis):
            return _ask("malformed_prompt_analysis")
        if not isinstance(analysis.ambiguities, tuple) or not all(
            isinstance(a, Ambiguity) for a in analysis.ambiguities
        ):
            return _ask("malformed_prompt_analysis")
        if not isinstance(evidence, Mapping):
            return _ask("malformed_evidence_context")
        if analysis.original_prompt_hash != hash_original_prompt(original_prompt):
            # This analysis was not actually produced from THIS exact
            # original prompt -- never evaluated as if it were. The
            # original prompt, never the analysis, is what this gate is
            # ultimately answerable to (§59, Immutable task intent).
            return _ask("prompt_identity_mismatch")

        questions: list[str] = []
        reasons: list[str] = []
        evidence_refs: list[str] = []
        for ambiguity in analysis.ambiguities:
            if not isinstance(ambiguity.id, str) or not ambiguity.id:
                return _ask("malformed_ambiguity_id")
            if not isinstance(ambiguity.question, str) or not ambiguity.question:
                return _ask(f"{ambiguity.id}:malformed_question")
            if not isinstance(ambiguity.risk_class, AmbiguityRiskClass):
                return _ask(f"{ambiguity.id}:malformed_risk_class")

            ref = _resolution(ambiguity, original_prompt, evidence)
            if ref is not None:
                evidence_refs.append(ref)
                reasons.append(f"{ambiguity.id}:resolved_by_evidence")
                continue
            if ambiguity.risk_class == AmbiguityRiskClass.ROUTINE:
                reasons.append(f"{ambiguity.id}:routine_default_applied")
                continue
            questions.append(ambiguity.question)
            reasons.append(f"{ambiguity.id}:unresolved_{ambiguity.risk_class.value.lower()}")

        if questions:
            return QuestionGateResult(
                GateDecision.ASK, tuple(questions), tuple(reasons), tuple(evidence_refs),
            )
        return QuestionGateResult(GateDecision.SUPPRESS, (), tuple(reasons), tuple(evidence_refs))
