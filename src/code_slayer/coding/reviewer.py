"""Reviewer: an independent role from Coder, never able to mutate code.

A Reviewer turn is deliberately offered NO tools at all
(`allowed_tools=()`) -- `workers.protocol_validation.validate_response()`
already makes any tool-call attempt `UNAUTHORIZED_CAPABILITY`
(structurally rejected before `policy.engine.PolicyEngine`/`tools.
executor.ToolExecutor` are ever reached) regardless of what a Reviewer
model might attempt, so "Reviewer does not mutate code" is enforced by
construction here, not by convention or prompt text. The diff a Reviewer
is shown is code-computed (`coding.mutation_guard`), never a model's own
claim about what changed.
"""

from __future__ import annotations

import json

from code_slayer.coding.pipeline_types import (
    ReviewResult,
    diff_fingerprint,
    parse_reviewer_model_result,
)
from code_slayer.workers.protocol import (
    ToolRequirement,
    WorkerAdapter,
    WorkerAdapterError,
    WorkerRequest,
)
from code_slayer.workers.protocol_validation import ValidationOutcome, validate_response


class ReviewerTurnError(RuntimeError):
    """A Reviewer turn produced no usable verdict at all (transport
    failure or malformed/non-conforming response) -- the caller must
    treat this the same as `ReviewVerdict.BLOCKED`: fail closed, never
    silently treated as an implicit PASS."""


def _render_review_prompt(*, original_prompt: str, plan_goal: str, diff_text: str) -> str:
    return "\n".join([
        "You are the Reviewer role in an autonomous, code-owned engineering "
        "pipeline. You have NO tools this turn -- you cannot read further "
        "files or run anything. Judge only the diff below.",
        "",
        "=== ORIGINAL REQUEST (verbatim) ===",
        original_prompt,
        "",
        f"=== PLAN GOAL ===\n{plan_goal}",
        "",
        "=== DIFF UNDER REVIEW (independently computed by Code Slayer, "
        "never the Coder's own claim) ===",
        diff_text,
        "",
        "Respond with plain text containing ONLY a JSON object matching "
        'this schema (no other text): {"verdict": "PASS"|"CHANGES_REQUIRED"'
        '|"BLOCKED", "summary": str, "findings": [{"description": str, '
        '"severity": str, "path": str}]}',
    ])


def run_reviewer_turn(
    adapter: WorkerAdapter, *, task_id: str, original_prompt: str, plan_goal: str,
    diff_text: str,
) -> ReviewResult:
    """Run one bounded, non-mutating Reviewer turn against `diff_text`
    (already independently computed by the caller -- see `coding.
    mutation_guard`). Raises `ReviewerTurnError` (never returns a
    fabricated PASS) if the model transport fails or the response cannot
    be parsed as a closed-schema `ReviewerModelResult`."""
    request = WorkerRequest(
        task_id=task_id, role="reviewer",
        original_prompt=_render_review_prompt(
            original_prompt=original_prompt, plan_goal=plan_goal, diff_text=diff_text,
        ),
        allowed_tools=(), tool_requirement=ToolRequirement.OPTIONAL,
    )
    try:
        response = adapter.infer(request)
    except WorkerAdapterError as exc:
        raise ReviewerTurnError(f"transport_failure:{exc}") from exc
    result = validate_response(request, response)
    if result.outcome != ValidationOutcome.VALID_TEXT:
        raise ReviewerTurnError(f"non_conforming_response:{result.outcome.value}:{result.reason}")
    try:
        payload = json.loads(result.text)
    except (TypeError, ValueError) as exc:
        raise ReviewerTurnError(f"non_json_response:{exc}") from exc
    model_result = parse_reviewer_model_result(payload)
    if model_result is None:
        raise ReviewerTurnError("malformed_reviewer_result")
    return ReviewResult(
        verdict=model_result.verdict, summary=model_result.summary,
        findings=model_result.findings, diff_fingerprint=diff_fingerprint(diff_text),
    )
