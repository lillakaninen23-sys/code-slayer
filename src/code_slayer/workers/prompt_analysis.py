"""Prompt Analyst: provider-neutral, deterministic-first structured
analysis of the original user prompt (Phase 7.6 — `docs/ROADMAP.md`'s
Local Worker Runtime stage, step 5 "Prompt / question intelligence";
`docs/CODE_SLAYER_VISION.md` §32 "Prompt Analyst", §34 "Authority model",
§59 "Immutable task intent").

## Core principle: the original prompt is authoritative, analysis is not

```
original_prompt                      <- authoritative, immutable intent
      |
      v
  PromptAnalyst.analyze()
      |
      v
  PromptAnalysis                     <- SUPPLEMENTAL structured analysis
      |
      v
  QuestionGate.evaluate()            <- workers.question_gate
      |
      v
  SUPPRESS or ASK
```

`PromptAnalysis` never replaces, rewrites, shortens, or "improves" the
original prompt (`docs/CODE_SLAYER_VISION.md` §32's "critical
invariant"). `original_prompt` is carried on the analysis object
verbatim, and `original_prompt_hash` is computed *by this module*, from
that exact field, the moment the object is constructed — never accepted
as a value a `PromptAnalyst` implementation supplies. However buggy or
adversarial a concrete analyst is, it cannot make a `PromptAnalysis`
object claim a hash the prompt text does not actually have; the only way
to get a specific hash onto this object is to actually carry that exact
prompt text (see `hash_original_prompt()`).

## Deterministic-first, no filesystem/tool access

`PromptAnalyst` is a minimal `typing.Protocol` — a real implementation
may eventually wrap a model call, but this phase provides only
`workers.fake_prompt_analyst.FakePromptAnalyst`, a deterministic,
fully-offline test double (mirroring `workers.fake_adapter.
FakeWorkerAdapter`'s own pattern), so ordinary `pytest` never depends on
a real model/provider. `analyze()` receives the exact original prompt
and a read-only `EvidenceContext` — never a database connection, a
`ToolExecutor`, a lease, or any other mutating capability (see this
module's "Security" note below).

## No model-consensus evidence, and no analyst self-resolution either

`EvidenceSource` intentionally has no "the analyst/model said so" member.
Every source in it is something Code Slayer itself deterministically
established — the original prompt's own text, real repository state,
real runtime state, or prior durable task evidence
(`docs/CODE_SLAYER_VISION.md` §34's authority model).

An `Ambiguity`'s own `resolved_by_prompt_substring`/`evidence_keys` are
the *analyst's own, non-binding hints* about what might resolve its
question — never proof that it already is resolved, and never
sufficient by themselves to suppress anything. The self-resolution
problem this guards against: an analyst that proposes an ambiguity *and*
proposes the very thing that resolves it would otherwise be able to
suppress its own question merely by asserting both halves. Resolving an
ambiguity is `workers.question_gate.ResolutionEvidence`'s job — a
structure the analyst cannot construct, mutate, or influence at all
(nothing in this module imports `workers.question_gate`, and a
`PromptAnalyst` never receives it). `QuestionGate.evaluate()` requires an
independently supplied `ResolutionEvidence` that *explicitly* names the
exact ambiguity id it resolves; an analyst's hint at most tells a caller
what evidence might be worth gathering, never that gathering it is
unnecessary. Nothing here can turn "two models agree," "an analyst
asserts X," or "the analyst also happened to name the evidence that
would answer its own question" into an actual resolution.

## Security

A `PromptAnalyst` and the `PromptAnalysis` it produces cannot mutate
files, execute tools, bypass `policy.engine.PolicyEngine`, grant worker
trust, change leases, create checkpoints, modify the original prompt, or
authorize destructive/external behavior — structurally, because nothing
in this module imports or accepts any of those capabilities. This is an
advisory/decision component only.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


def hash_original_prompt(original_prompt: str) -> str:
    """The exact-bytes SHA-256 identity of `original_prompt` — UTF-8
    encoded, no whitespace normalization, no canonicalization of any
    kind (the project has no existing canonical prompt format, so none
    is invented here). Two prompts differing by even a single byte hash
    differently; this is the sole authority `PromptAnalysis.
    original_prompt_hash` and `workers.question_gate.QuestionGate`'s own
    identity check are both built on."""
    if not isinstance(original_prompt, str):
        raise TypeError("original_prompt must be a str")
    return hashlib.sha256(original_prompt.encode("utf-8")).hexdigest()


class EvidenceSource(StrEnum):
    """Where one `EvidenceContext` entry deterministically came from —
    the complete set of channels `workers.question_gate.QuestionGate`
    ever treats as authoritative (`docs/CODE_SLAYER_VISION.md` §34).
    There is deliberately no `ANALYST_CLAIM`/model-opinion member: an
    analyst's own assertion can never itself be an `EvidenceSource` —
    see the module docstring's "No model-consensus evidence" section."""

    ORIGINAL_PROMPT = "ORIGINAL_PROMPT"
    REPOSITORY = "REPOSITORY"
    RUNTIME = "RUNTIME"
    DURABLE_TASK_EVIDENCE = "DURABLE_TASK_EVIDENCE"


@dataclass(frozen=True)
class EvidenceItem:
    """One deterministic fact Code Slayer itself established — never an
    analyst's or model's own assertion. `source` names which
    authoritative channel (§34) produced `value`; `detail` is a short,
    human-readable trace of exactly how (a file path, a command that was
    run, a prompt excerpt) — never large content, never raw secrets."""

    source: EvidenceSource
    value: str
    detail: str = ""


# A read-only bag of deterministic facts a caller gives `PromptAnalyst.
# analyze()` to inform its own reasoning, keyed by a short stable name
# (e.g. "repo:package_manager", "runtime:current_branch") an `Ambiguity.
# evidence_keys` entry may later reference as a hint. Never constructed
# from an analyst's own output — only from something Code Slayer itself
# established. This context is informational input to the analyst only:
# `workers.question_gate.QuestionGate` does not accept it and never
# resolves an ambiguity merely because a same-named entry exists here —
# see `workers.question_gate.ResolutionEvidence` for what actually does.
EvidenceContext = Mapping[str, EvidenceItem]


class AmbiguityRiskClass(StrEnum):
    """How materially an unresolved ambiguity matters
    (`docs/CODE_SLAYER_VISION.md` §35 "Risk-based autonomy") — the
    Question Gate's only signal for whether a conservative default may
    be applied automatically instead of asking.

    `ROUTINE` — a harmless implementation choice with a safe, reversible
    default already available; never worth interrupting the user for on
    its own (§35: "an unresolved-but-low-stakes question is not a reason
    to stop").
    `MATERIAL` — no authoritative answer exists, and the choice
    measurably affects correctness.
    `DESTRUCTIVE` — the ambiguous choice could damage data, state,
    history, or security, or is otherwise irreversible.
    `EXTERNAL_SIDE_EFFECT` — the ambiguous choice has consequences
    outside Code Slayer's own managed state (network access, third-party
    services, anything not cheaply undone locally).

    Only `ROUTINE` can ever be suppressed without authoritative evidence
    resolving it; every other class is asked unless evidence resolves it.
    """

    ROUTINE = "ROUTINE"
    MATERIAL = "MATERIAL"
    DESTRUCTIVE = "DESTRUCTIVE"
    EXTERNAL_SIDE_EFFECT = "EXTERNAL_SIDE_EFFECT"


@dataclass(frozen=True)
class Ambiguity:
    """One structured, potentially question-worthy gap the analyst
    identified — never itself a question the user is guaranteed to see;
    `workers.question_gate.QuestionGate` decides that.

    `id` is a short, stable, caller-chosen identifier for exactly this
    ambiguity (used to reference it in a durable gate-decision record —
    `workers.prompt_provenance`) — never regenerated per analysis run for
    the same underlying question.

    `resolved_by_prompt_substring` and `evidence_keys` are the analyst's
    own **non-binding hints** — advisory provenance about what it *thinks*
    might already answer this question, never proof that it does.
    `resolved_by_prompt_substring`, when not `None`, is the analyst's
    claim that this exact substring, verbatim, appears in the original
    prompt and answers the question; `evidence_keys` names
    `EvidenceContext` keys the analyst thinks are relevant. Neither field
    is ever, by itself, sufficient for `workers.question_gate.
    QuestionGate` to suppress this ambiguity — an analyst that proposes
    both an ambiguity *and* the thing that supposedly resolves it must
    never be able to suppress its own question merely by asserting both
    halves. A caller may still find these hints useful for deciding what
    to inspect, but actually resolving the ambiguity requires an
    independently supplied `workers.question_gate.ResolutionEvidence`
    that explicitly names this ambiguity's `id` — a structure the analyst
    can never construct, mutate, or see (see `workers.question_gate`'s
    module docstring).
    """

    id: str
    question: str
    rationale: str
    risk_class: AmbiguityRiskClass
    evidence_keys: tuple[str, ...] = ()
    resolved_by_prompt_substring: str | None = None


@dataclass(frozen=True)
class PromptAnalysis:
    """Supplemental structured analysis of one `original_prompt` — never
    a replacement, paraphrase, summary, or authoritative substitute for
    it (`docs/CODE_SLAYER_VISION.md` §32).

    `original_prompt_hash` is computed here, from `original_prompt`
    itself, the instant this object is constructed — never accepted as a
    caller-supplied field (`field(init=False)`), so no `PromptAnalyst`
    implementation, however buggy or adversarial, can ever make this
    object claim a hash the prompt text does not actually have.
    """

    original_prompt: str
    goals: tuple[str, ...] = ()
    explicit_requirements: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    already_answered: tuple[str, ...] = ()
    ambiguities: tuple[Ambiguity, ...] = ()
    risk_points: tuple[str, ...] = ()
    original_prompt_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "original_prompt_hash", hash_original_prompt(self.original_prompt),
        )


class PromptAnalystError(RuntimeError):
    """The stable, typed exception a real `PromptAnalyst.analyze()`
    implementation raises when it cannot produce a valid `PromptAnalysis`
    at all — transport failure, a non-conforming model response, or
    malformed structured output. Mirrors `workers.protocol.
    WorkerAdapterError`'s exact role for `WorkerAdapter.infer()`: the one
    typed failure boundary every caller of this protocol can catch,
    without needing to know which concrete `PromptAnalyst`
    implementation it is holding.

    `analyze()`'s own return type is `PromptAnalysis` unconditionally —
    there is no "malformed" variant to return instead (unlike `planning.
    planner.PlannerResponse`) — so a real implementation raises this
    rather than ever fabricating an empty, ambiguity-free `PromptAnalysis`
    on failure: an empty analysis would silently suppress every question
    `workers.question_gate.QuestionGate` might otherwise have asked,
    exactly the fail-open failure mode this codebase's "fail closed"
    posture forbids. A caller (`runner.local_worker_runner.
    LocalWorkerRunner.start()`) catches exactly this type to terminate a
    run cleanly and durably, never a bare `Exception`, so a genuine
    programming defect in an analyst implementation still propagates
    rather than being silently absorbed as an ordinary analysis failure.

    Deliberately distinct from `workers.fake_prompt_analyst.
    FakePromptAnalystError`, which signals a *test-harness*
    misconfiguration (a fake's canned response queue was exhausted) —
    never a real analysis failure a caller should catch and durably
    record. The two are intentionally not related by inheritance: a
    caller that catches this type must never accidentally swallow a
    broken test double's own loud failure."""


class PromptAnalyst(Protocol):
    """The one method a real or fake Prompt Analyst implements.
    Deliberately given no filesystem, tool, or database access here —
    only the exact original prompt and whatever deterministic
    `EvidenceContext` the caller already collected; see the module
    docstring's "Security" section. A concrete adapter needs no base
    class — this is structural (`typing.Protocol`), matching `workers.
    protocol.WorkerAdapter`'s own pattern.

    Raises `PromptAnalystError` when a real implementation cannot
    produce a valid `PromptAnalysis` at all — see that exception's own
    docstring. Never returns a fabricated/empty analysis on failure."""

    def analyze(self, original_prompt: str, evidence: EvidenceContext) -> PromptAnalysis: ...


# -- structured output parsing: strict schema, never heuristic prose --------
#
# Mirrors `planning.planner.parse_planner_output()`'s own "strict,
# whole-shape schema validation, never a partial/best-effort
# reconstruction" discipline exactly, applied to `PromptAnalysis`
# instead of `PlannerStructuredOutput` — the schema this exact module
# owns, so its parser lives here rather than beside whichever adapter
# happens to produce raw model output (`workers.worker_prompt_analyst.
# WorkerAdapterPromptAnalyst` today). Deliberately duplicated, rather
# than imported, from `planning.planner`'s equivalent helpers: `workers`
# is a lower layer than `planning` and must not depend on it (the same
# "mirrors... without importing" posture `WorkerSupplementalKind`/
# `WorkerSupplementalSource` already use against `workers.question_gate`/
# this module's own `EvidenceSource`).


@dataclass(frozen=True)
class PromptAnalysisFields:
    """Exactly the subset of `PromptAnalysis`'s own fields a model turn
    may supply — `original_prompt` is deliberately excluded: only the
    caller that actually holds the true original prompt text may ever
    set that field (see `PromptAnalysis.__post_init__` and this module's
    "the original prompt is authoritative" principle)."""

    goals: tuple[str, ...] = ()
    explicit_requirements: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    already_answered: tuple[str, ...] = ()
    risk_points: tuple[str, ...] = ()
    ambiguities: tuple[Ambiguity, ...] = ()


def _is_str_tuple(value: object) -> bool:
    return isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value)


def _parse_ambiguity(item: object) -> Ambiguity | None:
    if not isinstance(item, Mapping):
        return None
    id_, question, rationale = item.get("id"), item.get("question"), item.get("rationale")
    risk = item.get("risk_class")
    if not all(isinstance(v, str) and v for v in (id_, question, rationale)):
        return None
    if not isinstance(risk, str) or risk not in AmbiguityRiskClass.__members__:
        return None
    evidence_keys = item.get("evidence_keys", [])
    if not _is_str_tuple(evidence_keys):
        return None
    substring = item.get("resolved_by_prompt_substring")
    if substring is not None and not isinstance(substring, str):
        return None
    return Ambiguity(
        id=id_, question=question, rationale=rationale,
        risk_class=AmbiguityRiskClass(risk), evidence_keys=tuple(evidence_keys),
        resolved_by_prompt_substring=substring,
    )


def _parse_list(items: object, parser) -> tuple | None:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        return None
    parsed = []
    for item in items:
        result = parser(item)
        if result is None:
            return None
        parsed.append(result)
    return tuple(parsed)


_ALLOWED_FIELDS = frozenset({
    "goals", "explicit_requirements", "constraints", "already_answered",
    "risk_points", "ambiguities",
})


def parse_prompt_analysis_output(data: object) -> PromptAnalysisFields | None:
    """Strict, whole-shape schema validation of one model turn's raw
    `emit_prompt_analysis` tool-call parameters — `None` on anything
    that does not exactly match, never a partial or best-effort
    reconstruction (mirrors `planning.planner.parse_planner_output()`).
    An unrecognized top-level field rejects the whole payload. The
    caller (`workers.worker_prompt_analyst.WorkerAdapterPromptAnalyst`)
    combines the result with the one true `original_prompt` string to
    build a real `PromptAnalysis`."""
    if not isinstance(data, Mapping):
        return None
    if set(data) - _ALLOWED_FIELDS:
        return None
    for key in ("goals", "explicit_requirements", "constraints", "already_answered",
                "risk_points"):
        if key in data and not _is_str_tuple(data[key]):
            return None
    ambiguities = _parse_list(data.get("ambiguities", []), _parse_ambiguity)
    if ambiguities is None:
        return None
    return PromptAnalysisFields(
        goals=tuple(data.get("goals", ())),
        explicit_requirements=tuple(data.get("explicit_requirements", ())),
        constraints=tuple(data.get("constraints", ())),
        already_answered=tuple(data.get("already_answered", ())),
        risk_points=tuple(data.get("risk_points", ())),
        ambiguities=ambiguities,
    )
