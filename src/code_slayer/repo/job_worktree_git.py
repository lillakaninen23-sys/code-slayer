"""Narrow Git plumbing for disposable job worktrees (Phase 7.5c/7.5c
cleanup-hardening follow-up).

Unlike `repo/git.py` (reads and one narrowly-scoped `git config --local`
write) and `repo/checkpoint_git.py` (blob/tree/commit/ref plumbing that
never touches a real branch, index, or working tree), this module
performs the Git operations this phase needs that DO create/remove a real
linked working tree, or read its actual on-disk state: `git worktree add
--detach`, `git worktree remove`, a real-tree-vs-real-working-tree
cleanliness check built the same way `checkpoint_git.build_tree()` builds
a tree — a private, temporary index file, never the job worktree's own
real `.git/index` — and (Phase 7.7b) `staged_changes()`, the one function
here that DOES deliberately read the job worktree's own real `.git/index`
(read-only, via `git diff --cached`) because a private, temporary index
can never see content someone actually staged there. None of this ever
touches the PRIMARY worktree's own HEAD, index, or working-tree files —
only the primary repository's shared object/ref database (every linked
worktree shares this) and the new linked worktree's own private git-dir
under it (`.git/worktrees/<name>/`), exactly the same separation
`repo/identity.py` already relies on for `worktree_id`.

No shell, no string interpolation, no untrusted argv content: every
argument here is either a fixed literal, a path this module's own caller
produced (never a model-supplied path — see `job_worktree.py`'s module
docstring), or a commit/tree-ish `resolve_commit()` itself already
resolved via `git rev-parse`, or already-durable evidence
(`job_worktree.release_job_worktree()`'s latest-checkpoint tree sha, or
the job worktree's own frozen `base_revision`) before this module is
ever asked to compare anything against it. No generic "run arbitrary
git" entry point is exposed — every function here does exactly one
fixed, narrow thing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_TIMEOUT = 30.0


class JobWorktreeGitError(RuntimeError):
    """A worktree-lifecycle plumbing command failed or returned an
    unexpected shape."""


def _environment(*, index_path: Path | None = None) -> dict[str, str]:
    # Mirrors repo/git.py's own hardening: no inherited GIT_* overrides,
    # no system/global config, no network, no hooks-relevant surprises.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_ATTR_NOSYSTEM": "1", "LC_ALL": "C",
    })
    if index_path is not None:
        # A private, temporary index (checkpoint_git.build_tree()'s own
        # pattern): the job worktree's real `.git/index` is never read
        # from or written to by a cleanliness check.
        env["GIT_INDEX_FILE"] = str(index_path)
    return env


def _run(
    argv: list[str], *, cwd: Path, index_path: Path | None = None,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(  # noqa: S603 - argv list, shell=False, fixed binary name
            ["git", "--no-pager", "--no-optional-locks", *argv],
            cwd=str(cwd), capture_output=True, text=True, shell=False,
            env=_environment(index_path=index_path), timeout=_TIMEOUT,
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

    `force=True` by default: Git's own built-in "is this worktree clean"
    heuristic (what `git worktree remove` would otherwise refuse on by
    itself) does not understand Code Slayer's checkpoint model (a
    checkpoint is a commit object reachable only from a dedicated
    `refs/codeslayer/checkpoints/...` ref, never merged into the job
    worktree's own branch/HEAD, so Git always sees the working tree
    itself as dirty/untracked even once fully checkpointed). The actual
    safety decision belongs entirely to `job_worktree.release_job_worktree()`'s
    own lease/unresolved-operation/checkpoint-bookkeeping checks *and*
    this module's own `is_worktree_clean()` real-tree comparison,
    performed *before* this function is ever called — never to Git's
    built-in cleanliness heuristic, which this call deliberately bypasses
    (it is not the safety boundary; the caller's checks are).
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


def worktree_status(path: Path, *, against: str, index_path: Path) -> tuple[str, ...]:
    """Every path where the ACTUAL, on-disk working tree at `path`
    (never a filesystem diff this module computes itself — Git's own
    `status --porcelain` is the sole authority) differs from the tree
    named by `against` (a commit-ish or tree-ish): a modified tracked
    file, a deleted tracked file, an untracked file, or — Phase 7.7b —
    an *ignored* file that still physically exists. Empty means the
    working tree exactly matches `against` and nothing removing it would
    destroy.

    Ignored content is included deliberately (`--ignored`, the
    traditional per-file mode, never `--ignored=matching`, which would
    collapse a whole ignored directory into one non-enumerable entry):
    for Code Slayer's own cleanup purposes, "ignored by `.gitignore`"
    describes what a *commit* should skip, not what is disposable —
    `job_worktree.release_job_worktree()`'s own docstring states this
    explicitly. A build artifact, a cache file, or a log file the job
    itself produced is real filesystem content that `remove_worktree()`
    would permanently destroy, ignored or not.

    Uses a private, temporary index file at `index_path` (created fresh
    here, overwritten if it already exists; the caller removes it when
    done) seeded from `against` via `git read-tree` — exactly the same
    pattern `checkpoint_git.build_tree()` already uses for a different
    purpose. The job worktree's own real `.git/index` and `HEAD` are
    never read from or written to by this call (see `staged_changes()`
    for the check that *does* inspect the real index), and `against`
    differing from the job worktree's own (permanently
    base-revision-pinned) `HEAD` is never itself reported as dirtiness —
    only the working tree actually disagreeing with `against`'s own
    content is. A shared Git object/ref addition (e.g. a checkpoint
    commit reachable only from its own dedicated ref) is not an on-disk
    working-tree file and therefore never appears here either.
    """
    if index_path.exists():
        index_path.unlink()
    read = _run(["read-tree", against], cwd=path, index_path=index_path)
    if read.returncode != 0:
        raise JobWorktreeGitError(f"cannot read tree {against!r} for cleanliness check")
    result = _run(
        [
            "status", "--porcelain=v2", "--untracked-files=all",
            "--ignore-submodules=all", "--ignored",
        ],
        cwd=path, index_path=index_path,
    )
    if result.returncode != 0:
        raise JobWorktreeGitError("git status failed during cleanliness check")
    dirty: list[str] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        if line.startswith(("? ", "! ")):
            # Untracked ("?") and ignored-but-present ("!") content are
            # both physical filesystem bytes `remove_worktree()` would
            # destroy — see this function's own docstring on why ignored
            # is never treated as disposable here.
            dirty.append(line[2:].strip())
            continue
        if line.startswith("u "):
            # An unmerged/conflicted path is never clean, regardless of
            # its own XY code shape.
            fields = line.split(" ", 10)
            dirty.append(fields[-1] if fields else line)
            continue
        if line.startswith(("1 ", "2 ")):
            fields = line.split(" ", 2)
            xy = fields[1] if len(fields) > 1 else ""
            # `xy[0]` (index vs HEAD) is deliberately ignored: seeding the
            # index from `against` makes it differ from the job
            # worktree's own real HEAD whenever `against` itself does
            # (e.g. a checkpoint tree vs. the pinned base revision) —
            # that is expected and not itself dirtiness. `xy[1]`
            # (worktree vs index, i.e. vs `against`) is the one signal
            # that means the actual on-disk content has changed. The
            # trailing text captured here is the exact reported path for
            # an ordinary ("1 ") record; for a rename ("2 ") record it is
            # the remainder of that record's own extra fields plus the
            # path -- correct for detecting dirtiness (all we actually
            # rely on), only approximate as a human-readable path.
            if len(xy) == 2 and xy[1] != ".":
                dirty.append(fields[2] if len(fields) > 2 else line)
            continue
        raise JobWorktreeGitError(f"unrecognized git status record: {line!r}")
    return tuple(dirty)


def is_worktree_clean(path: Path, *, against: str, index_path: Path) -> bool:
    """`True` if `worktree_status()` reports no differences at all."""
    return worktree_status(path, against=against, index_path=index_path) == ()


def staged_changes(path: Path) -> tuple[str, ...]:
    """Every path staged in `path`'s own REAL index (never the private,
    temporary one `worktree_status()` seeds) that differs from its own
    `HEAD` — Phase 7.7b's fix for the "staged-only state can be lost"
    finding: a tracked file that was modified, `git add`ed, and then had
    its working-tree bytes restored to match `HEAD`/the checkpoint tree
    is invisible to a working-tree-vs-tree comparison (the bytes on disk
    are clean) but the staged blob a later `git commit` would use is not
    — cleanup must see that too.

    `git diff --cached --name-only HEAD` is run with no `GIT_INDEX_FILE`
    override, so it reads the job worktree's own genuine `.git/index` —
    read-only; `git diff` never mutates the index it compares. This
    reports staged modifications, staged additions, and staged deletions
    alike (confirmed empirically: `git rm --cached` and a staged new file
    both appear here exactly as a staged edit does). It deliberately
    compares against `HEAD` specifically, not against `against`
    (`worktree_status()`'s checkpoint-or-base-revision target): a job
    worktree's `HEAD` never moves after `job_worktree_git.add_worktree()`
    creates it (see `job_worktree.py`'s "Base revision: explicit, frozen,
    detached"), and nothing in Code Slayer's own tool-execution path ever
    legitimately stages anything, so ANY staged difference from that
    permanently-fixed `HEAD` is unexplained, real index content that a
    `git worktree remove` would discard.

    Intent-to-add (`git add -N`) needs no special handling here: it is
    invisible to `git diff --cached`, but the physically-existing file it
    marks is already caught by `worktree_status()`'s own untracked-file
    detection regardless of what the real index's intent-to-add bit says.
    """
    result = _run(["diff", "--cached", "--name-only", "HEAD"], cwd=path)
    if result.returncode != 0:
        raise JobWorktreeGitError("git diff --cached failed during cleanliness check")
    return tuple(line for line in result.stdout.splitlines() if line)
