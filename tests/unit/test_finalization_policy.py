"""Pure decision coverage for `finalization.policy` -- every branch,
priority order, and fail-closed malformed-input path."""

from __future__ import annotations

from code_slayer.core.states import TaskState
from code_slayer.finalization.policy import evaluate_completion, evaluate_verification
from code_slayer.finalization.types import (
    FinalizationFacts,
    FinalizerVerdict,
    ReviewEvidence,
    VerificationCommandResult,
)
from code_slayer.store.tool_operations_repo import OperationStatus

TASK_ID, REPO_ID, WORKTREE_ID = "t1", "r1", "w1"


def _result(command="pytest", status=OperationStatus.SUCCEEDED, **kw) -> VerificationCommandResult:
    return VerificationCommandResult(
        command=command, purpose="test", evidence_source="pyproject.toml", confidence="high",
        argv=(command,), operation_id="op1", status=status, returncode=kw.get("returncode", 0),
        truncated=False, timed_out=False, reason=kw.get("reason", "command_completed"),
    )


def _facts(**overrides) -> FinalizationFacts:
    base = dict(
        task_id=TASK_ID, repo_id=REPO_ID, worktree_id=WORKTREE_ID, state=TaskState.VERIFYING,
        identity_valid=True, baseline_valid=True, owned_paths_valid=True,
        protected_conflict=False, unresolved=False,
    )
    base.update(overrides)
    return FinalizationFacts(**base)


# -- evaluate_verification: malformed input --------------------------------

def test_verification_rejects_non_facts_object():
    decision = evaluate_verification("not facts")
    assert decision.verdict == FinalizerVerdict.BLOCKED
    assert decision.reason_code == "malformed_finalization_input"
    assert decision.target_state == TaskState.BLOCKED


def test_verification_rejects_wrong_state_type():
    decision = evaluate_verification(_facts(state="VERIFYING"))
    assert decision.reason_code == "malformed_finalization_input"


def test_verification_rejects_non_bool_flag():
    decision = evaluate_verification(_facts(unresolved="yes"))
    assert decision.reason_code == "malformed_finalization_input"


def test_verification_rejects_malformed_verification_items():
    decision = evaluate_verification(_facts(verification=("not a result",)))
    assert decision.reason_code == "malformed_finalization_input"


def test_verification_rejects_malformed_review():
    decision = evaluate_verification(_facts(review="not review evidence"))
    assert decision.reason_code == "malformed_finalization_input"


def test_verification_rejects_negative_repair_attempts():
    decision = evaluate_verification(_facts(repair_attempts=-1))
    assert decision.reason_code == "malformed_finalization_input"


# -- evaluate_verification: priority order ---------------------------------

def test_identity_mismatch_blocks_before_anything_else():
    decision = evaluate_verification(_facts(identity_valid=False, unresolved=True))
    assert decision.verdict == FinalizerVerdict.BLOCKED
    assert decision.reason_code == "identity_mismatch"
    assert decision.target_state == TaskState.BLOCKED


def test_unresolved_operations_block():
    decision = evaluate_verification(_facts(unresolved=True))
    assert decision.reason_code == "unresolved_operations"
    assert decision.target_state == TaskState.BLOCKED


def test_protected_conflict_blocks():
    decision = evaluate_verification(_facts(protected_conflict=True))
    assert decision.reason_code == "protected_baseline_path"


def test_baseline_drift_blocks():
    decision = evaluate_verification(_facts(baseline_valid=False))
    assert decision.reason_code == "baseline_drift_detected"


def test_owned_content_drift_blocks():
    decision = evaluate_verification(_facts(owned_paths_valid=False))
    assert decision.reason_code == "owned_content_changed"


def test_no_verification_commands_is_invalid_environment():
    decision = evaluate_verification(_facts(verification=()))
    assert decision.verdict == FinalizerVerdict.INVALID_ENVIRONMENT
    assert decision.reason_code == "no_verification_commands_grounded"
    assert decision.target_state == TaskState.BLOCKED


def test_uncertain_execution_is_invalid_environment():
    decision = evaluate_verification(
        _facts(verification=(_result(status=OperationStatus.UNKNOWN),)),
    )
    assert decision.verdict == FinalizerVerdict.INVALID_ENVIRONMENT
    assert decision.reason_code == "verification_execution_uncertain"


def test_one_uncertain_command_wins_over_a_failed_one():
    decision = evaluate_verification(_facts(verification=(
        _result(command="ruff check .", status=OperationStatus.FAILED),
        _result(command="pytest", status=OperationStatus.UNKNOWN),
    )))
    assert decision.verdict == FinalizerVerdict.INVALID_ENVIRONMENT


def test_failed_command_requires_repair():
    decision = evaluate_verification(
        _facts(verification=(_result(status=OperationStatus.FAILED),)),
    )
    assert decision.verdict == FinalizerVerdict.REPAIR_REQUIRED
    assert decision.reason_code == "verification_command_failed:pytest"
    assert decision.target_state == TaskState.REPAIRING


def test_failed_command_at_repair_bound_blocks_instead():
    decision = evaluate_verification(_facts(
        verification=(_result(status=OperationStatus.FAILED),),
        repair_attempts=3, max_repair_attempts=3,
    ))
    assert decision.verdict == FinalizerVerdict.BLOCKED
    assert decision.reason_code == "repair_attempts_exhausted"


def test_blocking_unapproved_review_requires_repair():
    decision = evaluate_verification(_facts(
        verification=(_result(status=OperationStatus.SUCCEEDED),),
        review=ReviewEvidence(approved=False, reason="needs changes", blocking=True),
    ))
    assert decision.verdict == FinalizerVerdict.REPAIR_REQUIRED
    assert decision.reason_code == "review_requested_changes"


def test_non_blocking_unapproved_review_does_not_block_verified():
    decision = evaluate_verification(_facts(
        verification=(_result(status=OperationStatus.SUCCEEDED),),
        review=ReviewEvidence(approved=False, reason="fyi", blocking=False),
    ))
    assert decision.verdict == FinalizerVerdict.VERIFIED
    # A real review verdict was actually supplied (even if non-blocking and
    # not approved) -- REVIEWING truthfully happened, so it is the target,
    # never skipped.
    assert decision.target_state == TaskState.REVIEWING


def test_approved_review_does_not_block():
    decision = evaluate_verification(_facts(
        verification=(_result(status=OperationStatus.SUCCEEDED),),
        review=ReviewEvidence(approved=True, reason="looks good"),
    ))
    assert decision.verdict == FinalizerVerdict.VERIFIED
    assert decision.target_state == TaskState.REVIEWING


# -- review_required = false: REVIEWING is never a pretend pass-through -----

def test_all_commands_succeed_with_no_review_goes_directly_to_ready_for_checkpoint():
    """review_required = false in this phase/version (no reviewer model
    exists): audit/state history must stay truthful -- VERIFIED with no
    review supplied goes straight to READY_FOR_CHECKPOINT, never through a
    pretend REVIEWING hop."""
    decision = evaluate_verification(_facts(verification=(
        _result(command="pytest"), _result(command="ruff check ."),
    )))
    assert decision.verdict == FinalizerVerdict.VERIFIED
    assert decision.reason_code == "all_verification_commands_passed"
    assert decision.target_state == TaskState.READY_FOR_CHECKPOINT


def test_future_review_evidence_contract_still_routes_through_reviewing():
    """The future `ReviewEvidence` contract is unaffected by the
    review_required=false change: once a real (approved) review verdict
    is actually supplied, VERIFIED correctly targets REVIEWING again."""
    decision = evaluate_verification(_facts(
        verification=(_result(status=OperationStatus.SUCCEEDED),),
        review=ReviewEvidence(approved=True, reason="looks good"),
    ))
    assert decision.target_state == TaskState.REVIEWING


# -- evaluate_completion ----------------------------------------------------

def test_completion_rejects_malformed_input():
    decision = evaluate_completion("nope")
    assert decision.reason_code == "malformed_finalization_input"


def test_completion_rejects_non_bool_confirmation_flags():
    decision = evaluate_completion(_facts(checkpoint_confirmed="yes"))
    assert decision.reason_code == "malformed_finalization_input"


def test_completion_blocks_on_unresolved_operations():
    decision = evaluate_completion(_facts(
        unresolved=True, checkpoint_confirmed=True, prior_verification_confirmed=True,
    ))
    assert decision.reason_code == "unresolved_operations"


def test_completion_blocks_without_checkpoint_evidence():
    decision = evaluate_completion(_facts(
        checkpoint_confirmed=False, prior_verification_confirmed=True,
    ))
    assert decision.reason_code == "no_checkpoint_evidence"


def test_completion_blocks_without_prior_verification_evidence():
    decision = evaluate_completion(_facts(
        checkpoint_confirmed=True, prior_verification_confirmed=False,
    ))
    assert decision.reason_code == "no_verification_evidence"


def test_completion_blocks_without_content_fingerprint_match():
    """TOCTOU closure (approved amendment #2): a real checkpoint and an
    unsuperseded VERIFIED record both existing is still not enough -- the
    checkpoint must represent the exact content that record was about."""
    decision = evaluate_completion(_facts(
        checkpoint_confirmed=True, prior_verification_confirmed=True,
        content_fingerprint_matches=False,
    ))
    assert decision.verdict == FinalizerVerdict.BLOCKED
    assert decision.reason_code == "verification_content_mismatch"


def test_content_fingerprint_matches_defaults_fail_closed():
    """Not explicitly proving the fingerprint match must never default to
    FINAL just because the other two confirmations are true."""
    decision = evaluate_completion(FinalizationFacts(
        task_id=TASK_ID, repo_id=REPO_ID, worktree_id=WORKTREE_ID, state=TaskState.CHECKPOINTED,
        identity_valid=True, baseline_valid=True, owned_paths_valid=True,
        protected_conflict=False, unresolved=False,
        checkpoint_confirmed=True, prior_verification_confirmed=True,
    ))
    assert decision.verdict == FinalizerVerdict.BLOCKED
    assert decision.reason_code == "verification_content_mismatch"


def test_completion_is_final_only_with_all_three_confirmations():
    decision = evaluate_completion(_facts(
        checkpoint_confirmed=True, prior_verification_confirmed=True,
        content_fingerprint_matches=True,
    ))
    assert decision.verdict == FinalizerVerdict.FINAL
    assert decision.target_state == TaskState.COMPLETED
