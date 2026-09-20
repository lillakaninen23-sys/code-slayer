"""Autonomous Engineering Loop V1: job lifecycle, Reviewer/Security
contracts, failure taxonomy, and the Planner->Coder handoff gate.

Builds on `coding.contracts` (Coder's own request/response types) without
duplicating its "three distinct output concepts" discipline: `coding.
contracts` already draws that boundary for Coder; this module draws the
analogous one for Reviewer and Security, and adds the code-owned job
state machine and failure vocabulary that ties Coder/Reviewer/Repairer/
Security together into one pipeline.

## Reviewer/Security verdicts are inherently model judgment, not "facts"

Unlike `coding.contracts.CoderAuthoritativeFacts` (durable evidence a
model's own claim can never manufacture -- a mutation is either a real,
journaled `store.models.ToolOperation` row or it does not exist), a
Reviewer/Security verdict has no independent "ground truth" a model's
own claim is checked against -- judging whether a diff is acceptable IS
the thing being asked for. What IS independently enforced, never taken
on a model's word, is everything *around* the verdict: `ReviewerModelResult
`/`SecurityModelResult` are parsed through the same closed-field,
fail-closed discipline `coding.contracts.parse_coder_model_result()`
already established (an unrecognized field rejects the whole payload,
never a partial reconstruction); the diff a Reviewer/Security turn is
shown is independently computed from real git state (`coding.
mutation_guard`), never a model's own description of "what I changed";
staleness (a later mutation invalidating an earlier verdict) is enforced
by a content fingerprint the orchestrator computes and compares itself,
never trusted from the model; and whether a verdict actually blocks
progress is a pure, code-owned decision (`review_blocks_progress()`/
`security_blocks_progress()` below), never something the model's own
text can assert its way around.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum

FORMAT_VERSION = 1


class PipelineContractError(ValueError):
    """A pipeline-level payload (Reviewer/Security model output, a
    Planner->Coder handoff) was malformed or insufficient. Stable, never
    includes raw untrusted content -- mirrors `coding.contracts.
    CoderContractError`."""


# --------------------------------------------------------------------------
# Code-owned coding-job lifecycle (never model-mutable to a success state)
# --------------------------------------------------------------------------


class CodingJobState(StrEnum):
    """The Autonomous Engineering Loop's own job lifecycle -- layered
    above, and distinct from, `core.states.TaskState` (which tracks the
    underlying isolated worktree's mutation lifecycle) exactly the way
    `runner.local_worker_runner.RunStatus` already layers above it for
    ordinary runs. No model response, in this module or any other, can
    write this value directly -- only `coding.pipeline.run_coding_job()`
    (code) ever calls `store.coding_jobs_repo.CodingJobsRepo.
    update_in_transaction()`.

    `READY_FOR_HUMAN_MERGE` is the furthest state this pipeline may ever
    reach -- it means the underlying `core.states.TaskState` reached
    `COMPLETED` (a durable checkpoint exists) AND an independent Security
    verdict was `PASS`. It never merges, pushes, deploys, or mutates any
    branch -- see `docs/AUTONOMOUS_ENGINEERING_LOOP.md`."""

    CREATED = "CREATED"
    PREFLIGHT = "PREFLIGHT"
    WORKSPACE_READY = "WORKSPACE_READY"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    EVIDENCE_READY = "EVIDENCE_READY"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    IN_REPAIR = "IN_REPAIR"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    READY_FOR_HUMAN_MERGE = "READY_FOR_HUMAN_MERGE"
    BLOCKED = "BLOCKED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    FAILED = "FAILED"


TERMINAL_JOB_STATES = frozenset({
    CodingJobState.READY_FOR_HUMAN_MERGE, CodingJobState.BLOCKED,
    CodingJobState.HUMAN_REQUIRED, CodingJobState.FAILED,
})


class CoderFailureCategory(StrEnum):
    """A rich failure taxonomy -- never collapsed to a generic "Coder
    failed". Mirrors `workers.protocol.WorkerAdapterError`/
    `workers.execution.TurnOutcome.reason`'s own stable-reason-code
    posture, named explicitly so a caller never has to string-match a
    free-form reason to tell these apart."""

    TRANSPORT_TIMEOUT = "TRANSPORT_TIMEOUT"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    UNAUTHORIZED_CAPABILITY = "UNAUTHORIZED_CAPABILITY"
    TOOL_EXECUTION_DENIED = "TOOL_EXECUTION_DENIED"
    POLICY_REFUSAL = "POLICY_REFUSAL"
    CONTEXT_TOKEN_EXHAUSTION = "CONTEXT_TOKEN_EXHAUSTION"
    VALIDATION_FAILURE = "VALIDATION_FAILURE"
    SCOPE_VIOLATION = "SCOPE_VIOLATION"
    SECURITY_FAILURE = "SECURITY_FAILURE"
    ITERATION_BUDGET_EXHAUSTED = "ITERATION_BUDGET_EXHAUSTED"
    WALL_CLOCK_BUDGET_EXHAUSTED = "WALL_CLOCK_BUDGET_EXHAUSTED"
    REPEATED_MALFORMED_BEHAVIOR = "REPEATED_MALFORMED_BEHAVIOR"
    NO_FINAL_RESULT_PRODUCED = "NO_FINAL_RESULT_PRODUCED"


# --------------------------------------------------------------------------
# Reviewer
# --------------------------------------------------------------------------


class ReviewVerdict(StrEnum):
    PASS = "PASS"
    CHANGES_REQUIRED = "CHANGES_REQUIRED"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class ReviewFinding:
    """One itemized Reviewer finding -- `severity` is the model's own
    advisory label (mirrors `coding.contracts.CoderUnresolvedIssue`),
    never a code-owned risk classification."""

    description: str
    severity: str = "advisory"
    path: str = ""


@dataclass(frozen=True)
class ReviewerModelResult:
    """The ONLY output shape a Reviewer turn may ever produce -- a
    closed, model-facing schema parsed exclusively by
    `parse_reviewer_model_result()` below, mirroring `coding.contracts.
    parse_coder_model_result()`'s exact fail-closed posture."""

    verdict: ReviewVerdict
    summary: str
    findings: tuple[ReviewFinding, ...] = ()


@dataclass(frozen=True)
class ReviewResult:
    """Code-owned aggregate of one completed Reviewer turn -- combines
    the model's own `ReviewerModelResult` with the code-computed
    `diff_fingerprint` it was actually shown (`coding.mutation_guard`),
    never a model's own claim about what it reviewed. Constructed only by
    `coding.reviewer.run_reviewer_turn()`, after a turn has actually
    executed -- not a model response schema itself."""

    verdict: ReviewVerdict
    summary: str
    findings: tuple[ReviewFinding, ...]
    diff_fingerprint: str


def review_blocks_progress(result: ReviewResult) -> bool:
    """Pure, code-owned: whether this verdict must stop the job short of
    `READY_FOR_HUMAN_MERGE` without a repair round. Only `PASS` does not
    block -- `CHANGES_REQUIRED` and `BLOCKED` both do (the distinction
    between them is for evidence/reporting, not for this gate)."""
    return result.verdict != ReviewVerdict.PASS


# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------


class SecurityVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"


@dataclass(frozen=True)
class SecurityFinding:
    """One itemized Security finding. `blocking` is the model's own
    advisory judgment of severity, never itself sufficient to allow
    readiness -- `security_blocks_progress()` below is what actually
    decides that, from `result.verdict` alone."""

    description: str
    category: str
    blocking: bool = True


@dataclass(frozen=True)
class SecurityModelResult:
    """The ONLY output shape a Security turn may ever produce -- a
    closed, model-facing schema parsed exclusively by
    `parse_security_model_result()` below."""

    verdict: SecurityVerdict
    summary: str
    findings: tuple[SecurityFinding, ...] = ()


@dataclass(frozen=True)
class SecurityResult:
    """Code-owned aggregate of one completed Security turn -- see
    `ReviewResult`'s own docstring for why this pairs a model verdict
    with a code-computed fingerprint rather than trusting either alone."""

    verdict: SecurityVerdict
    summary: str
    findings: tuple[SecurityFinding, ...]
    diff_fingerprint: str


def security_blocks_progress(result: SecurityResult) -> bool:
    """Pure, code-owned: only `PASS` allows the job to ever reach
    `READY_FOR_HUMAN_MERGE`. `FAIL` and `HUMAN_REQUIRED` both block --
    `HUMAN_REQUIRED` is never silently treated as an implicit PASS
    (fail-closed on ambiguity, per `AGENTS.md`)."""
    return result.verdict != SecurityVerdict.PASS


# --------------------------------------------------------------------------
# Model-facing parsers (closed field sets, fail closed -- mirrors
# coding.contracts.parse_coder_model_result() exactly)
# --------------------------------------------------------------------------

_REVIEW_VERDICTS = frozenset(v.value for v in ReviewVerdict)
_SECURITY_VERDICTS = frozenset(v.value for v in SecurityVerdict)


def _is_mapping(value: object) -> bool:
    return isinstance(value, dict)


def _parse_list(items: object, parser) -> tuple | None:
    if not isinstance(items, (list, tuple)):
        return None
    parsed = []
    for item in items:
        result = parser(item)
        if result is None:
            return None
        parsed.append(result)
    return tuple(parsed)


def _parse_review_finding(item: object) -> ReviewFinding | None:
    if not _is_mapping(item):
        return None
    if set(item) - {"description", "severity", "path"}:
        return None
    description = item.get("description")
    if not isinstance(description, str) or not description:
        return None
    severity = item.get("severity", "advisory")
    if not isinstance(severity, str):
        return None
    path = item.get("path", "")
    if not isinstance(path, str):
        return None
    return ReviewFinding(description=description, severity=severity, path=path)


def parse_reviewer_model_result(data: object) -> ReviewerModelResult | None:
    """The ONE model-facing parser for Reviewer turns. `None` on anything
    that does not exactly match a closed `{verdict, summary, findings}`
    shape -- never a partial reconstruction, never a best-effort guess,
    mirroring `coding.contracts.parse_coder_model_result()` exactly."""
    if not isinstance(data, dict):
        return None
    if set(data) - {"verdict", "summary", "findings"}:
        return None
    verdict = data.get("verdict")
    if not isinstance(verdict, str) or verdict not in _REVIEW_VERDICTS:
        return None
    summary = data.get("summary", "")
    if not isinstance(summary, str):
        return None
    findings = _parse_list(data.get("findings", []), _parse_review_finding)
    if findings is None:
        return None
    return ReviewerModelResult(verdict=ReviewVerdict(verdict), summary=summary, findings=findings)


def _parse_security_finding(item: object) -> SecurityFinding | None:
    if not _is_mapping(item):
        return None
    if set(item) - {"description", "category", "blocking"}:
        return None
    description = item.get("description")
    if not isinstance(description, str) or not description:
        return None
    category = item.get("category")
    if not isinstance(category, str) or not category:
        return None
    blocking = item.get("blocking", True)
    if type(blocking) is not bool:
        return None
    return SecurityFinding(description=description, category=category, blocking=blocking)


def parse_security_model_result(data: object) -> SecurityModelResult | None:
    """The ONE model-facing parser for Security turns -- see
    `parse_reviewer_model_result()`'s docstring; same posture."""
    if not isinstance(data, dict):
        return None
    if set(data) - {"verdict", "summary", "findings"}:
        return None
    verdict = data.get("verdict")
    if not isinstance(verdict, str) or verdict not in _SECURITY_VERDICTS:
        return None
    summary = data.get("summary", "")
    if not isinstance(summary, str):
        return None
    findings = _parse_list(data.get("findings", []), _parse_security_finding)
    if findings is None:
        return None
    return SecurityModelResult(verdict=SecurityVerdict(verdict), summary=summary, findings=findings)


def diff_fingerprint(diff_text: str) -> str:
    """The one, shared way this package computes a content fingerprint
    for staleness checks (`coding.reviewer`/`coding.security_gate`'s own
    "was this verdict actually about the CURRENT diff" gate) -- a plain
    SHA-256 of the exact diff text a Reviewer/Security turn was shown.
    Deliberately not a Git tree id (`finalization.service`'s own,
    stronger content identity binds tree *content*, not the tool-visible
    unified-diff *text* a review turn actually reads) -- this fingerprint
    exists only to detect "the text this verdict was about is no longer
    the text now in front of us", not to serve as checkpoint-grade
    evidence of repository content itself."""
    return hashlib.sha256(diff_text.encode("utf-8")).hexdigest()
