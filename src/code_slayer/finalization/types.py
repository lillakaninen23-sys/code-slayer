"""Finalizer verdict, evidence, and decision types.

Every dataclass here is a plain, frozen evidence container — never a place
that reads a database or calls a model. `FinalizationFacts` is the complete,
explicit input to `finalization.policy`'s pure decision functions: nothing
those functions decide may come from anywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from code_slayer.core.states import TaskState


class FinalizerVerdict(StrEnum):
    """The complete vocabulary a finalizer decision may produce.

    `VERIFIED` is an internal pass-through outcome (verification evidence is
    clean; the task proceeds toward checkpointing) — it is deliberately not
    one of the four outcomes named by the Deterministic Finalization design
    (`FINAL`, `REPAIR_REQUIRED`, `BLOCKED`, `INVALID_ENVIRONMENT`), since
    verification passing is not yet job completion. `FINAL` is reserved for
    `evaluate_completion()`'s own decision, made only once a durable
    checkpoint and prior verification evidence both exist.
    """

    VERIFIED = "VERIFIED"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    BLOCKED = "BLOCKED"
    INVALID_ENVIRONMENT = "INVALID_ENVIRONMENT"
    FINAL = "FINAL"


@dataclass(frozen=True)
class VerificationCommandResult:
    """Durable outcome of one actually-executed, repository-grounded
    verification command. `operation_id` names the exact `tool_operations`
    row this result came from — the finalizer's evidence is always
    traceable back to that append-only journal, never to this in-memory
    copy alone."""

    command: str
    purpose: str
    evidence_source: str
    confidence: str
    argv: tuple[str, ...]
    operation_id: str
    status: str  # OperationStatus: SUCCEEDED | FAILED | UNKNOWN
    returncode: int | None
    truncated: bool
    timed_out: bool
    reason: str


@dataclass(frozen=True)
class ReviewEvidence:
    """Placeholder input contract for a future reviewer verdict.

    No reviewer model is built or called in this patch — nothing in this
    codebase ever constructs a real `ReviewEvidence` yet. It exists purely
    so `finalization.policy` has a stable, forward-compatible place to
    accept review evidence later without changing its function signatures
    or the `FinalizationFacts` shape. When a caller does not supply one
    (`review=None`, the only case reachable today), a REVIEWING gate is
    treated as vacuously satisfied.
    """

    approved: bool
    reason: str
    blocking: bool = True


@dataclass(frozen=True)
class FinalizationFacts:
    """Complete, explicit evidence for one finalization decision.

    Fields with no default are required for every decision.
    `verification`/`review`/`repair_attempts`/`max_repair_attempts` matter
    only to `evaluate_verification()`; `checkpoint_confirmed`/
    `prior_verification_confirmed`/`content_fingerprint_matches` matter
    only to `evaluate_completion()` — each decision function ignores the
    fields it does not use, and never trusts an unrelated field's default
    as if it were verified evidence. `content_fingerprint_matches`
    defaults `False` (fail-closed): it must be explicitly proven by the
    caller — comparing the Git tree id (`verified_tree_sha`) a `VERIFIED`
    decision recorded at verification time against the checkpoint's own
    already-durable `store.checkpoint_repo.CheckpointVerified.tree_sha`
    (see `finalization.service.checkpointed_completion_guard`) — never
    assumed true just because a checkpoint and some prior `VERIFIED`
    record both happen to exist. A Git tree id is a strong, already-
    established content identity: it is exactly the same tree object
    `repo.checkpoint.CheckpointManager` itself commits into a real
    checkpoint, built the same way (`repo.checkpoint_git.build_tree()`)
    from the same inputs (the baseline tree plus each owned path's own
    blob) — so two tree ids can only match if the *full* content (every
    owned path's exact bytes, not just its path name) is identical, never
    merely because the same path names or a hash of hashes happen to
    coincide.
    """

    task_id: str
    repo_id: str
    worktree_id: str
    state: TaskState
    identity_valid: bool
    baseline_valid: bool
    owned_paths_valid: bool
    protected_conflict: bool
    unresolved: bool
    verification: tuple[VerificationCommandResult, ...] = ()
    review: ReviewEvidence | None = None
    repair_attempts: int = 0
    max_repair_attempts: int = 3
    checkpoint_confirmed: bool = False
    prior_verification_confirmed: bool = False
    content_fingerprint_matches: bool = False


@dataclass(frozen=True)
class FinalizerDecision:
    """One deterministic decision. `target_state` is always one of
    `TaskState.BLOCKED` (both `BLOCKED` and `INVALID_ENVIRONMENT` verdicts —
    see the package docstring), `TaskState.REPAIRING`, `TaskState.REVIEWING`
    (the `VERIFIED` pass-through), or `TaskState.COMPLETED` (`FINAL`
    only) — never `None`; a malformed-input decision still names a safe,
    fail-closed target (`BLOCKED`)."""

    verdict: FinalizerVerdict
    reason_code: str
    target_state: TaskState
    detail: str = ""
