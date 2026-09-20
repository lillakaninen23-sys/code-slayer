"""Security: an independent role from Coder/Reviewer, never able to
mutate code, whose verdict is the sole gate on whether a coding job may
ever reach `READY_FOR_HUMAN_MERGE`.

Same "no tools at all" enforcement-by-construction as `coding.reviewer`
-- see that module's docstring. A Security turn is shown the same
independently-computed diff, plus the command-execution history
(`coding.tool_loop.ToolCallAttempt`s) and verification evidence
(`finalization.types.VerificationCommandResult`), never a model's own
narrative about what it did.

This is explicitly SEPARATE from, and never a substitute for, the
mandatory `workers.security_baseline` Baseline Security Certification a
production-capable MODEL itself must hold -- see `AGENTS.md` and this
task's own spec ("preserve mandatory Baseline Security certification as
a SEPARATE, non-weakened invariant"). This module reviews one job's code
CHANGE; it never issues, weakens, or substitutes for a worker's own
baseline certificate.
"""

from __future__ import annotations

import json

from code_slayer.coding.pipeline_types import (
    SecurityResult,
    diff_fingerprint,
    parse_security_model_result,
)
from code_slayer.workers.protocol import (
    ToolRequirement,
    WorkerAdapter,
    WorkerAdapterError,
    WorkerRequest,
)
from code_slayer.workers.protocol_validation import ValidationOutcome, validate_response


class SecurityTurnError(RuntimeError):
    """A Security turn produced no usable verdict at all -- the caller
    must treat this the same as `SecurityVerdict.HUMAN_REQUIRED`: fail
    closed, never silently treated as an implicit PASS."""


def _render_security_prompt(
    *, original_prompt: str, diff_text: str, command_summary: str, verification_summary: str,
) -> str:
    return "\n".join([
        "You are the Security role in an autonomous, code-owned "
        "engineering pipeline. You have NO tools this turn. Review the "
        "diff and evidence below for secrets/credentials, permission/"
        "filesystem/subprocess/network misuse, dependency risk, "
        "deserialization, injection, path traversal, auth/privilege-"
        "escalation issues, unsafe defaults, logging leaks, fail-open "
        "behavior, test/validation bypasses, and scope regressions.",
        "",
        "=== ORIGINAL REQUEST (verbatim) ===",
        original_prompt,
        "",
        "=== DIFF UNDER REVIEW (independently computed by Code Slayer, "
        "never the Coder's own claim) ===",
        diff_text,
        "",
        f"=== COMMAND/TOOL EXECUTION HISTORY ===\n{command_summary}",
        "",
        f"=== VERIFICATION EVIDENCE ===\n{verification_summary}",
        "",
        "Respond with plain text containing ONLY a JSON object matching "
        'this schema (no other text): {"verdict": "PASS"|"FAIL"|'
        '"HUMAN_REQUIRED", "summary": str, "findings": [{"description": '
        'str, "category": str, "blocking": bool}]}',
    ])


def run_security_turn(
    adapter: WorkerAdapter, *, task_id: str, original_prompt: str, diff_text: str,
    command_summary: str, verification_summary: str,
) -> SecurityResult:
    """Run one bounded, non-mutating Security turn. Raises
    `SecurityTurnError` (never returns a fabricated PASS) if the model
    transport fails or the response cannot be parsed as a closed-schema
    `SecurityModelResult`."""
    request = WorkerRequest(
        task_id=task_id, role="security",
        original_prompt=_render_security_prompt(
            original_prompt=original_prompt, diff_text=diff_text,
            command_summary=command_summary, verification_summary=verification_summary,
        ),
        allowed_tools=(), tool_requirement=ToolRequirement.OPTIONAL,
    )
    try:
        response = adapter.infer(request)
    except WorkerAdapterError as exc:
        raise SecurityTurnError(f"transport_failure:{exc}") from exc
    result = validate_response(request, response)
    if result.outcome != ValidationOutcome.VALID_TEXT:
        raise SecurityTurnError(f"non_conforming_response:{result.outcome.value}:{result.reason}")
    try:
        payload = json.loads(result.text)
    except (TypeError, ValueError) as exc:
        raise SecurityTurnError(f"non_json_response:{exc}") from exc
    model_result = parse_security_model_result(payload)
    if model_result is None:
        raise SecurityTurnError("malformed_security_result")
    return SecurityResult(
        verdict=model_result.verdict, summary=model_result.summary,
        findings=model_result.findings, diff_fingerprint=diff_fingerprint(diff_text),
    )
