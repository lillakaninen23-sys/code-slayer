"""The Autonomous Engineering Loop orchestrator: Planner(handoff) ->
Coder -> independent mutation check -> Finalizer(verification+review) ->
bounded Repairer -> Security -> READY_FOR_HUMAN_MERGE.

Code owns every transition here -- no model response, in this module or
any it calls, can directly write a `coding.pipeline_types.CodingJobState`
success value or a `core.states.TaskState` transition; every state change
goes through `store.coding_jobs_repo.CodingJobsRepo`/`core.state_machine.
TaskStateMachine`, exactly as this task's own spec requires. This module
composes ONLY already-independently-tested boundaries: `coding.workspace`
(isolated worktree, independently re-verified), `coding.tool_loop` (bounded
Coder mutation), `coding.mutation_guard` (independent git-verified
mutation/diff evidence), `finalization.service.Finalizer` (real
verification + the existing bounded `REPAIRING <-> VERIFYING` loop,
unmodified), `coding.reviewer`/`coding.security_gate` (independent,
non-mutating roles). It never mutates a file itself, never runs a shell
command itself, and never merges, pushes, deploys, restarts a service, or
mutates live production configuration -- `READY_FOR_HUMAN_MERGE` is the
furthest state this module may ever reach.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.coding.contracts import (
    CoderContextEvidence,
    CoderInput,
    CoderPathScope,
    CoderPermissionsSnapshot,
    CoderPriorRepairEvidence,
    CoderRuntimeProfileIdentity,
    CoderTaskIdentity,
    CoderToolSchemaIdentity,
    CoderWorktreeIdentity,
)
from code_slayer.coding.handoff import validate_mutation_scope, validate_planner_handoff
from code_slayer.coding.mutation_guard import (
    DiffUnavailableError,
    compute_diff_text,
    verify_authorized_mutations,
)
from code_slayer.coding.pipeline_types import (
    CodingJobState,
    PipelineContractError,
    ReviewResult,
    ReviewVerdict,
    SecurityResult,
    SecurityVerdict,
    diff_fingerprint,
    verify_candidate_identity,
)
from code_slayer.coding.reviewer import ReviewerTurnError, run_reviewer_turn
from code_slayer.coding.security_gate import SecurityTurnError, run_security_turn
from code_slayer.coding.tool_loop import CODER_TOOLS, ToolLoopBounds, run_coder_turn
from code_slayer.coding.workspace import WorkspacePreflightError, prepare_coder_workspace
from code_slayer.core import TaskState, TaskStateMachine
from code_slayer.finalization.lifecycle import (
    CheckpointAdvanceOutcome,
    CompletionAdvanceOutcome,
    advance_checkpointed_completion,
    advance_ready_for_checkpoint,
)
from code_slayer.finalization.service import Finalizer
from code_slayer.finalization.types import FinalizerVerdict, ReviewEvidence
from code_slayer.lease.manager import LeaseManager
from code_slayer.policy.engine import Decision
from code_slayer.repo import identity as repo_identity
from code_slayer.repo import job_worktree_git as jwg
from code_slayer.repo.baseline import InspectionService
from code_slayer.repo.job_worktree import JobWorktreeError
from code_slayer.store import db as db_module
from code_slayer.store.coding_jobs_repo import CodingJobsRepo
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.models import EngineeringPlanRow
from code_slayer.store.task_repo import TaskRepo
from code_slayer.tools import file_tools as files
from code_slayer.workers.protocol import WorkerAdapter

DEFAULT_MAX_REPAIR_ATTEMPTS = 2


@dataclass(frozen=True)
class CodingJobConfig:
    """Every bound/identity a coding job is configured with -- code-owned
    and explicit. `max_repair_attempts` defaults to 2 (this task's own
    suggested default) -- passed straight through to `finalization.
    service.Finalizer`'s own, already-existing, already-tested bounded
    repair mechanism (`DEFAULT_MAX_REPAIR_ATTEMPTS`), never a second,
    separate budget invented here."""

    worker_id: str = "autonomous-engineering-loop"
    model_tag: str = "unspecified"
    runtime_version: str = "autonomous-loop-v1"
    protected_paths: tuple[str, ...] = ()
    max_repair_attempts: int = DEFAULT_MAX_REPAIR_ATTEMPTS
    coder_bounds: ToolLoopBounds = field(default_factory=ToolLoopBounds)
    base_revision: str | None = None
    state_root_override: str | Path | None = None


@dataclass(frozen=True)
class CodingJobResult:
    """The complete, structured outcome of one coding job -- never a
    model's own summary standing in for it. `final_state` is the
    code-owned `CodingJobState`; `READY_FOR_HUMAN_MERGE` is the furthest
    value it may ever hold."""

    job_id: str
    final_state: CodingJobState
    reason: str
    task_id: str | None = None
    job_worktree_path: str | None = None
    review: ReviewResult | None = None
    security: SecurityResult | None = None
    repair_attempts: int = 0


def _job_event(conn: sqlite3.Connection, job_id: str, event_type: EventType, payload: dict) -> None:
    """`conn` here is always `control_conn` -- the coding job's identity
    row lives there, but the real `tasks` row (which `audit_events.
    task_id` has a real foreign key to) lives in the execution-plane
    connection instead, a DIFFERENT SQLite database file. Writing
    `task_id=job_id` against `control_conn` would violate that foreign
    key (no such `tasks` row exists there) -- mirrors `runner.
    local_worker_runner.LocalWorkerRunner._audit()`'s own identical
    `task_id=None`, `job_id` folded into the payload instead, for exactly
    the same reason."""
    AuditWriter(conn).append(
        task_id=None, event_type=event_type, actor_type="system",
        actor_id="coding-pipeline", payload={"job_id": job_id, **payload},
    )


def _set_state(
    control_conn: sqlite3.Connection, job_id: str, state: CodingJobState, *, reason: str = "",
    **fields,
) -> None:
    """Durably write `state` (and, when `reason` is given, `final_reason`
    -- previously silently dropped, never actually persisted) through
    `CodingJobsRepo`'s own closed-set field validation, never a raw SQL
    write of our own. Also records `CODING_JOB_TERMINATED`, a single
    generic job-level audit event, whenever a real `reason` is supplied --
    every current call site that passes one is a terminal-or-blocking
    outcome, so this closes the "critical evidence is not only held in
    process memory" gap uniformly, rather than requiring every call site
    to separately remember to audit itself. A call with no `reason`
    (ordinary in-flight progress) writes no event and never overwrites a
    previously-recorded `final_reason` with an empty string."""
    if reason:
        fields = {**fields, "final_reason": reason}
    with transaction(control_conn):
        CodingJobsRepo(control_conn).update_in_transaction(
            job_id, updated_at=utcnow_iso(), state=state.value, **fields,
        )
        if reason:
            AuditWriter(control_conn).append(
                task_id=None, event_type=EventType.CODING_JOB_TERMINATED, actor_type="system",
                actor_id="coding-pipeline",
                payload={"job_id": job_id, "state": state.value, "reason": reason},
            )


def run_coding_job(
    primary_repo_path: Path | str, *,
    control_conn: sqlite3.Connection, control_blobs_dir: Path | str,
    plan: EngineeringPlanRow, original_prompt: str, allowed_scope: tuple[str, ...],
    coder_adapter: WorkerAdapter, reviewer_adapter: WorkerAdapter,
    security_adapter: WorkerAdapter, config: CodingJobConfig | None = None,
) -> CodingJobResult:
    """Run one complete coding job end to end, as far as it can safely
    and honestly go, up to `CodingJobState.READY_FOR_HUMAN_MERGE`.

    `control_conn`/`control_blobs_dir` hold this job's own durable
    identity row (`coding_jobs`) and the Planner plan it is validated
    against -- the control plane, exactly like `runner.
    local_worker_runner.LocalWorkerRunner`'s own split. A fresh,
    dedicated connection to the isolated job worktree's own database
    (the execution plane -- tasks/tool_operations/leases/checkpoints) is
    opened once workspace preflight succeeds, and is never the same
    connection as `control_conn`."""
    config = config if config is not None else CodingJobConfig()
    primary = repo_identity.resolve(primary_repo_path)
    job_id = uuid.uuid4().hex
    now = utcnow_iso()
    original_prompt_hash = files.digest(original_prompt.encode("utf-8"))

    try:
        validated_plan = validate_planner_handoff(control_conn, control_blobs_dir, plan)
        validate_mutation_scope(validated_plan.content, allowed_scope)
    except PipelineContractError as exc:
        # No coding_jobs row is created at all for an insufficient handoff
        # -- nothing durable or irreversible was ever attempted, matching
        # `runner.local_worker_runner.LocalWorkerRunner.start()`'s own
        # posture for a `PromptAnalystError` before any task exists.
        return CodingJobResult(job_id="", final_state=CodingJobState.FAILED, reason=str(exc))

    # Resolve the frozen base revision BEFORE the job's identity row is
    # created: `coding_jobs.base_revision` is an immutable identity field
    # (`coding_jobs_no_mutate_identity`), so it must be correct from the
    # start -- never a placeholder like the string "HEAD" written now and
    # "corrected" once the workspace exists.
    try:
        resolved_base_revision = jwg.resolve_commit(
            config.base_revision or "HEAD", cwd=primary.repo_root,
        )
    except jwg.JobWorktreeGitError as exc:
        return CodingJobResult(
            job_id="", final_state=CodingJobState.FAILED,
            reason=f"base_revision_unresolvable:{exc}",
        )

    with transaction(control_conn):
        CodingJobsRepo(control_conn).create_in_transaction(
            job_id=job_id, plan_id=plan.plan_id, repo_id=primary.repo_id,
            primary_worktree_id=primary.worktree_id, created_at=now,
            original_prompt_hash=original_prompt_hash, base_revision=resolved_base_revision,
            max_repair_attempts=config.max_repair_attempts, state=CodingJobState.CREATED.value,
        )
        _job_event(control_conn, job_id, EventType.CODING_JOB_CREATED, {
            "plan_id": plan.plan_id, "kind": "coding_job",
        })

    _set_state(control_conn, job_id, CodingJobState.PREFLIGHT)
    try:
        handle = prepare_coder_workspace(
            primary_repo_path, base_revision=resolved_base_revision,
            state_root_override=config.state_root_override,
        )
    except (WorkspacePreflightError, JobWorktreeError, jwg.JobWorktreeGitError) as exc:
        _set_state(
            control_conn, job_id, CodingJobState.BLOCKED,
            reason=f"workspace_preflight_failed:{exc}", finished_at=utcnow_iso(),
        )
        return CodingJobResult(job_id, CodingJobState.BLOCKED, f"workspace_preflight_failed:{exc}")

    _set_state(
        control_conn, job_id, CodingJobState.WORKSPACE_READY,
        execution_worktree_id=handle.worktree_id, job_worktree_path=str(handle.path),
    )

    exec_conn = db_module.connect(handle.db_path)
    db_module.migrate(exec_conn)
    lease = None
    try:
        task = TaskRepo(exec_conn).create(
            description=f"coding job {job_id}", repo_root=str(handle.path),
            repo_id=handle.repo_id, worktree_id=handle.worktree_id, task_id=job_id,
            config={
                "tool_policy": {"scope": list(allowed_scope)},
                "execution_kind": "coding_job",
            },
        )
        InspectionService(exec_conn, blobs_dir=handle.blobs_dir).start(task.task_id)
        InspectionService(exec_conn, blobs_dir=handle.blobs_dir).capture(task.task_id)
        machine = TaskStateMachine(exec_conn)
        for to_state, expected in (
            (TaskState.PLANNING, TaskState.BASELINED),
            (TaskState.PLANNED, TaskState.PLANNING),
            (TaskState.IMPLEMENTING, TaskState.PLANNED),
        ):
            machine.transition(
                task.task_id, expected_state=expected, to_state=to_state,
                reason="coding_pipeline_bootstrap",
            )
        _set_state(control_conn, job_id, CodingJobState.RUNNING, task_id=job_id)

        lease_result = LeaseManager(exec_conn).acquire(
            worktree_id=handle.worktree_id, task_id=job_id, worker_id=config.worker_id,
            worker_session_id=uuid.uuid4().hex,
        )
        if lease_result.decision != Decision.ALLOW:
            _set_state(
                control_conn, job_id, CodingJobState.BLOCKED,
                reason=f"lease_unavailable:{lease_result.reason}", finished_at=utcnow_iso(),
            )
            return CodingJobResult(
                job_id, CodingJobState.BLOCKED, f"lease_unavailable:{lease_result.reason}",
                task_id=job_id, job_worktree_path=str(handle.path),
            )
        lease = lease_result.handle

        coder_input = CoderInput(
            task=CoderTaskIdentity(
                task_id=job_id, repo_id=handle.repo_id, worktree_id=handle.worktree_id,
                original_prompt=original_prompt,
            ),
            validated_plan=validated_plan,
            paths=CoderPathScope(
                allowed_scope=allowed_scope, protected_paths=config.protected_paths,
            ),
            context=CoderContextEvidence(),
            worktree=CoderWorktreeIdentity(
                repo_id=handle.repo_id, worktree_id=handle.worktree_id,
                repo_root=str(handle.path), baseline_head=handle.base_revision,
            ),
            permissions=CoderPermissionsSnapshot(),
            tool_schema=CoderToolSchemaIdentity(
                schema_version="coder-tool-schema-v1", allowed_tools=CODER_TOOLS,
            ),
            runtime_profile=CoderRuntimeProfileIdentity(
                role="coder", model_tag=config.model_tag, runtime_version=config.runtime_version,
            ),
        )

        outcome = run_coder_turn(
            exec_conn, coder_adapter, task_id=job_id, coder_input=coder_input, lease=lease,
            blobs_dir=handle.blobs_dir, bounds=config.coder_bounds,
        )
        failure_category = (
            outcome.failure_category.value if outcome.failure_category is not None else None
        )
        _job_event(control_conn, job_id, EventType.WORKER_TURN_FINISHED, {
            "role": "coder", "ok": outcome.ok, "reason": outcome.reason,
            "failure_category": failure_category, "attempts": len(outcome.attempts),
        })
        if not outcome.ok:
            with transaction(exec_conn):
                machine.transition_in_transaction(
                    job_id, request=_failed_request(), actor_id="coding-pipeline",
                )
                LeaseManager(exec_conn).release_in_transaction(lease)
            lease = None
            _set_state(
                control_conn, job_id, CodingJobState.FAILED, reason=outcome.reason,
                finished_at=utcnow_iso(),
            )
            return CodingJobResult(
                job_id, CodingJobState.FAILED, outcome.reason, task_id=job_id,
                job_worktree_path=str(handle.path),
            )

        mutation_audit = verify_authorized_mutations(
            exec_conn, handle.path, task_id=job_id, base_revision=handle.base_revision,
            tmp_dir=handle.tmp_dir,
        )
        if not mutation_audit.ok:
            _job_event(control_conn, job_id, EventType.CODING_UNAUTHORIZED_MUTATION_DETECTED, {
                "unauthorized_paths": list(mutation_audit.unauthorized_paths),
            })
            with transaction(exec_conn):
                machine.transition_in_transaction(
                    job_id, request=_failed_request(), actor_id="coding-pipeline",
                )
                LeaseManager(exec_conn).release_in_transaction(lease)
            lease = None
            reason = f"unauthorized_mutation:{list(mutation_audit.unauthorized_paths)}"
            _set_state(
                control_conn, job_id, CodingJobState.BLOCKED, reason=reason,
                finished_at=utcnow_iso(),
            )
            return CodingJobResult(
                job_id, CodingJobState.BLOCKED, reason, task_id=job_id,
                job_worktree_path=str(handle.path),
            )

        _set_state(control_conn, job_id, CodingJobState.VALIDATING)
        machine.transition(
            job_id, expected_state=TaskState.IMPLEMENTING, to_state=TaskState.VERIFYING,
            reason="coder_turn_completed",
        )

        finalizer = Finalizer(exec_conn, blobs_dir=handle.blobs_dir, tmp_dir=handle.tmp_dir)
        review: ReviewResult | None = None
        repair_attempts = 0
        for _ in range(config.max_repair_attempts + 1):
            LeaseManager(exec_conn).renew(lease)
            try:
                diff_text = compute_diff_text(
                    handle.path, base_revision=handle.base_revision, tmp_dir=handle.tmp_dir,
                )
            except DiffUnavailableError as exc:
                _set_state(
                    control_conn, job_id, CodingJobState.BLOCKED,
                    reason=f"diff_unavailable:{exc}", finished_at=utcnow_iso(),
                )
                LeaseManager(exec_conn).release(lease)
                lease = None
                return CodingJobResult(
                    job_id, CodingJobState.BLOCKED, f"diff_unavailable:{exc}", task_id=job_id,
                    job_worktree_path=str(handle.path), repair_attempts=repair_attempts,
                )

            _job_event(control_conn, job_id, EventType.REVIEW_STARTED, {"attempt": repair_attempts})
            try:
                review = run_reviewer_turn(
                    reviewer_adapter, task_id=job_id, original_prompt=original_prompt,
                    plan_goal=validated_plan.content.goal, diff_text=diff_text,
                )
            except ReviewerTurnError as exc:
                review = ReviewResult(
                    verdict=ReviewVerdict.BLOCKED, summary=str(exc), findings=(),
                    diff_fingerprint=diff_fingerprint(diff_text),
                )
            _job_event(control_conn, job_id, EventType.REVIEW_FINDING, {
                "verdict": review.verdict.value, "summary": review.summary,
                "findings": len(review.findings), "diff_fingerprint": review.diff_fingerprint,
            })
            review_evidence = ReviewEvidence(
                approved=review.verdict == ReviewVerdict.PASS,
                reason=review.summary or review.verdict.value, blocking=True,
            )
            decision = finalizer.decide_after_verification(
                job_id, lease, max_repair_attempts=config.max_repair_attempts,
                review=review_evidence,
            )
            if decision.verdict == FinalizerVerdict.REPAIR_REQUIRED:
                repair_attempts += 1
                _job_event(control_conn, job_id, EventType.REPAIR_STARTED, {
                    "attempt": repair_attempts, "reason_code": decision.reason_code,
                })
                _set_state(
                    control_conn, job_id, CodingJobState.IN_REPAIR,
                    repair_attempts=repair_attempts,
                )
                repair_input = CoderInput(
                    task=coder_input.task, validated_plan=coder_input.validated_plan,
                    paths=coder_input.paths, context=coder_input.context,
                    worktree=coder_input.worktree, permissions=coder_input.permissions,
                    tool_schema=coder_input.tool_schema,
                    runtime_profile=coder_input.runtime_profile,
                    prior_repair=CoderPriorRepairEvidence(
                        attempt_number=repair_attempts, reason_code=decision.reason_code,
                        detail=f"{decision.detail} | review: {review.summary}",
                    ),
                )
                LeaseManager(exec_conn).renew(lease)
                repair_outcome = run_coder_turn(
                    exec_conn, coder_adapter, task_id=job_id, coder_input=repair_input,
                    lease=lease, blobs_dir=handle.blobs_dir, bounds=config.coder_bounds,
                )
                _job_event(control_conn, job_id, EventType.REPAIR_FINISHED, {
                    "attempt": repair_attempts, "ok": repair_outcome.ok,
                    "reason": repair_outcome.reason,
                })
                if not repair_outcome.ok:
                    with transaction(exec_conn):
                        machine.transition_in_transaction(
                            job_id, request=_failed_request(from_state=TaskState.REPAIRING),
                            actor_id="coding-pipeline",
                        )
                        LeaseManager(exec_conn).release_in_transaction(lease)
                    lease = None
                    _set_state(
                        control_conn, job_id, CodingJobState.FAILED, reason=repair_outcome.reason,
                        finished_at=utcnow_iso(), repair_attempts=repair_attempts,
                    )
                    return CodingJobResult(
                        job_id, CodingJobState.FAILED, repair_outcome.reason, task_id=job_id,
                        job_worktree_path=str(handle.path), repair_attempts=repair_attempts,
                    )
                repair_mutation_audit = verify_authorized_mutations(
                    exec_conn, handle.path, task_id=job_id, base_revision=handle.base_revision,
                    tmp_dir=handle.tmp_dir,
                )
                if not repair_mutation_audit.ok:
                    _job_event(
                        control_conn, job_id, EventType.CODING_UNAUTHORIZED_MUTATION_DETECTED,
                        {"unauthorized_paths": list(repair_mutation_audit.unauthorized_paths)},
                    )
                    with transaction(exec_conn):
                        machine.transition_in_transaction(
                            job_id, request=_failed_request(from_state=TaskState.REPAIRING),
                            actor_id="coding-pipeline",
                        )
                        LeaseManager(exec_conn).release_in_transaction(lease)
                    lease = None
                    unauthorized = list(repair_mutation_audit.unauthorized_paths)
                    reason = f"unauthorized_mutation:{unauthorized}"
                    _set_state(
                        control_conn, job_id, CodingJobState.BLOCKED, reason=reason,
                        finished_at=utcnow_iso(), repair_attempts=repair_attempts,
                    )
                    return CodingJobResult(
                        job_id, CodingJobState.BLOCKED, reason, task_id=job_id,
                        job_worktree_path=str(handle.path), repair_attempts=repair_attempts,
                    )
                machine.transition(
                    job_id, expected_state=TaskState.REPAIRING, to_state=TaskState.VERIFYING,
                    reason="repair_turn_completed",
                )
                continue  # loop back: re-diff, re-review, re-finalize
            break  # not REPAIR_REQUIRED -- decided (VERIFIED/BLOCKED/INVALID_ENVIRONMENT)

        review_verdict_value = review.verdict.value if review else None
        if decision.verdict != FinalizerVerdict.VERIFIED:
            final_job_state = (
                CodingJobState.HUMAN_REQUIRED if decision.reason_code == "repair_attempts_exhausted"
                else CodingJobState.BLOCKED
            )
            LeaseManager(exec_conn).release(lease)
            lease = None
            review_evidence_ref = review.diff_fingerprint if review is not None else None
            _set_state(
                control_conn, job_id, final_job_state, reason=decision.reason_code,
                finished_at=utcnow_iso(), repair_attempts=repair_attempts,
                review_verdict=review_verdict_value, review_evidence_ref=review_evidence_ref,
            )
            return CodingJobResult(
                job_id, final_job_state, decision.reason_code, task_id=job_id,
                job_worktree_path=str(handle.path), review=review, repair_attempts=repair_attempts,
            )

        # VERIFIED with a real, approved review -> REVIEWING; drive the
        # one remaining legal edge this pipeline (not Finalizer) owns.
        machine.transition(
            job_id, expected_state=TaskState.REVIEWING, to_state=TaskState.READY_FOR_CHECKPOINT,
            reason="reviewer_approved",
        )
        _set_state(
            control_conn, job_id, CodingJobState.SECURITY_REVIEW, repair_attempts=repair_attempts,
            review_verdict=review_verdict_value, review_evidence_ref=review.diff_fingerprint,
        )

        final_diff = compute_diff_text(
            handle.path, base_revision=handle.base_revision, tmp_dir=handle.tmp_dir,
        )
        offered = len(coder_input.tool_schema.allowed_tools)
        command_summary = f"{offered} tools offered; see audit log"
        verification_summary = (
            f"finalizer_verdict={decision.verdict.value} reason={decision.reason_code}"
        )
        try:
            security = run_security_turn(
                security_adapter, task_id=job_id, original_prompt=original_prompt,
                diff_text=final_diff, command_summary=command_summary,
                verification_summary=verification_summary,
            )
        except SecurityTurnError as exc:
            security = SecurityResult(
                verdict=SecurityVerdict.HUMAN_REQUIRED, summary=str(exc), findings=(),
                diff_fingerprint=diff_fingerprint(final_diff),
            )
        _job_event(control_conn, job_id, EventType.CODING_SECURITY_REVIEW_DECIDED, {
            "verdict": security.verdict.value, "summary": security.summary,
            "findings": len(security.findings), "diff_fingerprint": security.diff_fingerprint,
        })

        if security.verdict != SecurityVerdict.PASS:
            final_job_state = (
                CodingJobState.HUMAN_REQUIRED if security.verdict == SecurityVerdict.HUMAN_REQUIRED
                else CodingJobState.BLOCKED
            )
            LeaseManager(exec_conn).release(lease)
            lease = None
            _set_state(
                control_conn, job_id, final_job_state, reason=f"security:{security.verdict.value}",
                finished_at=utcnow_iso(), security_verdict=security.verdict.value,
                security_evidence_ref=security.diff_fingerprint,
            )
            return CodingJobResult(
                job_id, final_job_state, f"security:{security.verdict.value}", task_id=job_id,
                job_worktree_path=str(handle.path), review=review, security=security,
                repair_attempts=repair_attempts,
            )

        # Explicit defense-in-depth candidate-identity check (Fix 3):
        # recompute the current candidate's diff fingerprint fresh, from
        # authoritative git state, immediately before this function will
        # ever authorize a checkpoint, and require it to exactly match
        # both the Reviewer-PASS and Security-PASS fingerprints. Never
        # trusted from either model's own claim -- both `review.
        # diff_fingerprint`/`security.diff_fingerprint` were already
        # computed by this package's own code, not parsed from a
        # response. See `coding.pipeline_types.verify_candidate_identity`.
        recheck_diff = compute_diff_text(
            handle.path, base_revision=handle.base_revision, tmp_dir=handle.tmp_dir,
        )
        current_fingerprint = diff_fingerprint(recheck_diff)
        identity_check = verify_candidate_identity(review, security, current_fingerprint)
        if not identity_check.ok:
            LeaseManager(exec_conn).release(lease)
            lease = None
            _set_state(
                control_conn, job_id, CodingJobState.BLOCKED, reason=identity_check.reason,
                finished_at=utcnow_iso(), security_verdict=security.verdict.value,
                security_evidence_ref=security.diff_fingerprint,
            )
            return CodingJobResult(
                job_id, CodingJobState.BLOCKED, identity_check.reason, task_id=job_id,
                job_worktree_path=str(handle.path), review=review, security=security,
                repair_attempts=repair_attempts,
            )

        # Fix 1: READY_FOR_HUMAN_MERGE requires the REAL, inspected outcome
        # of both the checkpoint and the completion advance -- never
        # asserted merely because Security passed. Every non-success
        # CheckpointAdvanceOutcome/CompletionAdvanceOutcome (DENIED,
        # FAILED, NOT_READY, STALE_LEASE, UNKNOWN, CONTAINED_EXCEPTION)
        # fails the job closed to BLOCKED instead, with the exact outcome
        # and reason preserved -- matching this same function's own
        # BLOCKED usage for every other environment/policy-shaped failure
        # (workspace preflight, lease unavailable, unauthorized mutation).
        LeaseManager(exec_conn).renew(lease)
        checkpoint_result = advance_ready_for_checkpoint(
            exec_conn, job_id, lease, blobs_dir=handle.blobs_dir, tmp_dir=handle.tmp_dir,
        )
        completion_result = None
        if checkpoint_result.outcome == CheckpointAdvanceOutcome.CREATED:
            completion_result = advance_checkpointed_completion(exec_conn, job_id, lease)
        LeaseManager(exec_conn).release(lease)
        lease = None

        checkpoint_ok = checkpoint_result.outcome == CheckpointAdvanceOutcome.CREATED
        completion_ok = (
            completion_result is not None
            and completion_result.outcome == CompletionAdvanceOutcome.COMPLETED
        )
        if not (checkpoint_ok and completion_ok):
            reason = (
                f"checkpoint_not_created:{checkpoint_result.outcome.value}:"
                f"{checkpoint_result.reason}"
                if not checkpoint_ok else
                f"completion_not_confirmed:{completion_result.outcome.value}:"
                f"{completion_result.reason}"
            )
            _set_state(
                control_conn, job_id, CodingJobState.BLOCKED, reason=reason,
                finished_at=utcnow_iso(), security_verdict=security.verdict.value,
                security_evidence_ref=security.diff_fingerprint,
            )
            return CodingJobResult(
                job_id, CodingJobState.BLOCKED, reason, task_id=job_id,
                job_worktree_path=str(handle.path), review=review, security=security,
                repair_attempts=repair_attempts,
            )

        _set_state(
            control_conn, job_id, CodingJobState.READY_FOR_HUMAN_MERGE,
            reason="security_pass_checkpoint_and_completion_confirmed",
            finished_at=utcnow_iso(), security_verdict=security.verdict.value,
            security_evidence_ref=security.diff_fingerprint,
        )
        return CodingJobResult(
            job_id, CodingJobState.READY_FOR_HUMAN_MERGE,
            "security_pass_checkpoint_and_completion_confirmed",
            task_id=job_id, job_worktree_path=str(handle.path), review=review, security=security,
            repair_attempts=repair_attempts,
        )
    finally:
        if lease is not None:
            LeaseManager(exec_conn).release(lease)
        exec_conn.close()


def _failed_request(from_state: TaskState = TaskState.IMPLEMENTING):
    from code_slayer.core.transitions import TransitionRequest
    return TransitionRequest(
        expected_state=from_state, to_state=TaskState.FAILED,
        reason="coding_pipeline_failure", failure_decision=True,
    )
