"""The audit event vocabulary (Foundation Plan §07).

This enum names every event type the *full* design uses. Phase 1 only
ever emits a handful of these (TASK_CREATED, STATE_TRANSITION) — the rest
are declared now so later phases append to durable state through the same
typed vocabulary from day one, instead of inventing ad hoc string
constants per phase.
"""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    TASK_CREATED = "TASK_CREATED"
    STATE_TRANSITION = "STATE_TRANSITION"
    TOOL_REQUESTED = "TOOL_REQUESTED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    OPERATION_STARTED = "OPERATION_STARTED"
    OPERATION_FINISHED = "OPERATION_FINISHED"
    RULES_LOADED = "RULES_LOADED"
    REPO_INSPECTED = "REPO_INSPECTED"
    BASELINE_RECORDED = "BASELINE_RECORDED"
    PLAN_CREATED = "PLAN_CREATED"
    PLAN_ACCEPTED = "PLAN_ACCEPTED"
    WORKER_STARTED = "WORKER_STARTED"
    WORKER_HEARTBEAT = "WORKER_HEARTBEAT"
    FILE_READ = "FILE_READ"
    PATCH_APPLIED = "PATCH_APPLIED"
    PATCH_REJECTED = "PATCH_REJECTED"
    COMMAND_STARTED = "COMMAND_STARTED"
    COMMAND_FINISHED = "COMMAND_FINISHED"
    TEST_PASS = "TEST_PASS"
    TEST_FAIL = "TEST_FAIL"
    REPAIR_STARTED = "REPAIR_STARTED"
    REPAIR_FINISHED = "REPAIR_FINISHED"
    REVIEW_STARTED = "REVIEW_STARTED"
    REVIEW_FINDING = "REVIEW_FINDING"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    CHECKPOINT_VALIDATED = "CHECKPOINT_VALIDATED"
    WORKER_LIMIT_APPROACHING = "WORKER_LIMIT_APPROACHING"
    WORKER_LIMITED = "WORKER_LIMITED"
    WORKER_HANDOFF_ISSUED = "WORKER_HANDOFF_ISSUED"
    WORKER_HANDOFF_ACCEPTED = "WORKER_HANDOFF_ACCEPTED"
    WORKER_RESUMED = "WORKER_RESUMED"
    WORKER_TRUST_CHANGED = "WORKER_TRUST_CHANGED"
    WORKER_CONFORMANCE_RUN_STARTED = "WORKER_CONFORMANCE_RUN_STARTED"
    WORKER_CONFORMANCE_RUN_FINALIZED = "WORKER_CONFORMANCE_RUN_FINALIZED"
    # Phase 7.5a: attributes one bounded worker turn's protocol/trust
    # decisions to worker_id/role/capability -- concepts ToolExecutor's
    # own TOOL_REQUESTED/POLICY_EVALUATED/OPERATION_STARTED/
    # OPERATION_FINISHED audit trail (unmodified) has no notion of. See
    # `workers.execution`'s module docstring.
    WORKER_TOOL_CALL_EVALUATED = "WORKER_TOOL_CALL_EVALUATED"
    WORKER_TURN_FINISHED = "WORKER_TURN_FINISHED"
    # Phase 7.7e: the pre-transport cloud-escalation authorization
    # decision (`workers.cloud_escalation.check_cloud_escalation()`),
    # recorded for every bounded turn -- local (no escalation needed) and
    # cloud (authorized or denied) alike -- via `workers.execution.
    # execute_guarded_turn()`, always BEFORE any `adapter.infer()` call.
    # Distinct from CLOUD_ESCALATION_PACKAGE_BUILT below, which is a
    # later, still-unimplemented concept (an actual filtered/redacted
    # escalation bundle) this event does not attempt to anticipate.
    CLOUD_ESCALATION_EVALUATED = "CLOUD_ESCALATION_EVALUATED"
    # Phase 7.5c: an isolated, disposable job worktree's own lifecycle
    # (`repo.job_worktree`) -- not scoped to any one task_id (a job
    # worktree may exist before any task is created against it), so
    # these are recorded with `task_id=None`, same as any other
    # system-level event with no task to attribute it to yet.
    JOB_WORKTREE_CREATED = "JOB_WORKTREE_CREATED"
    JOB_WORKTREE_RELEASED = "JOB_WORKTREE_RELEASED"
    # Phase 7.7b: cleanup-authority claim/abort/failure bookkeeping
    # (`repo.job_worktree.release_job_worktree()`) -- the minimal new
    # events needed to audit the two-phase cleanup protocol's own
    # decisions; JOB_WORKTREE_RELEASED above still covers the terminal
    # success case, so no parallel "released" event is added here.
    JOB_WORKTREE_CLEANUP_CLAIMED = "JOB_WORKTREE_CLEANUP_CLAIMED"
    JOB_WORKTREE_CLEANUP_ABORTED = "JOB_WORKTREE_CLEANUP_ABORTED"
    JOB_WORKTREE_CLEANUP_REMOVAL_FAILED = "JOB_WORKTREE_CLEANUP_REMOVAL_FAILED"
    # Phase 7.6: durable provenance for one Prompt Analyst / Question
    # Gate decision (`workers.prompt_provenance`) -- the small,
    # structured facts a later query needs (content hashes, the gate's
    # decision, which ambiguities were asked/suppressed and why), never
    # the raw prompt/analysis text itself, which lives in `content_blobs`
    # instead. `task_id` may be `None`: a prompt can be analyzed before a
    # task formally exists.
    PROMPT_ANALYSIS_RECORDED = "PROMPT_ANALYSIS_RECORDED"
    QUESTION_GATE_DECISION = "QUESTION_GATE_DECISION"
    # Phase 7.7: the application-level run record's own lifecycle
    # (`runner.local_worker_runner.LocalWorkerRunner`) -- always
    # recorded in the control-plane database, `task_id` set only once an
    # execution-plane task exists for this run. RUN_FINISHED covers every
    # terminal outcome (COMPLETED/DENIED_TRUST/FAILED/
    # INTERRUPTED_RESUMABLE) via its own `status` payload field, rather
    # than one EventType member per outcome.
    RUN_STARTED = "RUN_STARTED"
    RUN_BLOCKED = "RUN_BLOCKED"
    RUN_USER_RESOLUTION_RECORDED = "RUN_USER_RESOLUTION_RECORDED"
    RUN_RESUMED = "RUN_RESUMED"
    RUN_FINISHED = "RUN_FINISHED"
    LEASE_ACQUIRED = "LEASE_ACQUIRED"
    LEASE_RENEWED = "LEASE_RENEWED"
    LEASE_QUIESCING = "LEASE_QUIESCING"
    LEASE_EXPIRED = "LEASE_EXPIRED"
    LEASE_RELEASED = "LEASE_RELEASED"
    FENCE_STALE_REJECTED = "FENCE_STALE_REJECTED"
    ORPHAN_PROCESS_DETECTED = "ORPHAN_PROCESS_DETECTED"
    ORPHAN_PROCESS_TERMINATED = "ORPHAN_PROCESS_TERMINATED"
    ORPHAN_PROCESS_WAITED = "ORPHAN_PROCESS_WAITED"
    RECONCILIATION_STARTED = "RECONCILIATION_STARTED"
    RECONCILIATION_FINDING = "RECONCILIATION_FINDING"
    EXTERNAL_MODIFICATION_DETECTED = "EXTERNAL_MODIFICATION_DETECTED"
    POLICY_DENIED = "POLICY_DENIED"
    POLICY_APPROVAL_REQUIRED = "POLICY_APPROVAL_REQUIRED"
    CLOUD_ESCALATION_PACKAGE_BUILT = "CLOUD_ESCALATION_PACKAGE_BUILT"
    TASK_BLOCKED = "TASK_BLOCKED"
    TASK_FAILED = "TASK_FAILED"
    TASK_COMPLETED = "TASK_COMPLETED"
    # Phase 8.2: the durable engineering-plan record's own lifecycle
    # (`planning.service.EngineeringPlanningService`) -- planning only,
    # never a mutation/execution event. `PLAN_FINISHED` covers every
    # terminal-for-this-attempt outcome (READY, or DRAFT with a recorded
    # evidence-validation failure) via its own `state`/`reason` payload
    # fields, mirroring `RUN_FINISHED`'s own single-event convention.
    PLAN_STARTED = "PLAN_STARTED"
    PLAN_BLOCKED = "PLAN_BLOCKED"
    PLAN_USER_RESOLUTION_RECORDED = "PLAN_USER_RESOLUTION_RECORDED"
    PLAN_RESUMED = "PLAN_RESUMED"
    PLAN_FINISHED = "PLAN_FINISHED"
    PLAN_SUPERSEDED = "PLAN_SUPERSEDED"
    # Phase 8.2d: a background planning job's own execution-lifecycle
    # events (`planning.service.EngineeringPlanningService`/`planning.
    # executor.PlanningJobExecutor`) -- distinct from PLAN_* above, which
    # record the plan CONTENT's own lifecycle; these record whether one
    # execution ATTEMPT ran, and by whom. Never a mutation/execution
    # event; never carries raw model output.
    PLANNING_JOB_ACCEPTED = "PLANNING_JOB_ACCEPTED"
    PLANNING_JOB_CLAIMED = "PLANNING_JOB_CLAIMED"
    PLANNING_JOB_FINISHED = "PLANNING_JOB_FINISHED"
    # CSLR Governance Foundation, slice G2: the Permission Engine's own
    # durable request/decision/revocation lifecycle
    # (`permissions.service.PermissionService`). Never carries secrets;
    # never carries raw model output. A successful `check()` is
    # deliberately never audited (it is side-effect free and would flood
    # the log for every harmless repeated check) -- only the
    # security-relevant outcomes below are.
    PERMISSION_REQUESTED = "PERMISSION_REQUESTED"
    PERMISSION_ALLOWED = "PERMISSION_ALLOWED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PERMISSION_REVOKED = "PERMISSION_REVOKED"
    PERMISSION_CHECK_DENIED = "PERMISSION_CHECK_DENIED"
