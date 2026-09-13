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
