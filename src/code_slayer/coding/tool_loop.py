"""The bounded, multi-tool-call Coder execution loop -- the "mutation
tool wiring" `coding.contracts`'s own module docstring explicitly says
does not exist yet.

## Why this is new code, not a reuse of `workers.execution.execute_guarded_turn`

`workers.execution.execute_guarded_turn()` is real, hardened, and already
wired to `ToolExecutor`/`PolicyEngine` -- but by its own module docstring
it offers exactly one capability (`read_file`), executes AT MOST ONE tool
call, and allows at most one continuation inference. It is a bounded
read-only conformance harness, not a mutation engine; no code path in
this repository has ever executed `write_file`/`create_file`/
`apply_patch` through it or any other existing module (confirmed by
`workers.trust`: no real worker currently holds mutating trust for any
capability). This module is the genuinely new "bounded model-tool-loop"
this task's own spec asks for -- but it reuses every real authority
boundary `execute_guarded_turn` itself reuses, unmodified: `workers.
protocol.WorkerRequest/WorkerResponse`, `workers.protocol_validation.
validate_response()` (the sole structural classifier of TEXT/TOOL_CALL/
MALFORMED/UNAUTHORIZED_CAPABILITY), and `tools.executor.ToolExecutor`
(the sole path any mutation can ever reach, itself gated by `policy.
engine.PolicyEngine` on every single call). This module does not
reimplement, weaken, or bypass any of them -- it only allows more than
one such call in a bounded loop, and offers the mutation capability set
`execute_guarded_turn` was never extended to offer.

`run_command` is deliberately never offered here: `tools.command_tools.
validate_command()` allow-lists exactly one profile (`git_rev_parse`,
a single-argv identity check `ToolExecutor` itself uses internally) --
it is not a general command-execution capability, and running actual
verification commands (pytest/ruff/...) is `finalization.service.
Finalizer`'s own, separate, already-hardened job (materialized isolated
tree, closed `finalization.verification._ARGV_BY_COMMAND` allowlist).
This loop's only job is to produce mutations and durable evidence of
them; verification happens afterward, once, by the existing Finalizer.

## No conversation/message-history framework invented

`WorkerRequest` (`workers.protocol`) deliberately carries at most ONE
`prior_tool_result` -- "never a list, never roles, never turn ordering"
per its own docstring. Rather than inventing a new multi-turn protocol
type, this loop keeps the TRUE, verbatim original prompt (`coding.
contracts.CoderTaskIdentity.original_prompt`) as an unchanged prefix on
every request (the same "original prompt stays authoritative" discipline
`workers.protocol`'s own module docstring requires) and appends a
bounded, code-constructed transcript of this turn's own prior tool calls
after it -- additional context, never a rewrite or a summary standing in
for the original request, exactly the same posture `WorkerSupplemental
Resolution` already uses for durable human answers alongside it."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from code_slayer.coding.contracts import (
    CoderExecutionRecord,
    CoderInput,
    build_authoritative_facts,
    coder_input_to_dict,
    coder_mutation_record_from_tool_operation,
    parse_coder_model_result,
)
from code_slayer.coding.pipeline_types import CoderFailureCategory
from code_slayer.lease.manager import LeaseHandle
from code_slayer.policy.engine import Decision
from code_slayer.store.tool_operations_repo import ToolOperationsRepo
from code_slayer.tools.executor import ToolExecutor
from code_slayer.tools.models import PatchHunk, ToolRequest
from code_slayer.workers.protocol import (
    ToolRequirement,
    WorkerAdapter,
    WorkerAdapterError,
    WorkerRequest,
    WorkerToolResult,
)
from code_slayer.workers.protocol_validation import ValidationOutcome, validate_response

# The complete, fixed capability set a Coder turn may ever be offered --
# code-owned, never request data (mirrors `workers.execution._ALLOWED_
# TOOLS`'s own closed-set posture). `run_command`/`checkpoint_create` are
# deliberately excluded -- see the module docstring.
CODER_TOOLS: tuple[str, ...] = ("read_file", "create_file", "write_file", "apply_patch")

_MAX_SUMMARY_CHARS = 2000
_MAX_TRANSCRIPT_CHARS = 20000


class ToolLoopContractError(RuntimeError):
    """`run_coder_turn()`'s caller supplied something malformed enough
    that no turn should even be attempted."""


@dataclass(frozen=True)
class ToolLoopBounds:
    """Every bound this loop enforces -- code-owned and explicit, never
    inferred. Mirrors `workers.execution`'s "at most one tool call"
    bound, generalized to a caller-chosen, still-finite ceiling."""

    max_iterations: int = 8
    max_wall_seconds: float = 180.0
    max_consecutive_malformed: int = 2


@dataclass(frozen=True)
class ToolCallAttempt:
    """One durable record of a single loop iteration's outcome -- never
    discarded even when the overall turn ultimately fails; this is
    exactly the "record tool calls/failures" evidence the spec asks for."""

    iteration: int
    outcome: str
    tool: str | None = None
    operation_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class ToolLoopOutcome:
    """The complete, bounded result of one Coder tool-loop turn. Never
    raises for an ordinary bound exhaustion/denial/malformed-response --
    `ok=False` with a stable `failure_category` covers every one of
    those; only a genuinely unexpected programming error propagates."""

    ok: bool
    reason: str
    execution_record: CoderExecutionRecord
    attempts: tuple[ToolCallAttempt, ...] = ()
    failure_category: CoderFailureCategory | None = None
    iterations_used: int = 0


def _render_base_prompt(coder_input: CoderInput) -> str:
    """A deterministic, bounded prompt built entirely from already-typed,
    code-owned evidence (`CoderInput`) -- never from raw, unvalidated
    text a caller supplied out of band. The exact verbatim original
    request is included unmodified (`coding.contracts.CoderTaskIdentity.
    original_prompt`'s own documented contract)."""
    plan = coder_input.validated_plan.content
    lines = [
        f"You are the {coder_input.runtime_profile.role} role in an autonomous, "
        "code-owned engineering "
        "pipeline. You may only act through the tool calls offered this "
        "turn -- nothing else you say has any authority.",
        "",
        "=== ORIGINAL REQUEST (verbatim) ===",
        coder_input.task.original_prompt,
        "",
        "=== VALIDATED PLAN ===",
        f"goal: {plan.goal}",
        *(f"planned_change: {c.description}" for c in plan.planned_changes),
        "",
        "=== AUTHORIZED PATH SCOPE ===",
        f"allowed_scope: {list(coder_input.paths.allowed_scope)}",
        f"protected_paths: {list(coder_input.paths.protected_paths)}",
        "",
        f"=== TOOLS AVAILABLE THIS TURN: {list(CODER_TOOLS)} ===",
        "When you are completely finished (or cannot proceed further), "
        "respond with plain text containing ONLY a JSON object matching "
        "this schema (no other text): "
        '{"completed_plan_steps": [{"plan_step_description": str, '
        '"status": "completed"|"partial"|"blocked"|"skipped", "note": str}], '
        '"unresolved_issues": [{"description": str, "severity": str}], '
        '"command_suggestions": [{"command": str, "purpose": str, "rationale": str}], '
        '"repair_notes": [{"note": str}]}',
    ]
    if coder_input.prior_repair is not None:
        pr = coder_input.prior_repair
        lines += [
            "",
            "=== PRIOR REPAIR EVIDENCE (this is attempt "
            f"{pr.attempt_number}; reason: {pr.reason_code}) ===",
            pr.detail,
            *(
                f"failed verification: {v.command} -> {v.status} ({v.reason})"
                for v in pr.failed_verification
            ),
        ]
    return "\n".join(lines)


def _summarize_tool_result(tool: str, result) -> str:
    text = (
        f"tool={tool} decision={result.decision} status={result.status} reason={result.reason}"
    )
    return text[:_MAX_SUMMARY_CHARS]


def _build_tool_request(tool: str, params: object) -> ToolRequest:
    """Strictly, structurally translate one model-claimed tool call into
    a `tools.models.ToolRequest` -- never trusted beyond structural
    shape: every actual authorization decision still happens entirely
    inside `tools.executor.ToolExecutor`/`policy.engine.PolicyEngine`
    below, unmodified. Raises `ToolLoopContractError` (never a bare
    KeyError/TypeError) on any malformed shape, so the caller can record
    a stable `VALIDATION_FAILURE` without ever attempting execution."""
    if not isinstance(params, dict):
        raise ToolLoopContractError("tool_call_params_not_a_mapping")
    if tool not in CODER_TOOLS:
        raise ToolLoopContractError("tool_not_offered")
    path = params.get("path")
    if not isinstance(path, str) or not path:
        raise ToolLoopContractError("missing_or_invalid_path")
    if tool == "read_file":
        extra = set(params) - {"path"}
        if extra:
            raise ToolLoopContractError("unexpected_params")
        return ToolRequest(tool="read_file", path=path)
    if tool == "create_file":
        extra = set(params) - {"path", "content"}
        if extra:
            raise ToolLoopContractError("unexpected_params")
        content = params.get("content", "")
        if not isinstance(content, str):
            raise ToolLoopContractError("content_must_be_text")
        return ToolRequest(tool="create_file", path=path, content=content.encode("utf-8"))
    if tool == "write_file":
        extra = set(params) - {"path", "content", "expected_hash"}
        if extra:
            raise ToolLoopContractError("unexpected_params")
        content = params.get("content")
        expected_hash = params.get("expected_hash")
        if not isinstance(content, str):
            raise ToolLoopContractError("content_must_be_text")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ToolLoopContractError("missing_or_invalid_expected_hash")
        return ToolRequest(
            tool="write_file", path=path, content=content.encode("utf-8"),
            expected_hash=expected_hash,
        )
    if tool == "apply_patch":
        extra = set(params) - {"path", "hunks", "expected_hash"}
        if extra:
            raise ToolLoopContractError("unexpected_params")
        expected_hash = params.get("expected_hash")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ToolLoopContractError("missing_or_invalid_expected_hash")
        raw_hunks = params.get("hunks")
        if not isinstance(raw_hunks, list) or not raw_hunks:
            raise ToolLoopContractError("missing_or_invalid_hunks")
        hunks = []
        for item in raw_hunks:
            if not isinstance(item, dict) or set(item) != {"offset", "before", "after"}:
                raise ToolLoopContractError("malformed_hunk")
            offset, before, after = item["offset"], item["before"], item["after"]
            if type(offset) is not int or offset < 0:
                raise ToolLoopContractError("malformed_hunk_offset")
            if not isinstance(before, str) or not isinstance(after, str):
                raise ToolLoopContractError("malformed_hunk_content")
            hunks.append(PatchHunk(offset, before.encode("utf-8"), after.encode("utf-8")))
        return ToolRequest(
            tool="apply_patch", path=path, expected_hash=expected_hash, hunks=tuple(hunks),
        )
    raise ToolLoopContractError("tool_not_offered")


def run_coder_turn(
    conn, adapter: WorkerAdapter, *, task_id: str,
    coder_input: CoderInput, lease: LeaseHandle, blobs_dir: Path | str,
    bounds: ToolLoopBounds | None = None,
) -> ToolLoopOutcome:
    """Run one bounded Coder turn: up to `bounds.max_iterations` rounds
    of (inference -> structural validation -> at most one real,
    policy-checked `ToolExecutor.execute()` call -> next inference),
    terminating on a final, closed-schema `CoderModelResult` text
    response, or on any bound (iteration count, wall-clock, repeated
    malformed behavior) being exhausted. Model claims are never trusted
    as evidence of a mutation -- `CoderAuthoritativeFacts` here is built
    exclusively from real `store.models.ToolOperation` rows this call's
    own successful `ToolExecutor.execute()` calls produced. Emits no
    audit events of its own -- every real tool call is already fully
    audited by `ToolExecutor` itself (`TOOL_REQUESTED`/`POLICY_EVALUATED`
    /`OPERATION_STARTED`/`OPERATION_FINISHED`); the caller
    (`coding.pipeline.run_coding_job()`) is responsible for persisting
    this call's own returned `attempts`/`reason`/`failure_category` as
    job-level evidence."""
    bounds = bounds if bounds is not None else ToolLoopBounds()
    if not isinstance(coder_input, CoderInput):
        raise ToolLoopContractError("coder_input must be a real CoderInput instance")
    coder_input_to_dict(coder_input)  # fail closed on any internal malformation, before any call
    executor = ToolExecutor(conn, blobs_dir=blobs_dir, lease=lease)
    operations = ToolOperationsRepo(conn)

    base_prompt = _render_base_prompt(coder_input)
    transcript: list[str] = []
    attempts: list[ToolCallAttempt] = []
    mutations: list = []
    prior_result: WorkerToolResult | None = None
    consecutive_malformed = 0
    start = time.monotonic()

    for iteration in range(1, bounds.max_iterations + 1):
        if time.monotonic() - start > bounds.max_wall_seconds:
            return ToolLoopOutcome(
                False, "wall_clock_budget_exhausted",
                CoderExecutionRecord(authoritative_facts=build_authoritative_facts(
                    mutations=tuple(mutations),
                )),
                tuple(attempts), CoderFailureCategory.WALL_CLOCK_BUDGET_EXHAUSTED, iteration - 1,
            )
        prompt = base_prompt
        if transcript:
            joined = "\n".join(transcript)[-_MAX_TRANSCRIPT_CHARS:]
            prompt = f"{base_prompt}\n\n=== THIS TURN'S TOOL-CALL TRANSCRIPT SO FAR ===\n{joined}"
        request = WorkerRequest(
            task_id=task_id, role=coder_input.runtime_profile.role,
            original_prompt=prompt, allowed_tools=CODER_TOOLS,
            prior_tool_result=prior_result, tool_requirement=ToolRequirement.OPTIONAL,
            max_output_tokens=coder_input.runtime_profile.output_token_budget,
        )
        try:
            response = adapter.infer(request)
        except WorkerAdapterError as exc:
            attempts.append(ToolCallAttempt(iteration, "transport_error", reason=str(exc)))
            return ToolLoopOutcome(
                False, f"transport_timeout:{exc}",
                CoderExecutionRecord(authoritative_facts=build_authoritative_facts(
                    mutations=tuple(mutations),
                )),
                tuple(attempts), CoderFailureCategory.TRANSPORT_TIMEOUT, iteration,
            )

        result = validate_response(request, response)

        if result.outcome == ValidationOutcome.VALID_TEXT:
            try:
                payload = json.loads(result.text)
            except (TypeError, ValueError):
                payload = None
            model_result = parse_coder_model_result(payload) if payload is not None else None
            if model_result is not None:
                attempts.append(ToolCallAttempt(iteration, "final_result"))
                record = CoderExecutionRecord(
                    model_result=model_result,
                    authoritative_facts=build_authoritative_facts(mutations=tuple(mutations)),
                )
                return ToolLoopOutcome(
                    True, "final_result_produced", record, tuple(attempts), None, iteration,
                )
            consecutive_malformed += 1
            attempts.append(ToolCallAttempt(iteration, "malformed_final_result"))
        elif result.outcome == ValidationOutcome.MALFORMED:
            consecutive_malformed += 1
            attempts.append(ToolCallAttempt(iteration, "malformed_response", reason=result.reason))
        elif result.outcome == ValidationOutcome.UNAUTHORIZED_CAPABILITY:
            consecutive_malformed += 1
            attempts.append(
                ToolCallAttempt(iteration, "unauthorized_capability", reason=result.reason),
            )
        else:  # VALID_TOOL_CALL
            call = result.tool_call
            try:
                tool_request = _build_tool_request(call.tool, call.params)
            except ToolLoopContractError as exc:
                consecutive_malformed += 1
                attempts.append(ToolCallAttempt(
                    iteration, "malformed_tool_params", tool=call.tool, reason=str(exc),
                ))
                prior_result = WorkerToolResult(
                    tool=call.tool, output_summary=f"rejected: {exc}"[:_MAX_SUMMARY_CHARS],
                )
                transcript.append(f"[{iteration}] {call.tool} -> rejected ({exc})")
                continue
            tool_result = executor.execute(task_id, tool_request)
            if tool_result.decision == Decision.ALLOW and tool_result.status == "SUCCEEDED":
                consecutive_malformed = 0
                operation = operations.get(tool_result.operation_id)
                mutations.append(coder_mutation_record_from_tool_operation(operation))
                attempts.append(ToolCallAttempt(
                    iteration, "tool_succeeded", tool=call.tool,
                    operation_id=tool_result.operation_id,
                ))
                summary = _summarize_tool_result(call.tool, tool_result)
                prior_result = WorkerToolResult(tool=call.tool, output_summary=summary)
                transcript.append(f"[{iteration}] {call.tool}({tool_request.path}) -> {summary}")
                continue
            if tool_result.decision == Decision.REQUIRE_APPROVAL:
                attempts.append(ToolCallAttempt(
                    iteration, "requires_approval", tool=call.tool, reason=tool_result.reason,
                ))
                record = CoderExecutionRecord(
                    authoritative_facts=build_authoritative_facts(mutations=tuple(mutations)),
                )
                return ToolLoopOutcome(
                    False, f"policy_requires_approval:{tool_result.reason}", record,
                    tuple(attempts), CoderFailureCategory.POLICY_REFUSAL, iteration,
                )
            attempts.append(ToolCallAttempt(
                iteration, "tool_denied_or_failed", tool=call.tool, reason=tool_result.reason,
            ))
            summary = _summarize_tool_result(call.tool, tool_result)
            prior_result = WorkerToolResult(tool=call.tool, output_summary=summary)
            transcript.append(f"[{iteration}] {call.tool} -> {summary}")

        if consecutive_malformed >= bounds.max_consecutive_malformed:
            record = CoderExecutionRecord(
                authoritative_facts=build_authoritative_facts(mutations=tuple(mutations)),
            )
            return ToolLoopOutcome(
                False, "repeated_malformed_behavior", record, tuple(attempts),
                CoderFailureCategory.REPEATED_MALFORMED_BEHAVIOR, iteration,
            )

    record = CoderExecutionRecord(
        authoritative_facts=build_authoritative_facts(mutations=tuple(mutations)),
    )
    return ToolLoopOutcome(
        False, "iteration_budget_exhausted", record, tuple(attempts),
        CoderFailureCategory.ITERATION_BUDGET_EXHAUSTED, bounds.max_iterations,
    )
