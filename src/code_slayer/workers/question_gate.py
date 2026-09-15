"""Question Gate: SUPPRESS or ASK, decided from trusted, independently
supplied resolution evidence only — never from anything the Prompt
Analyst itself asserted (Phase 7.6, hardened — `docs/CODE_SLAYER_VISION.
md` §33 "Question Gate", §34 "Authority model", §35 "Risk-based
autonomy").

## The self-resolution problem this module closes

The Prompt Analyst is advisory. Before this hardening, `Ambiguity.
resolved_by_prompt_substring`/`evidence_keys` and `risk_class` were
*themselves* enough to suppress a question: an analyst that proposed an
ambiguity and also proposed the very thing that resolves it could
suppress its own question just by asserting both halves — a
self-resolution loophole, and a `risk_class == ROUTINE` claim
automatically suppressed regardless of evidence. Neither is true here.

`QuestionGate.evaluate()` now takes a separate, independently supplied
`resolutions: tuple[ResolutionEvidence, ...]` argument — a structure the
Prompt Analyst never sees, never constructs, and never mutates (nothing
in `workers.prompt_analysis` imports this module). An `Ambiguity`'s own
`evidence_keys`/`resolved_by_prompt_substring` are, at most, a hint about
*what* a caller might want to check; they are never themselves checked
by this module, and never sufficient to resolve anything. Only a real
`ResolutionEvidence` — supplied by the caller from Code Slayer's own
repository/runtime inspection, the original prompt's own explicit text,
a durable prior human decision, or a code-owned deterministic default —
that **explicitly names the exact ambiguity id it resolves** can ever
suppress a question. Merely existing, or being pointed at by the
analyst's own `evidence_keys`, is never enough.

## SUPPRESS semantics

An ambiguity is suppressed only when at least one `ResolutionEvidence` in
`resolutions`:

1. names this ambiguity's exact `id` in `resolves_ambiguity_ids`
   (unrelated evidence — even genuinely authoritative evidence bound to a
   *different* ambiguity id — resolves nothing here);
2. carries an authoritative `source` (`ORIGINAL_PROMPT`, `REPOSITORY`,
   `RUNTIME`, `DURABLE_TASK_EVIDENCE`); and
3. carries a `resolution_kind` this ambiguity's `risk_class` actually
   permits (see "Risk-specific authority" below).

## Risk-specific authority

Not every `resolution_kind` is acceptable for every `risk_class`:

- `ROUTINE` — `SAFE_DEFAULT` (a code-owned, deterministic default —
  never analyst-supplied), `FACT`, or `AUTHORIZATION`. Without any
  trusted resolution, `ROUTINE` fails closed to `ASK`, same as every
  other risk class — a bare analyst claim of `ROUTINE` never suppresses
  anything by itself.
- `MATERIAL` — `FACT` or `AUTHORIZATION`: a deterministic repository/
  runtime/prompt/task fact genuinely answers a factual question.
- `DESTRUCTIVE` / `EXTERNAL_SIDE_EFFECT` — `AUTHORIZATION` only, and
  only from `ORIGINAL_PROMPT` or `DURABLE_TASK_EVIDENCE` (an explicit
  prior human decision) — never `REPOSITORY`/`RUNTIME` alone, and never
  a `FACT`/`SAFE_DEFAULT`, however authoritative. A fact ("the
  repository's deployment target is production") is never the same
  thing as an authorization ("the user authorized deploying to
  production") — keeping them structurally distinct is exactly what
  prevents a true-but-irrelevant fact from ever standing in for consent.

## No model-consensus evidence

`EvidenceSource` (`workers.prompt_analysis`) has no "the analyst/model
said so" member — model agreement, however many analysts agree, cannot
be expressed as a `ResolutionEvidence` at all, so it can never resolve
anything here.

## ASK semantics

Every ambiguity without a qualifying `ResolutionEvidence` is asked,
verbatim, exactly as the analysis proposed it — this module invents no
ambiguities and no questions of its own, and never rewrites one it is
given.

## Fail-closed on malformed input

A non-`PromptAnalysis` analysis, a non-tuple/non-`ResolutionEvidence`
`resolutions` collection, a non-tuple/non-`Ambiguity` ambiguities
collection, an unrecognized `source`/`resolution_kind` on an individual
`ResolutionEvidence` (never trusted, that specific item is simply
treated as not resolving anything), or an `analysis.original_prompt_hash`
that disagrees with `hash_original_prompt(original_prompt)` (the
analysis was not actually produced from *this* exact original prompt)
all fail toward `ASK` — the same "never guess, never repair" posture
`policy.engine.PolicyEngine.evaluate()` and `lease.manager.LeaseManager`
already use for their own malformed input.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from code_slayer.workers.prompt_analysis import (
    Ambiguity,
    AmbiguityRiskClass,
    EvidenceSource,
    PromptAnalysis,
    hash_original_prompt,
)

# The complete set of `EvidenceSource` members `QuestionGate` ever trusts
# at all. Every member of `EvidenceSource` is listed here deliberately,
# by name, rather than "all of them" — a future addition to
# `EvidenceSource` must be a conscious decision about whether it belongs
# in this gate's authority model too, never an accidental inclusion.
_AUTHORITATIVE_SOURCES = frozenset({
    EvidenceSource.ORIGINAL_PROMPT, EvidenceSource.REPOSITORY,
    EvidenceSource.RUNTIME, EvidenceSource.DURABLE_TASK_EVIDENCE,
})

# The narrower set of sources that may ever carry `ResolutionKind.
# AUTHORIZATION` — an explicit prior human decision. Deliberately
# excludes REPOSITORY/RUNTIME: a fact about the world is never itself
# permission to act destructively or externally on it.
_AUTHORIZATION_SOURCES = frozenset({
    EvidenceSource.ORIGINAL_PROMPT, EvidenceSource.DURABLE_TASK_EVIDENCE,
})


class ResolutionKind(StrEnum):
    """What kind of trusted resolution a `ResolutionEvidence` represents
    — never itself analyst-supplied.

    `FACT` — a deterministic fact (what package manager, what branch,
    ...) that genuinely answers a `MATERIAL` question, but is never
    itself permission to do anything destructive or externally
    consequential.
    `AUTHORIZATION` — an explicit prior human decision authorizing a
    specific destructive or externally-consequential action. The only
    kind that can ever resolve `DESTRUCTIVE`/`EXTERNAL_SIDE_EFFECT`, and
    only from `_AUTHORIZATION_SOURCES`.
    `SAFE_DEFAULT` — a code-owned, deterministic default Code Slayer
    itself applies for a genuinely harmless (`ROUTINE`) choice. Never
    something the Prompt Analyst proposes or supplies.
    """

    FACT = "FACT"
    AUTHORIZATION = "AUTHORIZATION"
    SAFE_DEFAULT = "SAFE_DEFAULT"


# Which ResolutionKind(s) may resolve an ambiguity of each risk class.
# DESTRUCTIVE/EXTERNAL_SIDE_EFFECT admit only AUTHORIZATION — no fact or
# default, however authoritative its source, ever substitutes for an
# explicit human decision on an irreversible or externally-consequential
# action. Every AmbiguityRiskClass member is listed explicitly (an
# ambiguity whose risk_class this table has no entry for resolves to
# nothing, i.e. always ASK).
_ALLOWED_RESOLUTION_KINDS: dict[AmbiguityRiskClass, frozenset[ResolutionKind]] = {
    AmbiguityRiskClass.ROUTINE: frozenset({
        ResolutionKind.SAFE_DEFAULT, ResolutionKind.FACT, ResolutionKind.AUTHORIZATION,
    }),
    AmbiguityRiskClass.MATERIAL: frozenset({ResolutionKind.FACT, ResolutionKind.AUTHORIZATION}),
    AmbiguityRiskClass.DESTRUCTIVE: frozenset({ResolutionKind.AUTHORIZATION}),
    AmbiguityRiskClass.EXTERNAL_SIDE_EFFECT: frozenset({ResolutionKind.AUTHORIZATION}),
}


@dataclass(frozen=True)
class ResolutionEvidence:
    """One trusted, explicit resolution — wholly independent of anything
    the Prompt Analyst proposed. The analyst cannot create, mutate, or
    even see this structure: it is supplied by the *caller* of
    `QuestionGate.evaluate()`, sourced from Code Slayer's own
    repository/runtime inspection, the original prompt's own explicit
    text, a durable prior human decision, or a code-owned deterministic
    default — never derived from `PromptAnalysis`.

    `resolves_ambiguity_ids` must explicitly name the ambiguity id(s)
    this evidence resolves. Merely existing in some general evidence
    bag is never enough — see the module docstring's "self-resolution
    problem" section. `key` is a short, human-readable trace (e.g.
    "repo.head", "prompt:authorization") for audit/evidence_refs
    purposes only; it plays no role in matching.
    """

    key: str
    source: EvidenceSource
    resolution_kind: ResolutionKind
    resolves_ambiguity_ids: tuple[str, ...]
    detail: str = ""


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
    which trusted resolution evidence resolved it."""

    decision: GateDecision
    questions: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


def _ask(reason: str) -> QuestionGateResult:
    return QuestionGateResult(GateDecision.ASK, reasons=(reason,))


def _find_resolution(
    ambiguity: Ambiguity, resolutions: tuple[ResolutionEvidence, ...],
) -> ResolutionEvidence | None:
    """The first trusted `ResolutionEvidence` that explicitly resolves
    `ambiguity` under its own `risk_class`'s authority rules, or `None`.

    Deliberately never consults `ambiguity.evidence_keys` or
    `resolved_by_prompt_substring` — those are the analyst's own,
    non-binding hints; only an independently supplied `ResolutionEvidence`
    naming `ambiguity.id` can resolve anything (see the module
    docstring)."""
    allowed_kinds = _ALLOWED_RESOLUTION_KINDS.get(ambiguity.risk_class)
    if not allowed_kinds:
        return None
    for item in resolutions:
        if ambiguity.id not in item.resolves_ambiguity_ids:
            continue
        if not isinstance(item.source, EvidenceSource) or item.source not in _AUTHORITATIVE_SOURCES:
            continue
        if not isinstance(item.resolution_kind, ResolutionKind):
            continue
        if item.resolution_kind not in allowed_kinds:
            continue
        if (
            item.resolution_kind == ResolutionKind.AUTHORIZATION
            and item.source not in _AUTHORIZATION_SOURCES
        ):
            # A fact about the world (repository/runtime state) is never
            # itself permission to act — see "Risk-specific authority".
            continue
        return item
    return None


class QuestionGate:
    """Stateless; holds no database connection, lease, or tool access —
    see the module docstring. `evaluate()` is pure and deterministic."""

    def evaluate(
        self, *, original_prompt: str, analysis: PromptAnalysis,
        resolutions: tuple[ResolutionEvidence, ...] = (),
    ) -> QuestionGateResult:
        if not isinstance(original_prompt, str):
            return _ask("malformed_original_prompt")
        if not isinstance(analysis, PromptAnalysis):
            return _ask("malformed_prompt_analysis")
        if not isinstance(analysis.ambiguities, tuple) or not all(
            isinstance(a, Ambiguity) for a in analysis.ambiguities
        ):
            return _ask("malformed_prompt_analysis")
        if not isinstance(resolutions, tuple) or not all(
            isinstance(r, ResolutionEvidence) for r in resolutions
        ):
            return _ask("malformed_resolution_evidence")
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

            resolution = _find_resolution(ambiguity, resolutions)
            if resolution is not None:
                evidence_refs.append(
                    f"{resolution.source.value.lower()}:{resolution.key}:"
                    f"{resolution.resolution_kind.value.lower()}"
                )
                reasons.append(f"{ambiguity.id}:resolved_by_trusted_evidence")
                continue
            questions.append(ambiguity.question)
            reasons.append(f"{ambiguity.id}:unresolved_{ambiguity.risk_class.value.lower()}")

        if questions:
            return QuestionGateResult(
                GateDecision.ASK, tuple(questions), tuple(reasons), tuple(evidence_refs),
            )
        return QuestionGateResult(GateDecision.SUPPRESS, (), tuple(reasons), tuple(evidence_refs))
