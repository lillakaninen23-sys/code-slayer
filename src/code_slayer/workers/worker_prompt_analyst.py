"""`WorkerAdapterPromptAnalyst`: the one production bridge from an
existing `workers.protocol.WorkerAdapter` to the `workers.prompt_
analysis.PromptAnalyst` protocol — the real-model counterpart to
`workers.fake_prompt_analyst.FakePromptAnalyst`, mirroring `planning.
worker_planner.WorkerAdapterPlanner`'s exact pattern applied to prompt
analysis instead of planning.

## Reusing the existing transport, not inventing a second one

This module builds exactly one `workers.protocol.WorkerRequest`, calls
the *same* `WorkerAdapter.infer()` every real/fake worker already
implements, and validates the response through the *same* `workers.
protocol_validation.validate_response()` every bounded worker turn
already goes through — including its reserved tool-call-transport-
marker detection. A turn always sets `tool_requirement=REQUIRED` and
`allowed_tools=(TOOL_NAME,)`: only a genuine, structurally valid
`WorkerToolCall` naming exactly `TOOL_NAME` can ever become a real
`PromptAnalysis`; a `TEXT` response, a `WorkerToolCall` naming anything
else, or a validator-rejected response always raises
`PromptAnalystError` — never parsed as prose, never retried, never
"repaired."

## The original prompt stays authoritative

`workers.prompt_analysis.PromptAnalysis.original_prompt_hash` is
computed, by that module, from `PromptAnalysis.original_prompt` alone —
so this class always constructs the final `PromptAnalysis` with the
exact, unmodified `original_prompt` argument it received, never the
turn's own rendered instruction text (`_render_analysis_prompt()`
below, which *is* what actually goes out over the wire as `WorkerRequest.
original_prompt`, exactly as `planning.worker_planner._render_planning_
prompt()` already does for planning turns). A buggy or adversarial
model response can add a wrong `ambiguity`/`goal`/etc, but it can never
make the returned `PromptAnalysis` claim a hash the true prompt text
does not have.

## Fail closed, not fail open

`PromptAnalyst.analyze()`'s protocol signature returns a `PromptAnalysis`
unconditionally — there is no "malformed" variant to return instead (
unlike `planning.planner.PlannerResponse`). A transport failure or a
non-conforming response therefore raises `PromptAnalystError` rather
than silently returning an empty, ambiguity-free `PromptAnalysis`: an
empty analysis would suppress every question `workers.question_gate.
QuestionGate` might otherwise have asked, exactly the fail-open failure
mode this codebase's "fail closed" posture forbids. `workers.
fake_prompt_analyst.FakePromptAnalystError` already established the
same "raise, never fabricate a benign result" contract for the
deterministic test double; this is its real-adapter counterpart.
"""

from __future__ import annotations

import json

from code_slayer.workers.prompt_analysis import (
    EvidenceContext,
    PromptAnalysis,
    PromptAnalyst,
    parse_prompt_analysis_output,
)
from code_slayer.workers.protocol import (
    ToolRequirement,
    WorkerAdapter,
    WorkerAdapterError,
    WorkerRequest,
)
from code_slayer.workers.protocol_validation import validate_response

TOOL_NAME = "emit_prompt_analysis"

_ANALYSIS_INSTRUCTION = (
    "You are producing a structured, SUPPLEMENTAL analysis of the user's original prompt "
    "below. Call the '" + TOOL_NAME + "' tool exactly once with your complete structured "
    "analysis. Never restate, rewrite, shorten, or replace the original prompt — only "
    "identify its goals, explicit requirements, constraints, already-answered points, risk "
    "points, and genuine ambiguities. Raise an ambiguity instead of guessing whenever a "
    "destructive action, an external service choice, or a required user preference is not "
    "already decided by the prompt itself."
)


class PromptAnalystError(RuntimeError):
    """A real Prompt Analyst turn did not produce a valid, structured
    `emit_prompt_analysis` tool call — transport failure, a non-tool
    response, an unauthorized/wrong tool call, or a tool call whose own
    parameters failed `parse_prompt_analysis_output()`'s strict schema
    check. Never raised for the ordinary case (a valid structured
    response, however few or many ambiguities it raises)."""


def _render_analysis_prompt(original_prompt: str) -> str:
    return f"{_ANALYSIS_INSTRUCTION}\n\nORIGINAL_PROMPT:\n{original_prompt}"


class WorkerAdapterPromptAnalyst:
    """One Prompt Analyst turn per `.analyze()` call — stateless beyond
    the wrapped adapter and task/role identity. `task_id`/`role` are
    caller-chosen protocol-layer labels for this turn's own identity —
    they authorize nothing and are never a real mutating task."""

    def __init__(
        self, adapter: WorkerAdapter, *, task_id: str, role: str = "prompt_analyst",
    ) -> None:
        self._adapter = adapter
        self._task_id = task_id
        self._role = role

    def analyze(self, original_prompt: str, evidence: EvidenceContext) -> PromptAnalysis:
        # `evidence` is not yet rendered into the turn's own prompt text —
        # no production caller populates it today (`runner.
        # local_worker_runner.LocalWorkerRunner.start()`/`resume()` always
        # pass `{}`); accepted here only to satisfy the `PromptAnalyst`
        # protocol shape, exactly like `FakePromptAnalyst.analyze()`.
        request = WorkerRequest(
            task_id=self._task_id, role=self._role,
            original_prompt=_render_analysis_prompt(original_prompt),
            allowed_tools=(TOOL_NAME,), tool_requirement=ToolRequirement.REQUIRED,
        )
        try:
            response = self._adapter.infer(request)
        except WorkerAdapterError as exc:
            raise PromptAnalystError(f"adapter_error:{exc}") from exc

        validation = validate_response(request, response)
        if not validation.executable or validation.tool_call is None:
            raise PromptAnalystError(f"invalid_transport_response:{validation.reason}")
        if validation.tool_call.tool != TOOL_NAME:
            raise PromptAnalystError(f"unexpected_tool_call:{validation.tool_call.tool}")

        fields = parse_prompt_analysis_output(validation.tool_call.params)
        if fields is None:
            raw = json.dumps(dict(validation.tool_call.params), sort_keys=True, default=str)
            raise PromptAnalystError(f"malformed_structured_output:{raw}")

        return PromptAnalysis(
            original_prompt=original_prompt,
            goals=fields.goals,
            explicit_requirements=fields.explicit_requirements,
            constraints=fields.constraints,
            already_answered=fields.already_answered,
            ambiguities=fields.ambiguities,
            risk_points=fields.risk_points,
        )


# Satisfy the `PromptAnalyst` structural protocol explicitly for
# readability; not required at runtime (typing.Protocol needs no
# inheritance).
_: type[PromptAnalyst] = WorkerAdapterPromptAnalyst
