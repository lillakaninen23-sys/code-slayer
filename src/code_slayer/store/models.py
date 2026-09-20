"""Frozen dataclasses mirroring the rows Phase 1's repositories return.

These are read models over the database, not the source of truth — the
schema (`migrations/0001_init.sql`) is. Keeping them frozen means a caller
can never mistake a returned object for something it can mutate in place
and expect persisted.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    task_id: str
    description: str
    repo_root: str
    repo_id: str
    worktree_id: str
    created_at: str
    updated_at: str
    state: str
    current_phase: str | None
    config_json: str


@dataclass(frozen=True)
class ToolOperation:
    operation_id: str
    task_id: str
    worktree_id: str
    worker_id: str
    worker_session_id: str
    lease_generation: int | None
    tool_name: str
    risk_class: str
    request_hash: str
    target_resource: str
    child_pid: int | None
    child_pid_started_at: str | None
    started_at: str
    finished_at: str | None
    status: str
    before_evidence: str | None
    after_evidence: str | None
    result_json: str | None


@dataclass(frozen=True)
class ContentBlob:
    content_hash: str
    media_type: str
    source_kind: str
    byte_size: int
    truncated: bool
    exportable: bool
    created_at: str


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    task_id: str
    seq: int
    parent_checkpoint: str | None
    created_at: str
    phase: str
    status: str
    safe_to_resume: bool
    git_ref: str
    git_branch: str | None
    worker_id: str | None
    completed_json: str
    pending_json: str
    verified_json: str
    changed_files_json: str
    next_action: str | None


@dataclass(frozen=True)
class WorkerLease:
    worktree_id: str
    task_id: str
    worker_id: str
    worker_session_id: str
    generation: int
    acquired_at: str
    heartbeat_at: str
    status: str
    worker_pid: int | None
    worker_pid_started_at: str | None
    checkpoint_id: str | None


@dataclass(frozen=True)
class Worker:
    worker_id: str
    kind: str
    network_class: str
    capabilities_json: str
    availability_state: str
    available_after: str | None
    last_probe_at: str | None
    last_error: str | None
    # H.3: administrative lifecycle (schema v18) -- separate from
    # `availability_state` (runtime health/reachability). See
    # `code_slayer.store.workers_repo.WorkerLifecycleState`.
    lifecycle_state: str = "ACTIVE"
    lifecycle_changed_at: str | None = None


@dataclass(frozen=True)
class WorkerTrustEvent:
    """One durable trust transition (Phase 7.2). `capability` is `None`
    when the event is scoped to the whole `role` rather than one
    narrower capability within it."""

    id: int
    worker_id: str
    role: str
    capability: str | None
    from_level: str
    to_level: str
    reason: str
    evidence_ref: str | None
    occurred_at: str


@dataclass(frozen=True)
class WorkerConformanceRun:
    """One durable conformance suite execution (Phase 7.3). `status` is
    `RUNNING`/`PASSED`/`FAILED`; `completed_at` is `None` only while
    `RUNNING`. `suite_version` names the fixed, code-owned case list this
    run was checked against."""

    run_id: str
    worker_id: str
    role: str
    suite_version: str
    started_at: str
    completed_at: str | None
    status: str


@dataclass(frozen=True)
class WorkerConformanceResult:
    """One durable case result within exactly one run (Phase 7.3).
    `detail_content_hash` is reserved for a future, optional evidence
    blob (`content_blobs`) and is unpopulated by this phase's own
    runner."""

    id: int
    run_id: str
    case_name: str
    passed: bool
    reason: str
    detail_content_hash: str | None
    occurred_at: str


@dataclass(frozen=True)
class WorkerBaselineSecurityCertificate:
    """One durable Baseline Security Certificate evaluation result
    (Baseline Security Certification foundation —
    migrations/0011_baseline_security_certification.sql). A SEPARATE
    dimension from `WorkerTrustEvent` (execution authority) and
    `WorkerConformanceRun`/`WorkerConformanceResult` (capability
    conformance) — see `code_slayer.workers.security_baseline`'s module
    docstring.

    Append-only: a re-evaluation, or the same worker evaluated under a
    changed `model_tag`/`model_digest`/`endpoint`/`runtime_version`/
    compatibility-normalizer identity/`runtime_config_fingerprint`/
    `runtime_identity_fingerprint`, always creates a new row. The current
    certificate for a given `(worker_id, runtime profile)` binding is
    *derived* by finding the most recent row whose common runtime-identity
    fields match exactly — see `code_slayer.workers.production_eligibility`.

    `evidence_ref` is required on every row, `PASS`/`FAIL`/
    `HARD_DISQUALIFIED` alike — a certificate is never a bare boolean
    with no referenceable evidence. `hard_disqualifiers_json` is a JSON
    array of `code_slayer.workers.security_baseline.
    HardDisqualifierCategory` values, non-empty only when
    `outcome == "HARD_DISQUALIFIED"`.

    `promoted_from_validation_certificate_id` (schema v17) is `None` for
    every ordinary certificate — it is set ONLY by `code_slayer.
    security.production_promotion` when this row durably carries a
    VALIDATION certificate forward into PRODUCTION, to that VALIDATION
    row's own `certificate_id`. Never a client-supplied value; see that
    module's own docstring."""

    certificate_id: str
    worker_id: str
    baseline_version: str
    model_tag: str
    model_digest: str | None
    endpoint: str | None
    runtime_version: str | None
    outcome: str
    hard_disqualifiers_json: str
    evidence_ref: str
    reason: str
    issued_at: str
    normalizer_id: str | None = None
    normalizer_version: int | None = None
    runtime_config_fingerprint: str | None = None
    runtime_identity_fingerprint: str | None = None
    promoted_from_validation_certificate_id: str | None = None


@dataclass(frozen=True)
class WorkerRoleCertificate:
    """One durable role-qualification certification decision (Role
    Qualification Certification foundation —
    migrations/0012_role_qualification_certification.sql). A SEPARATE
    dimension from `WorkerTrustEvent` (execution authority),
    `WorkerConformanceRun`/`WorkerConformanceResult` (capability
    conformance), and `WorkerBaselineSecurityCertificate` (the
    mandatory, role-independent Security floor) — see
    `code_slayer.workers.role_qualification`'s module docstring.

    Append-only: a re-evaluation, a different role, or the same
    `(worker_id, role)` evaluated under a changed runtime profile,
    role/evaluation profile, or `policy_version`, always creates a new
    row. The current certificate for a given `(worker_id, role, common
    runtime identity, role/evaluation profile)` binding is *derived* by
    finding the most recent row whose identity fields match exactly —
    see `code_slayer.workers.production_eligibility`.

    `evidence_ref` is required on every row, `PASS`/`FAIL` alike — a
    certificate is never a bare boolean with no referenceable evidence.
    `classification` preserves the richer, role-specific evidence detail
    behind `outcome` (e.g. a Planner certification passes through
    `planning.qualification.QualificationOutcome`'s own value, such as
    `"PASS_FIRST_TRY"`/`"FAIL_POLICY"`)."""

    certificate_id: str
    worker_id: str
    role: str
    policy_version: str
    model_tag: str
    model_digest: str | None
    endpoint: str | None
    runtime_version: str | None
    outcome: str
    classification: str
    evidence_ref: str
    reason: str
    issued_at: str
    normalizer_id: str | None = None
    normalizer_version: int | None = None
    runtime_config_fingerprint: str | None = None
    runtime_identity_fingerprint: str | None = None
    role_evaluation_fingerprint: str | None = None


@dataclass(frozen=True)
class RunnerRun:
    """One durable application-level run record (Phase 7.7 —
    `runner.local_worker_runner.LocalWorkerRunner`). Always lives in the
    control database; `status` is an application-level `RunStatus`, never
    a `core.states.TaskState`."""

    run_id: str
    created_at: str
    updated_at: str
    repo_id: str
    primary_worktree_id: str
    original_prompt_hash: str
    worker_id: str
    role: str
    requires_mutation: bool
    status: str
    task_id: str | None
    execution_worktree_id: str | None
    job_worktree_path: str | None
    analysis_content_hash: str | None
    final_text_content_hash: str | None
    tool_operation_id: str | None
    questions_json: str | None
    reason: str | None


@dataclass(frozen=True)
class RunnerHumanResolution:
    """One durable, append-only human/application resolution answering
    exactly one blocked ambiguity (Phase 7.7)."""

    id: int
    run_id: str
    ambiguity_id: str
    source: str
    resolution_kind: str
    answer_content_hash: str
    created_at: str


@dataclass(frozen=True)
class PlanningJobRow:
    """One durable, mutable background-planning-job record (Phase 8.2d —
    `planning.service.EngineeringPlanningService`). A job's `state`
    (QUEUED/RUNNING/SUCCEEDED/FAILED) is a distinct concern from the
    `EngineeringPlanRow` it points at via `plan_id` — see
    `store.migrations.0009_planning_jobs`'s module comment. Identity
    fields locked at creation; ownership/outcome fields evolve as the
    job progresses.

    `worker_id` through `planner_policy_version` (schema v19, H.4) are
    the durable Planner route binding this job was created with — see
    `store.migrations.0019_planner_worker_routing`'s own module
    comment. All eight are `None` only for a job created before schema
    v19 (never rewritten to claim a binding it never had); every job
    created at or after v19 has all eight populated, enforced both by
    `store.planning_jobs_repo.PlanningJobsRepo.create_in_transaction()`
    requiring them as mandatory parameters and by that migration's own
    `planning_jobs_require_route_binding_on_insert` trigger. Locked at
    creation exactly like every other identity field above — see that
    migration's `planning_jobs_no_mutate_identity` trigger.

    `planner_timeout_seconds` (schema v20, H.4.1) is a NINTH route-
    binding field, added later — `None` for any job created before
    schema v20 (a v19 job otherwise fully bound; never a value this job
    was never actually queued with — see `store.migrations.
    0020_planner_timeout_binding`), mandatory and immutable for every
    job created at or after v20, same enforcement pattern as the
    original eight."""

    job_id: str
    plan_id: str
    repo_id: str
    worktree_id: str
    created_at: str
    updated_at: str
    kind: str
    state: str
    attempt: int
    owner_pid: int | None
    owner_pid_started_at: str | None
    owner_generation: int
    started_at: str | None
    finished_at: str | None
    failure_category: str | None
    failure_reason: str | None
    predecessor_job_id: str | None
    worker_id: str | None = None
    runtime_identity_fingerprint: str | None = None
    role_evaluation_fingerprint: str | None = None
    security_certificate_id: str | None = None
    role_certificate_id: str | None = None
    output_token_budget: int | None = None
    tool_choice_enforcement: str | None = None
    planner_policy_version: str | None = None
    planner_timeout_seconds: float | None = None


@dataclass(frozen=True)
class PermissionRequestRow:
    """One durable, immutable permission-request record (CSLR Governance
    Foundation, slice G2 — `permissions.service.PermissionService`).
    Created only by trusted backend code
    (`PermissionService.request()`) — never from an HTTP body, model
    output, or planner output. `permission_key`/`semantic_version` name
    an entry in the code-owned `permissions.definitions.
    PERMISSION_DEFINITIONS` registry; this row never carries the
    definition's own explanation metadata, only the identity of which
    definition was requested."""

    request_id: str
    created_at: str
    repo_id: str
    worktree_id: str
    permission_key: str
    semantic_version: str
    resource: str | None
    purpose: str
    requesting_subsystem: str


@dataclass(frozen=True)
class PermissionDecisionRow:
    """One durable, immutable decision on exactly one request — append-
    only, and unique per `request_id` (migration 0010's own DB
    constraint, not merely an application-level check)."""

    id: int
    request_id: str
    decision: str
    decided_at: str


@dataclass(frozen=True)
class PermissionGrantRow:
    """One durable, immutable grant, created only alongside an ALLOW
    decision. `authority_origin` is always `USER_EXPLICIT` in this
    phase — see `permissions.definitions.AuthorityOrigin`. Whether this
    grant is currently active is never stored here; it is derived by
    checking `permission_revocations` and `expiry` at read time
    (`permissions.service.PermissionService`)."""

    grant_id: str
    request_id: str
    permission_key: str
    semantic_version: str
    resource: str | None
    authority_origin: str
    granted_at: str
    expiry: str | None


@dataclass(frozen=True)
class PermissionRevocationRow:
    """One durable, immutable revocation of exactly one grant — append-
    only, and unique per `grant_id`."""

    id: int
    grant_id: str
    revoked_at: str


@dataclass(frozen=True)
class EngineeringPlanRow:
    """One durable, mutable engineering-plan revision record (Phase 8.2
    — `planning.service.EngineeringPlanningService`). Mirrors
    `RunnerRun`'s own shape: identity fields locked at creation,
    evolving orchestration fields (`state`, `reason`, repository binding,
    every content-hash pointer, `questions_json`) updatable in place.
    The actual planning content never lives in this row — see
    `plan_content_hash`."""

    plan_id: str
    created_at: str
    updated_at: str
    schema_version: str
    repo_id: str
    worktree_id: str
    run_id: str | None
    request_content_hash: str
    predecessor_plan_id: str | None
    revision: int
    state: str
    reason: str | None
    head_sha: str | None
    working_tree_dirty: bool
    working_tree_fingerprint: str | None
    intelligence_snapshot_id: str | None
    planner_input_content_hash: str | None
    planner_output_content_hash: str | None
    validation_content_hash: str | None
    plan_content_hash: str | None
    questions_json: str | None


@dataclass(frozen=True)
class EngineeringPlanHumanResolution:
    """One durable, append-only human/application resolution answering
    exactly one blocked planning ambiguity (Phase 8.2) — byte-for-byte
    the same shape as `RunnerHumanResolution`."""

    id: int
    plan_id: str
    ambiguity_id: str
    source: str
    resolution_kind: str
    answer_content_hash: str
    created_at: str


@dataclass(frozen=True)
class RepositoryIntelligenceSnapshotRow:
    """One durable, append-only repository-intelligence snapshot record
    (Phase 8.1) — small identity/pointer row only; the actual inventory/
    project/command/symbol/graph content lives content-addressed in
    `content_blobs`, referenced by `snapshot_content_hash`."""

    snapshot_id: str
    repo_id: str
    worktree_id: str
    head_sha: str | None
    working_tree_dirty: bool
    working_tree_fingerprint: str
    index_version: str
    created_at: str
    snapshot_content_hash: str
    file_count: int
    inventory_truncated: bool


@dataclass(frozen=True)
class CertificationRunRow:
    """One durable Certification Center job (schema v16). Distinct from
    a Baseline Security certificate: a run may finish INCOMPLETE with
    no certificate. Identity fields are immutable; lifecycle fields
    evolve until a terminal state."""

    run_id: str
    worker_id: str
    kind: str
    environment: str
    state: str
    reason: str | None
    preflight_json: str
    expected_runtime_identity_fingerprint: str | None
    model_tag: str | None
    model_digest: str | None
    ollama_root: str | None
    certificate_id: str | None
    evidence_ref: str | None
    hard_disqualifiers_json: str
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None
    attempt: int
    owner_pid: int | None
    owner_pid_started_at: str | None
    owner_generation: int


@dataclass(frozen=True)
class CodingJobRow:
    """One durable Autonomous Engineering Loop job record
    (`store.migrations.0021_coding_jobs`) -- `code_slayer.coding.
    jobs_repo.CodingJobsRepo`'s own row shape. Mirrors `PlanningJobRow`'s
    "identity locked at creation, lifecycle/outcome fields evolve" split;
    see that migration's module comment for why rich per-attempt evidence
    (review/security findings, tool calls, repair reasoning) lives in
    `audit_events` (addressable via `task_id`) rather than duplicated
    here. `state` is `code_slayer.coding.pipeline_types.CodingJobState`'s
    own string vocabulary."""

    job_id: str
    plan_id: str
    repo_id: str
    primary_worktree_id: str
    created_at: str
    updated_at: str
    original_prompt_hash: str
    base_revision: str
    max_repair_attempts: int
    state: str
    execution_worktree_id: str | None
    job_worktree_path: str | None
    task_id: str | None
    repair_attempts: int
    review_verdict: str | None
    review_evidence_ref: str | None
    security_verdict: str | None
    security_evidence_ref: str | None
    final_reason: str | None
    finished_at: str | None
