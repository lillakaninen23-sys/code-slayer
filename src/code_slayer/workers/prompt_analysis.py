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

## No model-consensus evidence

`EvidenceSource` intentionally has no "the analyst/model said so" member.
Every source in it is something Code Slayer itself deterministically
established — the original prompt's own text, real repository state,
real runtime state, or prior durable task evidence
(`docs/CODE_SLAYER_VISION.md` §34's authority model). An `Ambiguity`'s
own `resolved_by_prompt_substring`/`evidence_keys` are the *analyst's*
proposal of what kind of fact would resolve its question — never treated
as already-resolved by this module alone. `workers.question_gate.
QuestionGate` is what independently re-checks a proposed prompt
substring against the real prompt text, and a proposed evidence key
against a real, authoritatively-sourced `EvidenceContext` entry, before
ever suppressing a question. Nothing here can turn "two models agree" or
"an analyst asserts X" into evidence by itself.

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
from collections.abc import Mapping
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


# A read-only bag of deterministic facts, keyed by a short stable name an
# `Ambiguity.evidence_keys` entry can reference (e.g. "repo:package_manager",
# "runtime:current_branch"). Never constructed from an analyst's own
# output — only from something Code Slayer itself established.
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

    `resolved_by_prompt_substring`, when not `None`, is the analyst's own
    claim that this *exact* substring, verbatim, already appears in the
    original prompt and answers the question. `QuestionGate` independently
    re-checks that the substring genuinely occurs in the real original
    prompt text before ever trusting it — never taken on the analyst's
    word alone.

    `evidence_keys` names the `EvidenceContext` keys that, if present and
    carrying an authoritative `EvidenceSource`, deterministically resolve
    this ambiguity. The analyst proposes *what kind* of fact would answer
    its own question; only `QuestionGate`, checking the real
    `EvidenceContext`, decides whether that fact is actually available.
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


class PromptAnalyst(Protocol):
    """The one method a real or fake Prompt Analyst implements.
    Deliberately given no filesystem, tool, or database access here —
    only the exact original prompt and whatever deterministic
    `EvidenceContext` the caller already collected; see the module
    docstring's "Security" section. A concrete adapter needs no base
    class — this is structural (`typing.Protocol`), matching `workers.
    protocol.WorkerAdapter`'s own pattern."""

    def analyze(self, original_prompt: str, evidence: EvidenceContext) -> PromptAnalysis: ...
