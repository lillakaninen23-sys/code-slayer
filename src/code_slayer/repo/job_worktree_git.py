"""Narrow Git plumbing for disposable job worktrees (Phase 7.5c).

Unlike `repo/git.py` (reads and one narrowly-scoped `git config --local`
write) and `repo/checkpoint_git.py` (blob/tree/commit/ref plumbing that
never touches a real branch, index, or working tree), this module
performs the one Git operation this phase needs that DOES create/remove
a real linked working tree: `git worktree add --detach` and
`git worktree remove`. It never touches the PRIMARY worktree's own HEAD,
index, or working-tree files — only the primary repository's shared
object/ref database (every linked worktree shares this) and the new
linked worktree's own private git-dir under it
(`.git/worktrees/<name>/`), exactly the same separation
`repo/identity.py` already relies on for `worktree_id`.

No shell, no string interpolation, no untrusted argv content: every
argument here is either a fixed literal, a path this module's own caller
produced (never a model-supplied path — see `job_worktree.py`'s module
docstring), or a commit sha `resolve_commit()` itself already resolved
via `git rev-parse` before any worktree is created from it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_TIMEOUT = 30.0


class JobWorktreeGitError(RuntimeError):
    """A worktree-lifecycle plumbing command failed or returned an
    unexpected shape."""


def _environment() -> dict[str, str]:
    # Mirrors repo/git.py's own hardening: no inherited GIT_* overrides,
    # no system/global config, no network, no hooks-relevant surprises.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_ATTR_NOSYSTEM": "1", "LC_ALL": "C",
    })
    return env


def _run(argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(  # noqa: S603 - argv list, shell=False, fixed binary name
            ["git", "--no-pager", "--no-optional-locks", *argv],
            cwd=str(cwd), capture_output=True, text=True, shell=False,
            env=_environment(), timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise JobWorktreeGitError(f"git {argv[0]} timed out") from exc


def resolve_commit(revision: str, *, cwd: Path) -> str:
    """Resolve `revision` (a branch, tag, or sha) to a concrete, frozen
    commit sha *right now*. The job worktree is pinned to this exact
    result, never re-resolved later, so a moving branch tip after this
    call never silently changes what the job is based on (§Phase 7.5c
    "Base revision")."""
    result = _run(["rev-parse", "--verify", "-q", f"{revision}^{{commit}}"], cwd=cwd)
    if result.returncode != 0:
        raise JobWorktreeGitError(f"cannot resolve base revision {revision!r}")
    return result.stdout.strip()


def add_worktree(path: Path, commit_sha: str, *, cwd: Path) -> None:
    """Create a new linked, DETACHED worktree at `path`, pinned to
    `commit_sha` — never a branch, so nothing can later move it by
    fast-forwarding or checking out some branch tip. Never creates or
    moves any of the user's own branches. Fails if `path` already exists:
    this module never overwrites or silently reuses a path."""
    result = _run(["worktree", "add", "--detach", str(path), commit_sha], cwd=cwd)
    if result.returncode != 0:
        raise JobWorktreeGitError(f"git worktree add failed: {result.stderr.strip()}")


def remove_worktree(path: Path, *, cwd: Path, force: bool = True) -> None:
    """Remove a linked worktree Code Slayer itself created.

    `force=True` by default: Git's own "is this worktree clean" heuristic
    does not understand Code Slayer's checkpoint model (a checkpoint is a
    commit object reachable only from a dedicated
    `refs/codeslayer/checkpoints/...` ref, never merged into the job
    worktree's own branch/HEAD, so Git always sees the working tree
    itself as dirty/untracked even once fully checkpointed). The actual
    safety decision belongs entirely to `job_worktree.release_job_worktree()`'s
    own lease/unresolved-operation/checkpoint checks, performed *before*
    this function is ever called — never to Git's own cleanliness
    heuristic, which this call deliberately bypasses.
    """
    argv = ["worktree", "remove"]
    if force:
        argv.append("--force")
    argv.append(str(path))
    result = _run(argv, cwd=cwd)
    if result.returncode != 0:
        raise JobWorktreeGitError(f"git worktree remove failed: {result.stderr.strip()}")


def list_worktrees(*, cwd: Path) -> tuple[str, ...]:
    """Every worktree path Git itself currently knows about for this
    repository (`git worktree list --porcelain`) — ground truth for
    whether a path this module generated is still a real linked worktree,
    independent of whatever Code Slayer's own sidecar metadata claims."""
    result = _run(["worktree", "list", "--porcelain"], cwd=cwd)
    if result.returncode != 0:
        raise JobWorktreeGitError("git worktree list failed")
    paths = []
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            paths.append(line[len("worktree "):].strip())
    return tuple(paths)
