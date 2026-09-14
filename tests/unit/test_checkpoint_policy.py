"""Pure `evaluate_checkpoint()` decisions over explicit facts."""

from __future__ import annotations

import pytest

from code_slayer.core.states import TaskState
from code_slayer.policy.engine import CheckpointPolicyInput, Decision, evaluate_checkpoint


def outcome(result):
    return (result.decision, result.reason)


def facts(**overrides) -> CheckpointPolicyInput:
    base = dict(
        task_id="t1", repo_id="r1", worktree_id="w1", state=TaskState.READY_FOR_CHECKPOINT,
        network=False, identity_valid=True, baseline_valid=True, owned_paths_valid=True,
        protected_conflict=False, unresolved=False,
    )
    base.update(overrides)
    return CheckpointPolicyInput(**base)


def test_non_input_denies_malformed():
    result = evaluate_checkpoint("nope")  # type: ignore[arg-type]
    assert outcome(result) == (Decision.DENY, "malformed_policy_input")


def test_wrong_state_type_denies_malformed():
    result = evaluate_checkpoint(facts(state="READY_FOR_CHECKPOINT"))
    assert outcome(result) == (Decision.DENY, "malformed_policy_input")


@pytest.mark.parametrize("field", [
    "network", "identity_valid", "baseline_valid", "owned_paths_valid",
    "protected_conflict", "unresolved",
])
def test_non_bool_flag_denies_malformed(field):
    result = evaluate_checkpoint(facts(**{field: 1}))
    assert outcome(result) == (Decision.DENY, "malformed_policy_input")


@pytest.mark.parametrize("field", ["task_id", "repo_id", "worktree_id"])
def test_empty_or_non_string_identity_denies_malformed(field):
    assert outcome(evaluate_checkpoint(facts(**{field: ""}))) == (
        Decision.DENY, "malformed_policy_input",
    )
    assert outcome(evaluate_checkpoint(facts(**{field: 7}))) == (
        Decision.DENY, "malformed_policy_input",
    )


def test_network_denies():
    result = evaluate_checkpoint(facts(network=True))
    assert outcome(result) == (Decision.DENY, "network_denied")


def test_identity_mismatch_denies():
    result = evaluate_checkpoint(facts(identity_valid=False))
    assert outcome(result) == (Decision.DENY, "identity_mismatch")


@pytest.mark.parametrize("state", [
    TaskState.CREATED, TaskState.INSPECTING, TaskState.BASELINED, TaskState.PLANNING,
    TaskState.PLANNED, TaskState.IMPLEMENTING, TaskState.VERIFYING, TaskState.REPAIRING,
    TaskState.REVIEWING, TaskState.CHECKPOINTED, TaskState.COMPLETED, TaskState.FAILED,
    TaskState.INTERRUPTED_RESUMABLE, TaskState.BLOCKED,
])
def test_only_ready_for_checkpoint_allows(state):
    result = evaluate_checkpoint(facts(state=state))
    assert outcome(result) == (Decision.DENY, "wrong_task_state")


def test_unresolved_operation_denies_before_baseline_check():
    result = evaluate_checkpoint(facts(unresolved=True, baseline_valid=False))
    assert outcome(result) == (Decision.DENY, "reconciliation_required")


def test_protected_conflict_denies():
    result = evaluate_checkpoint(facts(protected_conflict=True))
    assert outcome(result) == (Decision.DENY, "protected_baseline_path")


def test_baseline_drift_denies():
    result = evaluate_checkpoint(facts(baseline_valid=False))
    assert outcome(result) == (Decision.DENY, "baseline_drift_detected")


def test_owned_path_drift_denies():
    result = evaluate_checkpoint(facts(owned_paths_valid=False))
    assert outcome(result) == (Decision.DENY, "owned_content_changed")


def test_allows_when_everything_holds():
    result = evaluate_checkpoint(facts())
    assert outcome(result) == (Decision.ALLOW, "allowed")


def test_never_returns_require_approval():
    # No combination of facts should ever produce REQUIRE_APPROVAL: a
    # checkpoint is either safe to create now or it is not.
    from itertools import product

    for network, identity, baseline, owned, protected, unresolved in product(
        (False, True), repeat=6
    ):
        result = evaluate_checkpoint(facts(
            network=network, identity_valid=identity, baseline_valid=baseline,
            owned_paths_valid=owned, protected_conflict=protected, unresolved=unresolved,
        ))
        assert result.decision != Decision.REQUIRE_APPROVAL
