"""Independent, code-owned verification of what a Coder/Repairer turn
actually changed on disk -- never a model's own claim about which files
it touched.

Every mutation in this pipeline already goes exclusively through `tools.
executor.ToolExecutor` (checked per-call by `policy.engine.PolicyEngine`
against the task's declared scope before it is ever allowed to write) --
so an unauthorized-path write cannot happen through this package's own
Coder tool-loop in the first place. This module is the deliberate,
independent second check anyway: real Git plumbing (`repo.
job_worktree_git.worktree_status()`, the same function `repo.
job_worktree.release_job_worktree()` itself trusts as sole authority for
on-disk cleanliness) is compared against the task's own durable
`task_owned_paths` ledger -- catching anything that reached the working
tree WITHOUT a corresponding, policy-checked `ToolExecutor` operation
(e.g. a `run_command` invocation's own subprocess writing an unintended
file as a side effect), never trusted to be impossible merely because
the ordinary path should already prevent it.

A finding here is never silently repaired or cleaned up: the job fails
closed, and the exact unauthorized paths are themselves persisted
evidence (`code_slayer.audit.events.EventType.
CODING_UNAUTHORIZED_MUTATION_DETECTED`)."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from code_slayer.repo import job_worktree_git as jwg

_DIFF_TIMEOUT = 30.0
_MAX_DIFF_CHARS = 200_000
_GIT = shutil.which("git", path=os.defpath)


class DiffUnavailableError(RuntimeError):
    """The independent `git diff` used to show Reviewer/Security the
    actual change could not be produced -- fails closed (never a
    model-claimed substitute)."""


@dataclass(frozen=True)
class MutationAuditResult:
    """`changed_paths` is the complete, real-git-verified set of paths
    where the working tree now differs from the job's frozen base
    revision. `unauthorized_paths` is the subset NOT covered by any
    `task_owned_paths` row for this task -- non-empty here means the job
    must fail closed."""

    changed_paths: tuple[str, ...]
    owned_paths: tuple[str, ...]
    unauthorized_paths: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.unauthorized_paths


def verify_authorized_mutations(
    conn: sqlite3.Connection, worktree_path: Path, *,
    task_id: str, base_revision: str, tmp_dir: Path,
) -> MutationAuditResult:
    """Independently inspect `worktree_path`'s actual, on-disk state
    against `base_revision` (real Git plumbing, never a claim from the
    model or from `ToolExecutor`'s own bookkeeping alone) and cross-check
    every changed path against this task's durable `task_owned_paths`
    ledger. Raises `repo.job_worktree_git.JobWorktreeGitError` (fail
    closed, never a best-effort guess) if the Git-plumbing check itself
    could not run."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    index_path = tmp_dir / f"mutation-guard-{uuid.uuid4().hex}"
    try:
        changed = jwg.worktree_status(worktree_path, against=base_revision, index_path=index_path)
    finally:
        index_path.unlink(missing_ok=True)
    owned = tuple(sorted({
        row["path"] for row in conn.execute(
            "SELECT path FROM task_owned_paths WHERE task_id = ? AND deleted = 0", (task_id,),
        ).fetchall()
    }))
    owned_set = set(owned)
    unauthorized = tuple(path for path in changed if path not in owned_set)
    return MutationAuditResult(
        changed_paths=changed, owned_paths=owned, unauthorized_paths=unauthorized,
    )


def _diff_env(*, index_path: Path | None = None) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_ATTR_NOSYSTEM": "1", "LC_ALL": "C",
    })
    if index_path is not None:
        env["GIT_INDEX_FILE"] = str(index_path)
    return env


def _diff_run(argv: list[str], *, cwd: Path, index_path: Path | None = None):
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, shell=False, fixed binary path
            [_GIT, "--no-pager", "--no-optional-locks", *argv],
            cwd=str(cwd), capture_output=True, text=True, shell=False,
            env=_diff_env(index_path=index_path), timeout=_DIFF_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise DiffUnavailableError(f"git_{argv[0]}_timed_out") from exc


def compute_diff_text(
    worktree_path: Path, *, base_revision: str, tmp_dir: Path | None = None,
) -> str:
    """The real, independently-computed unified diff between
    `base_revision` and `worktree_path`'s actual current working-tree
    content -- what Reviewer/Security are shown, never a model's own
    description of "what I changed". Narrow, fixed-argv, no-shell
    subprocess boundary mirroring `repo.job_worktree_git`'s own
    hardening (no inherited `GIT_*`, no system/global config, no
    network, no hooks); this module owns its own instance of that
    boundary rather than importing `job_worktree_git`'s module-private
    helpers, matching this codebase's existing precedent of each module
    owning its own narrow git-subprocess boundary (`tools.command_tools`,
    `finalization.verification`).

    Uses a private, temporary index (`job_worktree_git.worktree_status()`'s
    own technique -- seeded from `base_revision` via `read-tree`, then
    `add -A` stages the actual current working-tree content into it,
    including UNTRACKED/new files, which a plain `git diff <rev>` would
    silently omit since untracked content never participates in that
    comparison at all). The job worktree's own real `.git/index` is never
    read from or written to. Bounded to `_MAX_DIFF_CHARS`, with a clear
    truncation marker, so an unusually large change can never make a
    review turn's own prompt unbounded."""
    if _GIT is None:
        raise DiffUnavailableError("git_executable_not_found")
    base_tmp = tmp_dir if tmp_dir is not None else worktree_path.parent
    base_tmp.mkdir(parents=True, exist_ok=True)
    index_path = base_tmp / f"diff-index-{uuid.uuid4().hex}"
    try:
        read = _diff_run(["read-tree", base_revision], cwd=worktree_path, index_path=index_path)
        if read.returncode != 0:
            raise DiffUnavailableError(f"git_read_tree_failed:{read.stderr.strip()[:200]}")
        added = _diff_run(["add", "-A"], cwd=worktree_path, index_path=index_path)
        if added.returncode != 0:
            raise DiffUnavailableError(f"git_add_failed:{added.stderr.strip()[:200]}")
        result = _diff_run(
            ["diff", "--cached", "--no-color", "--binary", base_revision, "--"],
            cwd=worktree_path, index_path=index_path,
        )
    finally:
        index_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise DiffUnavailableError(f"git_diff_failed:{result.stderr.strip()[:200]}")
    text = result.stdout
    if len(text) > _MAX_DIFF_CHARS:
        text = text[:_MAX_DIFF_CHARS] + "\n...(diff truncated)"
    return text
