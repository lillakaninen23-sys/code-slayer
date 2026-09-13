"""Pure PolicyEngine.evaluate() decisions over explicit facts.

No filesystem, database, or subprocess involved: every fact is supplied
directly as a `PolicyInput`, so these tests pin the decision table itself,
independent of how `ToolExecutor._facts()` happens to derive those facts.
"""

from __future__ import annotations

import pytest

from code_slayer.core.states import TaskState
from code_slayer.policy.engine import Decision, PolicyEngine, PolicyInput
from code_slayer.tools.models import RiskClass

ENGINE = PolicyEngine()


def outcome(result):
    # PolicyResult is a frozen dataclass; it never compares equal to a
    # plain tuple, so tests compare this projection instead.
    return (result.decision, result.reason)


def facts(**overrides) -> PolicyInput:
    base = dict(
        task_id="t1", repo_id="r1", worktree_id="w1", state=TaskState.IMPLEMENTING,
        tool="write_file", resource="a.txt", risk=RiskClass.WRITE_OWNED, network=False,
        baseline_valid=True, scope_valid=True, protected=False, owned=True,
        pre_existing=True, unresolved=False,
    )
    base.update(overrides)
    return PolicyInput(**base)


def test_non_policy_input_denies_malformed():
    result = ENGINE.evaluate("not a PolicyInput")  # type: ignore[arg-type]
    assert result.decision == Decision.DENY
    assert result.reason == "malformed_policy_input"


@pytest.mark.parametrize("field", ["state", "risk"])
def test_wrong_enum_type_denies_malformed(field):
    result = ENGINE.evaluate(facts(**{field: "not-an-enum"}))
    assert outcome(result) == (Decision.DENY, "malformed_policy_input")


@pytest.mark.parametrize("field", [
    "network", "baseline_valid", "scope_valid", "protected", "owned", "pre_existing", "unresolved",
])
def test_non_bool_flag_denies_malformed(field):
    result = ENGINE.evaluate(facts(**{field: 1}))
    assert result.decision == Decision.DENY
    assert result.reason == "malformed_policy_input"


@pytest.mark.parametrize("field", ["task_id", "repo_id", "worktree_id", "tool", "resource"])
def test_empty_or_non_string_identity_denies_malformed(field):
    assert ENGINE.evaluate(facts(**{field: ""})).reason == "malformed_policy_input"
    assert ENGINE.evaluate(facts(**{field: 123})).reason == "malformed_policy_input"


def test_unknown_tool_denies():
    result = ENGINE.evaluate(facts(tool="delete_repo"))
    assert outcome(result) == (Decision.DENY, "unknown_tool")


def test_network_flag_denies_even_for_read_only_tool():
    result = ENGINE.evaluate(facts(tool="read_file", risk=RiskClass.READ_ONLY, network=True))
    assert outcome(result) == (Decision.DENY, "network_denied")


def test_network_risk_class_denies():
    result = ENGINE.evaluate(facts(risk=RiskClass.NETWORK))
    assert outcome(result) == (Decision.DENY, "network_denied")


def test_invalid_baseline_denies():
    result = ENGINE.evaluate(facts(baseline_valid=False))
    assert outcome(result) == (Decision.DENY, "baseline_required")


def test_outside_scope_denies():
    result = ENGINE.evaluate(facts(scope_valid=False))
    assert outcome(result) == (Decision.DENY, "outside_task_scope")


@pytest.mark.parametrize("state", [TaskState.CREATED, TaskState.INSPECTING])
def test_early_lifecycle_states_deny_even_read_only(state):
    result = ENGINE.evaluate(
        facts(tool="read_file", risk=RiskClass.READ_ONLY, state=state, owned=False),
    )
    assert outcome(result) == (Decision.DENY, "wrong_task_state")


@pytest.mark.parametrize("state", [TaskState.INTERRUPTED_RESUMABLE, TaskState.BLOCKED,
                                    TaskState.COMPLETED, TaskState.FAILED])
def test_suspended_or_terminal_states_deny(state):
    result = ENGINE.evaluate(
        facts(tool="read_file", risk=RiskClass.READ_ONLY, state=state, owned=False),
    )
    assert outcome(result) == (Decision.DENY, "wrong_task_state")


def test_read_only_tool_allowed_in_planning_before_implementing():
    result = ENGINE.evaluate(
        facts(tool="read_file", risk=RiskClass.READ_ONLY, state=TaskState.PLANNING, owned=False),
    )
    assert outcome(result) == (Decision.ALLOW, "allowed")


def test_run_command_allowed_outside_implementing():
    result = ENGINE.evaluate(
        facts(tool="run_command", resource=".", risk=RiskClass.GIT_READ,
              state=TaskState.VERIFYING, owned=False),
    )
    assert outcome(result) == (Decision.ALLOW, "allowed")


@pytest.mark.parametrize("state", [TaskState.BASELINED, TaskState.PLANNING, TaskState.PLANNED,
                                    TaskState.VERIFYING, TaskState.REVIEWING])
def test_mutation_denied_outside_implementing_or_repairing(state):
    result = ENGINE.evaluate(facts(state=state))
    assert outcome(result) == (Decision.DENY, "wrong_task_state")


@pytest.mark.parametrize("state", [TaskState.IMPLEMENTING, TaskState.REPAIRING])
def test_mutation_allowed_states(state):
    result = ENGINE.evaluate(facts(state=state))
    assert outcome(result) == (Decision.ALLOW, "allowed")


def test_protected_path_denies_even_when_owned():
    result = ENGINE.evaluate(facts(protected=True, owned=True))
    assert outcome(result) == (Decision.DENY, "protected_baseline_path")


def test_unresolved_operation_blocks_mutation():
    result = ENGINE.evaluate(facts(unresolved=True))
    assert outcome(result) == (Decision.DENY, "reconciliation_required")


def test_create_file_over_existing_path_denies_not_a_new_path():
    """create_file has no legitimate approval path (regression: this must
    not become REQUIRE_APPROVAL just because risk looks like WRITE_EXISTING
    upstream)."""
    result = ENGINE.evaluate(
        facts(tool="create_file", risk=RiskClass.WRITE_OWNED,
              pre_existing=True, owned=False),
    )
    assert outcome(result) == (Decision.DENY, "not_a_new_path")


def test_create_file_over_owned_path_denies_not_a_new_path():
    result = ENGINE.evaluate(
        facts(tool="create_file", risk=RiskClass.WRITE_OWNED,
              pre_existing=False, owned=True),
    )
    assert outcome(result) == (Decision.DENY, "not_a_new_path")


def test_create_file_new_path_allowed():
    result = ENGINE.evaluate(
        facts(tool="create_file", risk=RiskClass.WRITE_OWNED,
              pre_existing=False, owned=False),
    )
    assert outcome(result) == (Decision.ALLOW, "allowed")


def test_create_file_with_write_existing_risk_still_denies_outright():
    """Even if upstream risk classification is (incorrectly) WRITE_EXISTING
    for create_file, the policy engine must still deny, never approve."""
    result = ENGINE.evaluate(
        facts(tool="create_file", risk=RiskClass.WRITE_EXISTING,
              pre_existing=True, owned=False),
    )
    assert outcome(result) == (Decision.DENY, "not_a_new_path")


def test_write_file_not_owned_requires_approval_never_executes_via_decision_alone():
    result = ENGINE.evaluate(
        facts(tool="write_file", risk=RiskClass.WRITE_EXISTING, owned=False, pre_existing=True),
    )
    assert outcome(result) == (Decision.REQUIRE_APPROVAL, "existing_path_requires_approval")


def test_write_file_owned_allowed():
    result = ENGINE.evaluate(facts(tool="write_file", risk=RiskClass.WRITE_OWNED, owned=True))
    assert outcome(result) == (Decision.ALLOW, "allowed")


def test_write_file_owned_but_wrong_risk_denies_unsupported_risk():
    result = ENGINE.evaluate(facts(tool="write_file", risk=RiskClass.READ_ONLY, owned=True))
    assert outcome(result) == (Decision.DENY, "unsupported_risk")


def test_apply_patch_without_ownership_row_requires_approval():
    result = ENGINE.evaluate(
        facts(tool="apply_patch", risk=RiskClass.WRITE_EXISTING, owned=False),
    )
    assert outcome(result) == (Decision.REQUIRE_APPROVAL, "existing_path_requires_approval")


def test_read_only_tool_risk_mismatch_denies_unsupported_risk():
    """Defense in depth: even if a caller manages to attach the wrong risk
    to a read-only capability, the engine still refuses it."""
    result = ENGINE.evaluate(
        facts(tool="read_file", risk=RiskClass.WRITE_OWNED, state=TaskState.IMPLEMENTING,
              owned=False),
    )
    assert outcome(result) == (Decision.DENY, "unsupported_risk")


def test_run_command_risk_mismatch_denies_unsupported_risk():
    result = ENGINE.evaluate(
        facts(tool="run_command", resource=".", risk=RiskClass.WRITE_OWNED,
              state=TaskState.IMPLEMENTING, owned=False),
    )
    assert outcome(result) == (Decision.DENY, "unsupported_risk")
