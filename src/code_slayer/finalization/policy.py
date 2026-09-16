"""Pure, deterministic finalization decisions over explicit, verified facts.

No database access, no subprocess, no model call — exactly the same posture
as `policy.engine.PolicyEngine.evaluate()`/`evaluate_checkpoint()`, which
these functions deliberately mirror. Malformed input always fails closed to
`BLOCKED`, never raises past this module and never guesses a more permissive
outcome.
"""

from __future__ import annotations

from code_slayer.core.states import TaskState
from code_slayer.finalization.types import (
    FinalizationFacts,
    FinalizerDecision,
    FinalizerVerdict,
    ReviewEvidence,
    VerificationCommandResult,
)
from code_slayer.store.tool_operations_repo import OperationStatus


def _blocked(reason_code: str, detail: str = "") -> FinalizerDecision:
    return FinalizerDecision(FinalizerVerdict.BLOCKED, reason_code, TaskState.BLOCKED, detail)


def _invalid_environment(reason_code: str, detail: str = "") -> FinalizerDecision:
    return FinalizerDecision(
        FinalizerVerdict.INVALID_ENVIRONMENT, reason_code, TaskState.BLOCKED, detail,
    )


def _sane_common_facts(facts: FinalizationFacts) -> bool:
    if not isinstance(facts, FinalizationFacts):
        return False
    if not isinstance(facts.state, TaskState):
        return False
    if not all(isinstance(v, str) and v for v in (
        facts.task_id, facts.repo_id, facts.worktree_id,
    )):
        return False
    flags = (
        facts.identity_valid, facts.baseline_valid, facts.owned_paths_valid,
        facts.protected_conflict, facts.unresolved,
    )
    return all(type(flag) is bool for flag in flags)


def evaluate_verification(facts: FinalizationFacts) -> FinalizerDecision:
    """Decide the outcome of a `VERIFYING`-state task from verified
    evidence only. Never reads `facts.verification` items as trusted
    unless each is a real `VerificationCommandResult` — a caller passing
    anything else is malformed input, not a permissive default.

    Priority, each checked before the next (first match wins, exactly one
    decision is ever returned): malformed input -> identity -> unresolved
    operations -> protected-path conflict -> baseline drift -> owned-content
    drift -> no groundable verification commands (`INVALID_ENVIRONMENT` —
    an environment problem, not a code defect) -> an uncertain command
    execution (`INVALID_ENVIRONMENT`) -> a deterministically failed command
    (`REPAIR_REQUIRED`, or `BLOCKED` once the repair bound is exhausted) ->
    an unresolved, blocking review requirement (same repair/blocked
    handling) -> `VERIFIED`.
    """
    if not _sane_common_facts(facts):
        return _blocked("malformed_finalization_input")
    if not isinstance(facts.verification, tuple) or not all(
        isinstance(r, VerificationCommandResult) for r in facts.verification
    ):
        return _blocked("malformed_finalization_input")
    if facts.review is not None and not isinstance(facts.review, ReviewEvidence):
        return _blocked("malformed_finalization_input")
    if type(facts.repair_attempts) is not int or facts.repair_attempts < 0:
        return _blocked("malformed_finalization_input")
    if type(facts.max_repair_attempts) is not int or facts.max_repair_attempts < 0:
        return _blocked("malformed_finalization_input")

    if not facts.identity_valid:
        return _blocked("identity_mismatch")
    if facts.unresolved:
        return _blocked("unresolved_operations")
    if facts.protected_conflict:
        return _blocked("protected_baseline_path")
    if not facts.baseline_valid:
        return _blocked("baseline_drift_detected")
    if not facts.owned_paths_valid:
        return _blocked("owned_content_changed")

    if not facts.verification:
        # No repository-grounded, code-owned-allowlisted verification
        # command could even be identified/run for this task -- this is a
        # fact about the runtime/repository environment, never about
        # whether the worker's own changes were correct. Environment
        # problems are normally reparable, so this maps to BLOCKED with a
        # structured reason_code, not a terminal failure.
        return _invalid_environment("no_verification_commands_grounded")
    if any(r.status == OperationStatus.UNKNOWN for r in facts.verification):
        return _invalid_environment("verification_execution_uncertain")

    failed = [r for r in facts.verification if r.status == OperationStatus.FAILED]
    if failed:
        if facts.repair_attempts >= facts.max_repair_attempts:
            return _blocked("repair_attempts_exhausted")
        return FinalizerDecision(
            FinalizerVerdict.REPAIR_REQUIRED, f"verification_command_failed:{failed[0].command}",
            TaskState.REPAIRING,
        )

    if facts.review is not None and facts.review.blocking and not facts.review.approved:
        if facts.repair_attempts >= facts.max_repair_attempts:
            return _blocked("repair_attempts_exhausted")
        return FinalizerDecision(
            FinalizerVerdict.REPAIR_REQUIRED, "review_requested_changes", TaskState.REPAIRING,
        )

    if facts.review is not None:
        # A real review verdict was actually supplied and approved -- REVIEWING
        # truthfully happened, so it is recorded as its own hop (a future
        # reviewer-aware caller, not built in this patch, drives it onward
        # from there). No reviewer exists in this patch, so this branch is
        # currently unreachable in production -- `facts.review` is always
        # `None` -- but the contract is unaffected by the change below.
        return FinalizerDecision(
            FinalizerVerdict.VERIFIED, "all_verification_commands_passed", TaskState.REVIEWING,
        )
    # No reviewer was consulted at all (review_required = false in this
    # phase/version): audit/state history must stay truthful, so REVIEWING
    # is never entered as a pretend pass-through -- go directly to
    # READY_FOR_CHECKPOINT.
    return FinalizerDecision(
        FinalizerVerdict.VERIFIED, "all_verification_commands_passed",
        TaskState.READY_FOR_CHECKPOINT,
    )


def evaluate_completion(facts: FinalizationFacts) -> FinalizerDecision:
    """Decide whether a `CHECKPOINTED` task may become `COMPLETED`
    (`FINAL`). Never re-runs verification here — it consumes only durable
    checkpoint existence, a durable record that verification already
    passed for this task without being superseded by a later, unverified
    mutation (`prior_verification_confirmed`), AND that the checkpoint
    actually being completed represents the *same content* that record
    was about (`content_fingerprint_matches`) — closing the TOCTOU gap
    where content is verified, the repository changes, a *different*
    content set is checkpointed, and a stale `VERIFIED` record would
    otherwise still read as valid. Both booleans are computed by the
    caller from durable evidence — see `finalization.service`."""
    if not _sane_common_facts(facts):
        return _blocked("malformed_finalization_input")
    if not all(type(v) is bool for v in (
        facts.checkpoint_confirmed, facts.prior_verification_confirmed,
        facts.content_fingerprint_matches,
    )):
        return _blocked("malformed_finalization_input")

    if facts.unresolved:
        return _blocked("unresolved_operations")
    if not facts.checkpoint_confirmed:
        return _blocked("no_checkpoint_evidence")
    if not facts.prior_verification_confirmed:
        return _blocked("no_verification_evidence")
    if not facts.content_fingerprint_matches:
        return _blocked("verification_content_mismatch")
    return FinalizerDecision(
        FinalizerVerdict.FINAL, "checkpoint_and_verification_evidence_confirmed",
        TaskState.COMPLETED,
    )
