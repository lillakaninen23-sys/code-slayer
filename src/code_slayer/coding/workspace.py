"""Isolated Coder workspace: a thin wrapper around the already-existing,
hardened `repo.job_worktree` abstraction, plus an independent, from-
scratch re-verification of the exact base commit and cleanliness before
any model execution is ever authorized against it.

Per `AGENTS.md` and this task's own spec: "Inspect all relevant code
before modifying anything... Do not invent parallel architecture when
CSLR already has an abstraction that can be extended." `repo.
job_worktree.create_job_worktree()` already provides a Code-Slayer-owned,
disposable, detached-HEAD linked worktree, pinned to an explicit frozen
base revision, with conservative two-phase cleanup -- this module does
not reimplement any of that. It adds exactly one thing `job_worktree`
does not already do on its own: a SECOND, independent check -- real,
freshly-run Git plumbing, never trusting `create_job_worktree()`'s own
return value alone -- that the worktree that now exists on disk really
is at the exact commit requested and genuinely clean, before this
package's Coder tool-loop is ever allowed to run against it. This
mirrors this codebase's own established defense-in-depth convention
(e.g. `finalization.service.Finalizer._baseline_facts()` independently
re-reading and re-hashing content `tools.executor.ToolExecutor` already
wrote, rather than trusting its own prior bookkeeping alone)."""

from __future__ import annotations

import uuid
from pathlib import Path

from code_slayer.repo import job_worktree as job_worktree_module
from code_slayer.repo import job_worktree_git as jwg


class WorkspacePreflightError(RuntimeError):
    """The freshly created job worktree did not independently verify as
    clean and pinned to the requested base commit -- fails closed, never
    proceeds to authorize any model execution against it."""


def prepare_coder_workspace(
    primary_repo_path: Path | str, *, base_revision: str | None = None,
    state_root_override: str | Path | None = None,
) -> job_worktree_module.JobWorktree:
    """Create one isolated job worktree (`repo.job_worktree.
    create_job_worktree()`, unmodified) and independently re-verify it
    before returning -- raises `WorkspacePreflightError` (fail closed) on
    any check failure, on the theory that a Coder turn must never be
    allowed to start against a workspace this module has not itself
    freshly confirmed is exactly what it claims to be.

    Verification, in order: (1) `handle.base_revision` is a real,
    resolvable commit in the PRIMARY repository (guards against a caller
    ever forging one) and the job worktree's own on-disk `HEAD`
    (re-resolved fresh via `job_worktree_git.resolve_commit()`, never
    read from `handle` alone) is exactly that same commit; (2) the
    worktree has no staged changes and no working-tree drift against
    that commit (`job_worktree_git.staged_changes()`/`is_worktree_clean()`
    -- the same real-Git-plumbing checks `repo.job_worktree.
    release_job_worktree()` itself trusts, reused here at the opposite
    end of the workspace's lifecycle instead of reimplemented)."""
    handle = job_worktree_module.create_job_worktree(
        primary_repo_path, base_revision=base_revision, state_root_override=state_root_override,
    )
    try:
        primary_head = jwg.resolve_commit("HEAD", cwd=handle.primary_repo_root) if (
            base_revision is None
        ) else jwg.resolve_commit(base_revision, cwd=handle.primary_repo_root)
    except jwg.JobWorktreeGitError as exc:
        raise WorkspacePreflightError(f"base_revision_unresolvable_in_primary:{exc}") from exc
    if primary_head != handle.base_revision:
        raise WorkspacePreflightError(
            "base_revision_mismatch:primary_resolution_disagrees_with_handle",
        )
    try:
        worktree_head = jwg.resolve_commit("HEAD", cwd=handle.path)
    except jwg.JobWorktreeGitError as exc:
        raise WorkspacePreflightError(f"worktree_head_unresolvable:{exc}") from exc
    if worktree_head != handle.base_revision:
        raise WorkspacePreflightError("worktree_head_mismatch:not_pinned_to_base_revision")
    try:
        staged = jwg.staged_changes(handle.path)
    except jwg.JobWorktreeGitError as exc:
        raise WorkspacePreflightError(f"staged_status_unavailable:{exc}") from exc
    if staged:
        raise WorkspacePreflightError("worktree_not_clean:staged_changes_present")
    handle.tmp_dir.mkdir(parents=True, exist_ok=True)
    index_path = handle.tmp_dir / f"preflight-status-{uuid.uuid4().hex}"
    try:
        clean = jwg.is_worktree_clean(
            handle.path, against=handle.base_revision, index_path=index_path,
        )
        if not clean:
            raise WorkspacePreflightError("worktree_not_clean:working_tree_drift")
    except jwg.JobWorktreeGitError as exc:
        raise WorkspacePreflightError(f"worktree_status_unavailable:{exc}") from exc
    finally:
        index_path.unlink(missing_ok=True)
    return handle
