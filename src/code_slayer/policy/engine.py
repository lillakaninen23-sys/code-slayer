"""Pure decisions over explicit, validated task and baseline facts."""

from dataclasses import dataclass
from enum import StrEnum

from code_slayer.core.states import TaskState, is_active
from code_slayer.tools.models import RiskClass
from code_slayer.tools.registry import CAPABILITIES


class Decision(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


@dataclass(frozen=True)
class PolicyResult:
    decision: Decision
    reason: str


@dataclass(frozen=True)
class PolicyInput:
    task_id: str
    repo_id: str
    worktree_id: str
    state: TaskState
    tool: str
    resource: str
    risk: RiskClass
    network: bool
    baseline_valid: bool
    scope_valid: bool
    protected: bool
    owned: bool
    pre_existing: bool
    unresolved: bool


def deny(reason: str) -> PolicyResult:
    return PolicyResult(Decision.DENY, reason)


class PolicyEngine:
    def evaluate(self, facts: PolicyInput) -> PolicyResult:
        if not isinstance(facts, PolicyInput):
            return deny("malformed_policy_input")
        if not isinstance(facts.state, TaskState) or not isinstance(facts.risk, RiskClass):
            return deny("malformed_policy_input")
        flags = (facts.network, facts.baseline_valid, facts.scope_valid, facts.protected,
                 facts.owned, facts.pre_existing, facts.unresolved)
        if any(type(flag) is not bool for flag in flags):
            return deny("malformed_policy_input")
        if not all(isinstance(v, str) and v for v in (
            facts.task_id, facts.repo_id, facts.worktree_id, facts.tool, facts.resource,
        )):
            return deny("malformed_policy_input")
        capability = CAPABILITIES.get(facts.tool)
        if capability is None:
            return deny("unknown_tool")
        if facts.network or facts.risk == RiskClass.NETWORK:
            return deny("network_denied")
        if not facts.baseline_valid:
            return deny("baseline_required")
        if not facts.scope_valid:
            return deny("outside_task_scope")
        if not is_active(facts.state) or facts.state in (TaskState.CREATED, TaskState.INSPECTING):
            return deny("wrong_task_state")
        if capability.mutation:
            if facts.state not in (TaskState.IMPLEMENTING, TaskState.REPAIRING):
                return deny("wrong_task_state")
            if facts.protected:
                return deny("protected_baseline_path")
            if facts.unresolved:
                return deny("reconciliation_required")
            # create_file has no legitimate approval path: an existing or
            # already-owned path is a wrong tool choice, not something a
            # future approver could ever grant (O_CREAT|O_EXCL would still
            # refuse it at the filesystem layer). Deny outright, checked
            # before risk classification so an upstream WRITE_EXISTING
            # classification can never turn this into REQUIRE_APPROVAL.
            if facts.tool == "create_file":
                if facts.pre_existing or facts.owned:
                    return deny("not_a_new_path")
                if facts.risk != RiskClass.WRITE_OWNED:
                    return deny("unsupported_risk")
            else:
                if facts.risk == RiskClass.WRITE_EXISTING:
                    return PolicyResult(
                        Decision.REQUIRE_APPROVAL, "existing_path_requires_approval",
                    )
                if facts.risk != RiskClass.WRITE_OWNED:
                    return deny("unsupported_risk")
                if not facts.owned:
                    return deny("ownership_required")
        elif facts.risk != capability.risk:
            return deny("unsupported_risk")
        return PolicyResult(Decision.ALLOW, "allowed")


@dataclass(frozen=True)
class CheckpointPolicyInput:
    """Facts for the one checkpoint-creation decision (Phase 5).

    Deliberately separate from `PolicyInput`: a checkpoint has no single
    `resource` path, no per-path ownership/pre-existing distinction, and
    is legal from exactly one task state — reusing the path-shaped
    `PolicyInput`/`evaluate()` for it would either misuse those fields or
    force `evaluate()` to branch heavily on tool identity. A narrow,
    dedicated decision function keeps both auditable independently.
    """

    task_id: str
    repo_id: str
    worktree_id: str
    state: TaskState
    network: bool
    identity_valid: bool
    baseline_valid: bool
    owned_paths_valid: bool
    protected_conflict: bool
    unresolved: bool


def evaluate_checkpoint(facts: CheckpointPolicyInput) -> PolicyResult:
    """ALLOW/DENY a checkpoint-creation request. Never REQUIRE_APPROVAL:
    a checkpoint either safely represents what Code Slayer owns right now
    or it does not — there is no partial/approvable middle ground here."""
    if not isinstance(facts, CheckpointPolicyInput):
        return deny("malformed_policy_input")
    if not isinstance(facts.state, TaskState):
        return deny("malformed_policy_input")
    flags = (
        facts.network, facts.identity_valid, facts.baseline_valid,
        facts.owned_paths_valid, facts.protected_conflict, facts.unresolved,
    )
    if any(type(flag) is not bool for flag in flags):
        return deny("malformed_policy_input")
    if not all(isinstance(v, str) and v for v in (
        facts.task_id, facts.repo_id, facts.worktree_id,
    )):
        return deny("malformed_policy_input")
    if facts.network:
        return deny("network_denied")
    if not facts.identity_valid:
        return deny("identity_mismatch")
    if facts.state != TaskState.READY_FOR_CHECKPOINT:
        return deny("wrong_task_state")
    if facts.unresolved:
        return deny("reconciliation_required")
    if facts.protected_conflict:
        return deny("protected_baseline_path")
    if not facts.baseline_valid:
        return deny("baseline_drift_detected")
    if not facts.owned_paths_valid:
        return deny("owned_content_changed")
    return PolicyResult(Decision.ALLOW, "allowed")
