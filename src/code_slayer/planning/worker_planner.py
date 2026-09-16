"""`WorkerAdapterPlanner`: the one production bridge from an existing
`workers.protocol.WorkerAdapter` to the `planning.planner.Planner`
protocol (Phase 8.2).

## Reusing the existing transport, not inventing a second one

This module builds exactly one `workers.protocol.WorkerRequest`, calls
the *same* `WorkerAdapter.infer()` every real/fake worker already
implements (Phase 7.1/7.4a), and validates the response through the
*same* `workers.protocol_validation.validate_response()` every bounded
worker turn already goes through (Phase 7.1) — including its reserved
tool-call-transport-marker detection, which is exactly what stops a
model from embedding a tool call inside plain text to bypass this
protocol. A planning turn always sets `tool_requirement=REQUIRED` and
`allowed_tools=(TOOL_NAME,)`: only a genuine, structurally valid
`WorkerToolCall` naming exactly `TOOL_NAME` can ever become a
`PlannerResponse(outcome=STRUCTURED, ...)`; a `TEXT` response, a
`WorkerToolCall` naming anything else, or a validator-rejected response
is always `MALFORMED` — never parsed as prose, never retried, never
"repaired."

`WorkerToolCall.params` (the structured payload the adapter itself
claims to have received back, already gated through `validate_response`)
is then run through `planning.planner.parse_planner_output()` — the
narrower, planning-specific schema check that decides whether the
payload is well-formed *engineering-plan* structured output specifically
(as opposed to merely being *a* well-formed tool call).

## Bounded context becomes this turn's own prompt, not the user's request

`WorkerRequest.original_prompt` must be that request's own complete,
unmodified text (`workers.protocol`'s own invariant) — but the "turn"
here is a planning turn, whose complete instruction legitimately
includes the bounded Repository Intelligence context the caller already
selected, not only the human's original words. `_render_planning_prompt()`
builds that complete planning-turn text deterministically: the original
engineering request verbatim, followed by a clearly labeled, bounded,
JSON-serialized evidence section — never the entire repository, never
free-form narrative summarization of anything. This is a different
field from `planning.models.EngineeringPlanRow.request_content_hash`,
which is bound to the pure, untouched original user request only (see
`planning.service`) — the two are never conflated.
"""

from __future__ import annotations

import json

from code_slayer.planning.planner import (
    Planner,
    PlannerFailureCategory,
    PlannerOutcome,
    PlannerRequest,
    PlannerResponse,
    parse_planner_output,
    render_bounded_context,
)
from code_slayer.workers.protocol import (
    ToolRequirement,
    WorkerAdapter,
    WorkerAdapterError,
    WorkerRequest,
    WorkerToolResult,
)
from code_slayer.workers.protocol_validation import validate_response

TOOL_NAME = "emit_engineering_plan"

_PLANNING_INSTRUCTION = (
    "You are producing a structured engineering plan for the request below. "
    "Call the '" + TOOL_NAME + "' tool exactly once with your complete structured plan. "
    "Only claim a file exists if it appears in REPOSITORY_CONTEXT; propose new files "
    "with action \"create\" instead. Only claim a command via evidence_claims if it "
    "appears in REPOSITORY_CONTEXT.discovered_commands. Raise an ambiguity instead of "
    "guessing whenever a destructive action, an external service choice, or a required "
    "user preference is not already decided by the request or the evidence."
)


def _render_planning_prompt(request: PlannerRequest) -> str:
    """`render_bounded_context()` (`planning.planner`) is the exact same
    bounded-context view `planning.provenance.store_planner_input()`
    durably records — the two never drift apart (Phase 8.2b)."""
    return (
        f"{_PLANNING_INSTRUCTION}\n\n"
        f"ORIGINAL_REQUEST:\n{request.original_request}\n\n"
        f"REPOSITORY_CONTEXT:\n{json.dumps(render_bounded_context(request), sort_keys=True)}"
    )


class WorkerAdapterPlanner:
    """One planning turn per `.plan()` call — stateless beyond the
    wrapped adapter and task/role identity. `task_id` is a caller-chosen
    label for this planning turn's own protocol-layer identity (e.g. the
    plan_id); it authorizes nothing and is never a real mutating task
    (`core.states.TaskState` never enters this module)."""

    def __init__(self, adapter: WorkerAdapter, *, task_id: str, role: str = "planner") -> None:
        self._adapter = adapter
        self._task_id = task_id
        self._role = role

    def plan(self, request: PlannerRequest) -> PlannerResponse:
        # `prior_attempt_feedback` (Phase 8.2e qualification self-
        # correction extension) reuses the *existing* "one prior tool
        # result" transport unchanged -- see `PlannerRequest`'s own
        # docstring. `None` here (every ordinary production planning
        # turn) means `prior_tool_result` stays `None`, exactly as
        # before this field existed.
        prior_tool_result = (
            WorkerToolResult(tool=TOOL_NAME, output_summary=request.prior_attempt_feedback)
            if request.prior_attempt_feedback is not None else None
        )
        worker_request = WorkerRequest(
            task_id=self._task_id, role=self._role,
            original_prompt=_render_planning_prompt(request),
            allowed_tools=(TOOL_NAME,), tool_requirement=ToolRequirement.REQUIRED,
            supplemental_resolutions=request.supplemental_resolutions,
            prior_tool_result=prior_tool_result,
            max_output_tokens=request.output_token_budget,
        )
        try:
            response = self._adapter.infer(worker_request)
        except WorkerAdapterError as exc:
            return PlannerResponse(
                PlannerOutcome.MALFORMED, error=f"adapter_error:{exc}",
                failure_category=PlannerFailureCategory.TRANSPORT_ERROR,
            )

        validation = validate_response(worker_request, response)
        if not validation.executable or validation.tool_call is None:
            return PlannerResponse(
                PlannerOutcome.MALFORMED, raw=response.text or response.raw,
                error=f"invalid_transport_response:{validation.reason}",
                failure_category=PlannerFailureCategory.NON_TOOL_RESPONSE,
                usage=response.usage, finish_reason=response.finish_reason,
            )
        if validation.tool_call.tool != TOOL_NAME:
            return PlannerResponse(
                PlannerOutcome.MALFORMED, error=f"unexpected_tool_call:{validation.tool_call.tool}",
                failure_category=PlannerFailureCategory.NON_TOOL_RESPONSE, usage=response.usage,
                finish_reason=response.finish_reason,
            )
        raw_params = json.dumps(dict(validation.tool_call.params), sort_keys=True, default=str)
        structured = parse_planner_output(validation.tool_call.params)
        if structured is None:
            return PlannerResponse(
                PlannerOutcome.MALFORMED, raw=raw_params, error="malformed_structured_output",
                failure_category=PlannerFailureCategory.SCHEMA_INVALID, usage=response.usage,
                finish_reason=response.finish_reason,
            )
        return PlannerResponse(
            PlannerOutcome.STRUCTURED, output=structured, raw=raw_params, usage=response.usage,
            finish_reason=response.finish_reason,
        )


# Satisfy the `Planner` structural protocol explicitly for readability;
# not required at runtime (typing.Protocol needs no inheritance).
_: type[Planner] = WorkerAdapterPlanner
