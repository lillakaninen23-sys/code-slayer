"""Structural validation of a `WorkerResponse` (Phase 7.1).

This module answers exactly one question, conservatively: *is this
response, as the adapter itself classified it, structurally well-formed
enough to even be considered further* — never whether the repository
state, task policy, or lease permits whatever it asks for. That is
`policy.engine.PolicyEngine`'s job, applied only after a tool call has
already passed through here as `VALID_TOOL_CALL`; this module never
touches task state, scope, ownership, or the filesystem, and never
duplicates that decision (`docs/CODE_SLAYER_VISION.md` §40).

## Two distinct ways a tool call never becomes executable

- **`MALFORMED`** — the response is not, structurally, a valid instance
  of what it claims to be: `TOOL_CALL` without a real `WorkerToolCall`,
  an empty/non-string tool name, non-mapping params, or the adapter
  reporting `MALFORMED` itself. This is a *protocol* failure — the
  response cannot be trusted to mean anything in particular.
- **`UNAUTHORIZED_CAPABILITY`** — the response *is* a well-formed,
  structurally valid tool call, naming a real (if unknown-to-this-turn)
  tool, but not one this turn's `WorkerRequest.allowed_tools` offered.
  This is deliberately kept distinct from `MALFORMED`: the protocol was
  followed correctly; the request just was not for this capability.
  Preserving this distinction keeps `docs/CODE_SLAYER_VISION.md` §42's
  per-capability trust scoping meaningful later — a worker that
  correctly used the protocol but reached for the wrong tool is a
  different signal than one that cannot speak the protocol at all.

Both outcomes are equally non-executable right now (`ValidationResult.
executable` is `False` for either) — the distinction is for evidence and
future trust-scoping, never for deciding *this* call gets to run.

## No fallback parser

`TEXT` and `MALFORMED` responses are never inspected for content that
*looks like* a tool call and never "recovered" into one. A response
either arrived through the adapter's own structured channel as a real
`WorkerToolCall`, or it did not — there is no third path here that
extracts one from a string after the fact.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from code_slayer.workers.protocol import (
    WorkerRequest,
    WorkerResponse,
    WorkerResponseKind,
    WorkerToolCall,
)


class ValidationOutcome(StrEnum):
    VALID_TEXT = "VALID_TEXT"
    VALID_TOOL_CALL = "VALID_TOOL_CALL"
    MALFORMED = "MALFORMED"
    UNAUTHORIZED_CAPABILITY = "UNAUTHORIZED_CAPABILITY"


@dataclass(frozen=True)
class ValidationResult:
    outcome: ValidationOutcome
    reason: str
    text: str | None = None
    tool_call: WorkerToolCall | None = None

    @property
    def executable(self) -> bool:
        """`True` only for a validated, authorized tool call — the one
        condition under which a caller may even consider proceeding
        toward `PolicyEngine`/`ToolExecutor`. Every other outcome,
        including a well-formed but unauthorized tool call, is never
        executable from this validator's judgment alone."""
        return self.outcome == ValidationOutcome.VALID_TOOL_CALL


def _reject(outcome: ValidationOutcome, reason: str) -> ValidationResult:
    return ValidationResult(outcome, reason)


def validate_response(request: WorkerRequest, response: WorkerResponse) -> ValidationResult:
    """Validate `response` against `request`. Malformed, unrecognized, or
    ambiguous input denies — matching every other decision function in
    this codebase (`policy.engine.PolicyEngine.evaluate`,
    `lease.manager.LeaseManager`): never guess, never repair, never
    execute anything from here."""
    if not isinstance(request, WorkerRequest) or not isinstance(response, WorkerResponse):
        return _reject(ValidationOutcome.MALFORMED, "malformed_validation_input")
    if not isinstance(response.kind, WorkerResponseKind):
        return _reject(ValidationOutcome.MALFORMED, "unrecognized_response_kind")

    if response.kind == WorkerResponseKind.MALFORMED:
        # The adapter itself already could not classify this response —
        # trusted at face value, never second-guessed into TEXT/TOOL_CALL.
        return _reject(ValidationOutcome.MALFORMED, response.error or "adapter_reported_malformed")

    if response.kind == WorkerResponseKind.TEXT:
        if not isinstance(response.text, str):
            return _reject(ValidationOutcome.MALFORMED, "text_response_missing_text")
        return ValidationResult(ValidationOutcome.VALID_TEXT, "text_response", text=response.text)

    if response.kind == WorkerResponseKind.TOOL_CALL:
        call = response.tool_call
        if not isinstance(call, WorkerToolCall):
            return _reject(ValidationOutcome.MALFORMED, "tool_call_not_a_structured_call")
        if not isinstance(call.tool, str) or not call.tool:
            return _reject(ValidationOutcome.MALFORMED, "tool_call_name_missing_or_empty")
        if not isinstance(call.params, Mapping):
            return _reject(ValidationOutcome.MALFORMED, "tool_call_params_not_a_mapping")
        if request.allowed_tools is not None and call.tool not in request.allowed_tools:
            return _reject(ValidationOutcome.UNAUTHORIZED_CAPABILITY, "tool_not_in_allowed_schema")
        return ValidationResult(
            ValidationOutcome.VALID_TOOL_CALL, "tool_call_validated", tool_call=call,
        )

    # Unreachable given every WorkerResponseKind member is handled above
    # — kept explicit rather than falling through silently, matching
    # this codebase's fail-closed posture toward an unhandled case.
    return _reject(ValidationOutcome.MALFORMED, "unhandled_response_kind")
