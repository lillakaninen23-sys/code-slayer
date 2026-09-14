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
    """

    task_id: str
    role: str
    original_prompt: str
    allowed_tools: tuple[str, ...] | None = None
    prior_tool_result: WorkerToolResult | None = None


@dataclass(frozen=True)
class WorkerResponse:
    """One inference response, exactly as the adapter itself classified
    it. Exactly one of `text`/`tool_call` is meaningful, selected by
    `kind` — the others are `None`. `raw` and `error` are opaque
    evidence only (never parsed by anything that reads this object)."""

    kind: WorkerResponseKind
    text: str | None = None
    tool_call: WorkerToolCall | None = None
    raw: str | None = None
    error: str | None = None


class WorkerAdapter(Protocol):
    """The one minimal method every future model/provider adapter must
    implement. A concrete adapter needs no base class — this is
    structural (`typing.Protocol`), so a provider-specific adapter stays
    entirely outside this module's import graph."""

    def infer(self, request: WorkerRequest) -> WorkerResponse: ...
