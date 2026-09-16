"""The provider-independent worker protocol boundary (Phase 7.1).

This module defines the immutable request/response shapes and the
minimal adapter interface every future model/provider integration must
satisfy — and nothing else. It contains no streaming, no scheduler, no
multi-turn orchestration, no retries, no trust levels, and no provider
configuration; those belong to later Phase 7 slices. No provider name
(Qwen, Claude, OpenAI, Ollama, ...) appears anywhere in this module —
per `adr/0006-worker-model-local-first.md` and INV-10, a provider is
something a future adapter *implements against*, never something core
code names.

## Model output is untrusted data

A model does not gain a capability by emitting text that resembles a
tool call (`docs/CODE_SLAYER_VISION.md` §40). `WorkerResponse.kind` is
the adapter's own structural claim about what it received back — never
inferred here by pattern-matching text content. Only `WorkerResponseKind.
TOOL_CALL`, carrying an actual `WorkerToolCall` instance (not a string),
can ever become executable; `TEXT` and `MALFORMED` cannot, structurally,
regardless of what either one's content happens to look like. This
module never parses free text looking for something that resembles a
tool call — see `protocol_validation.py` for the validator that builds
on this same principle.

## Original prompt stays authoritative

`WorkerRequest.original_prompt` must always carry the complete, verbatim
original prompt for the task/turn — never a derived summary or a
truncation (`docs/CODE_SLAYER_VISION.md` §32, §59). This slice does not
implement a Prompt Analyst; the field exists now so that when one is
added later, nothing about this contract has to change to keep the
original prompt authoritative alongside it.

## Supplemental resolutions: authoritative context, never a rewrite (Phase 7.7d)

`WorkerRequest.supplemental_resolutions` carries verified, durable human/
application answers to previously-blocking ambiguities *alongside*
`original_prompt` — never merged, concatenated, or substituted into it.
`WorkerSupplementalResolution.kind`/`.source` deliberately mirror
`workers.question_gate.ResolutionKind`/`workers.prompt_analysis.
EvidenceSource`'s exact string vocabulary (`"FACT"`/`"AUTHORIZATION"`/
`"SAFE_DEFAULT"`, `"ORIGINAL_PROMPT"`/`"REPOSITORY"`/`"RUNTIME"`/
`"DURABLE_TASK_EVIDENCE"`) without importing either module here — this
module stays free of any dependency on a specific analyst/gate phase,
matching its own "provider-independent... and nothing else" boundary
above. The one legitimate producer, `runner.local_worker_runner.
LocalWorkerRunner`, constructs these exclusively from resolutions that
already passed the hardened `workers.question_gate.QuestionGate` and are
durably recorded (`runner_human_resolutions`) — never from a `PromptAnalyst`'s
own advisory hints (`already_answered`, `evidence_keys`,
`resolved_by_prompt_substring`), which can never become one of these.

A `WorkerSupplementalResolution` is inert data: carrying `kind=
AUTHORIZATION` here states only that a human explicitly authorized
something — it grants no trust, bypasses no `policy.engine.PolicyEngine`
check, and acquires no lease or tool capability by itself. Every actual
mutation/tool-execution authority still comes entirely from `workers.
trust.WorkerTrustManager`, `PolicyEngine`, and `tools.executor.
ToolExecutor`, completely unaware that this field exists.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class WorkerAdapterError(RuntimeError):
    """Raised by `WorkerAdapter.infer()` when no response was received at
    all (transport failure, timeout, ...) — distinct from a returned
    `WorkerResponse(kind=MALFORMED)`, which *is* a definite response the
    adapter received but could not classify as text or a valid tool
    call. Stable, short reason only; never raw provider output."""


class WorkerResponseKind(StrEnum):
    """What the adapter itself claims its response is — never inferred
    from content by anything downstream."""

    TEXT = "TEXT"
    TOOL_CALL = "TOOL_CALL"
    MALFORMED = "MALFORMED"


class ToolRequirement(StrEnum):
    """Whether this turn's `allowed_tools` are merely available
    (`OPTIONAL`, the default) or a genuine structured tool call is
    specifically required to satisfy this turn (`REQUIRED`) — Phase
    7.4c (`docs/CODE_SLAYER_VISION.md` §58's follow-up investigation).

    `OPTIONAL` is correct for essentially every ordinary task turn: a
    task may legitimately have tools available without needing to call
    one, and `allowed_tools` being non-empty must never, by itself, be
    read as a demand to use one. `REQUIRED` exists for the narrow case
    where the caller is not asking "can you get this done, optionally
    with a tool" but specifically testing or requiring "prove a genuine
    structured tool call can be produced right now" — today, that is
    exactly `workers.conformance`'s `structured_tool_call` case, and
    nothing else in this codebase sets it.

    This is provider-neutral by construction: a concrete adapter decides
    how to honor `REQUIRED` using whatever mechanism its own provider
    exposes (`workers.openai_compatible_adapter.OpenAICompatibleAdapter`
    maps it to the standard OpenAI-compatible `tool_choice: "required"`
    field) — this type and field name never reference a provider or
    model directly, and nothing here weakens `protocol_validation.
    validate_response()`'s own judgment of whatever actually comes
    back."""

    OPTIONAL = "OPTIONAL"
    REQUIRED = "REQUIRED"


@dataclass(frozen=True)
class WorkerToolCall:
    """A structurally claimed tool call. Being an instance of this exact
    type at all is itself part of what "structured" means here — an
    adapter that only has a raw string it *thinks* looks like a tool
    call must report `WorkerResponseKind.TEXT` or `MALFORMED`, never
    construct one of these from unparsed text."""

    tool: str
    params: Mapping[str, Any]


@dataclass(frozen=True)
class WorkerToolResult:
    """The smallest possible representation of "a tool ran and here is
    what happened," for a follow-up request to carry as context (Phase
    7.3, added for the `tool_result_consumption` conformance case).

    Deliberately not a conversation/message-history framework: exactly
    one prior result, never a list, never roles, never turn ordering.
    `output_summary` is a short, caller-prepared summary string — never
    raw command output, file bytes, or anything that needs its own
    evidence/redaction handling; a caller with real tool output decides
    what's safe and useful to summarize before this field is ever built,
    the same way `tools.executor` never puts raw bytes in an audit
    payload. If a real multi-turn need ever outgrows this, that is a
    deliberately later, separate design decision — not one this phase
    makes by accident."""

    tool: str
    output_summary: str


class WorkerSupplementalKind(StrEnum):
    """What kind of trusted resolution one `WorkerSupplementalResolution`
    represents — see the module docstring's "Supplemental resolutions"
    section for why this mirrors, rather than imports,
    `workers.question_gate.ResolutionKind`.

    `FACT` — a deterministic fact that answers a question (e.g. "the
    target module is billing.py") — never itself permission to do
    anything destructive or externally consequential.
    `AUTHORIZATION` — an explicit prior human decision authorizing a
    specific destructive or externally-consequential action.
    `SAFE_DEFAULT` — a code-owned, deterministic default Code Slayer
    itself applied for a genuinely harmless choice.
    """

    FACT = "FACT"
    AUTHORIZATION = "AUTHORIZATION"
    SAFE_DEFAULT = "SAFE_DEFAULT"


class WorkerSupplementalSource(StrEnum):
    """Where one `WorkerSupplementalResolution` came from — mirrors
    `workers.prompt_analysis.EvidenceSource`'s exact vocabulary; see the
    module docstring's "Supplemental resolutions" section."""

    ORIGINAL_PROMPT = "ORIGINAL_PROMPT"
    REPOSITORY = "REPOSITORY"
    RUNTIME = "RUNTIME"
    DURABLE_TASK_EVIDENCE = "DURABLE_TASK_EVIDENCE"


@dataclass(frozen=True)
class WorkerSupplementalResolution:
    """One verified, durable answer to a previously-blocking ambiguity —
    supplemental authoritative context, never a modification to the
    original prompt (see the module docstring). Bound to exactly one
    `ambiguity_id`; a caller must never let one ambiguity's resolution
    stand in for another's.

    `content` is the exact durable answer text (already read back from
    `store.content_store.ContentStore` and verified against
    `content_hash` by the caller — this dataclass carries no verification
    logic of its own, only the already-verified result). `content_hash`
    is retained for provenance/audit even after the text has been read,
    so a caller can always cite exactly which durable evidence this
    resolution came from without re-embedding the hash into `content`
    itself.
    """

    ambiguity_id: str
    kind: WorkerSupplementalKind
    source: WorkerSupplementalSource
    content: str
    content_hash: str


@dataclass(frozen=True)
class WorkerRequest:
    """One inference request for one task/role turn.

    `original_prompt` is the complete, unmodified original prompt — see
    the module docstring. `allowed_tools`, when not `None`, is the exact
    set of capability names this turn may request; `None` means no
    capability constraint was declared by the caller (the structural
    validator then skips the authorization check entirely — it never
    invents a default allowlist). An empty tuple is a deliberate,
    different statement: no tool call is authorized at all this turn.
    `prior_tool_result`, when set, is the one immediately-preceding tool
    outcome this turn continues from — see `WorkerToolResult`.

    `supplemental_resolutions`, when non-empty, carries verified, durable
    human/application answers to ambiguities that were resolved before
    this turn began — see `WorkerSupplementalResolution` and the module
    docstring's "Supplemental resolutions" section. Ordered deterministically
    by the caller (never left to incidental storage order); an adapter
    renders each entry into its own provider input deterministically,
    always distinguishable from `original_prompt` itself.

    `tool_requirement` (`ToolRequirement`, default `OPTIONAL`) is a
    separate, deliberately narrow signal from `allowed_tools`: having
    tools available (`allowed_tools` non-empty) never implies a demand
    to use one. Only a caller that specifically needs proof a structured
    tool call can be produced sets `REQUIRED` — see `ToolRequirement`.

    `max_output_tokens` (Phase 8.2e's qualification context-adequacy
    hardening), when set, maps to the standard OpenAI-compatible
    `max_tokens` request field (`workers.openai_compatible_adapter.
    OpenAICompatibleAdapter._build_payload()`) — a genuine, provider-
    neutral, generic completion-length cap, never a model-specific hack.
    `None` (the default) omits it entirely, exactly as every ordinary
    production task turn already behaves; only `planning.qualification`
    ever sets this, to make a qualification run's own output budget a
    real, runtime-enforced limit rather than pure bookkeeping.
    """

    task_id: str
    role: str
    original_prompt: str
    allowed_tools: tuple[str, ...] | None = None
    prior_tool_result: WorkerToolResult | None = None
    tool_requirement: ToolRequirement = ToolRequirement.OPTIONAL
    supplemental_resolutions: tuple[WorkerSupplementalResolution, ...] = ()
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class WorkerUsage:
    """Token accounting the provider itself reported for one turn —
    read-only evidence only, never authoritative for anything beyond
    diagnostics/qualification measurement (`planning.qualification`).
    `WorkerResponse.usage` is `None` whenever a provider/adapter does not
    report it — never invented, never estimated here."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class WorkerResponse:
    """One inference response, exactly as the adapter itself classified
    it. Exactly one of `text`/`tool_call` is meaningful, selected by
    `kind` — the others are `None`. `raw` and `error` are opaque
    evidence only (never parsed by anything that reads this object).
    `usage`, when the provider reports it, is real token accounting for
    this exact call — see `WorkerUsage`. `finish_reason`, when the
    provider reports it (the standard OpenAI-compatible field, e.g.
    `"stop"`/`"length"`/`"tool_calls"`), is read-only evidence of *why*
    generation ended — `"length"` means the completion was cut off by a
    token cap (the caller's own `max_output_tokens` or the runtime's own
    default), which `planning.qualification` uses to distinguish a
    genuinely malformed/irrelevant response from one that was simply
    truncated before it could finish; never itself interpreted here."""

    kind: WorkerResponseKind
    text: str | None = None
    tool_call: WorkerToolCall | None = None
    raw: str | None = None
    error: str | None = None
    usage: WorkerUsage | None = None
    finish_reason: str | None = None


class WorkerAdapter(Protocol):
    """The one minimal method every future model/provider adapter must
    implement. A concrete adapter needs no base class — this is
    structural (`typing.Protocol`), so a provider-specific adapter stays
    entirely outside this module's import graph."""

    def infer(self, request: WorkerRequest) -> WorkerResponse: ...
