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
