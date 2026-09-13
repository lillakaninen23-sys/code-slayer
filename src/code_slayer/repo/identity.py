"""Repository/worktree identity foundation (Foundation Plan §03, Revision 2.1).

Identity is never derived from an untracked working-tree file — a
`git clean -fdx` would delete it, destroying exactly the thing meant to
protect against that class of mistake. Instead:

* `repo_id` is a UUID stored in the repository's SHARED git config
  (`git config --local codeslayer.repo-id <uuid>`). Because `--local` is
  passed explicitly, this lands in the common config even on a repository
  with `extensions.worktreeConfig` enabled, and so is visible from every
  linked worktree of that repository.
* `worktree_id` is a UUID stored in a file under the per-worktree git dir
  (`git rev-parse --git-dir`) — exactly where Git itself keeps
  per-worktree private state (`HEAD`, `index`, `logs/HEAD`).

Both survive `git clean -fdx` and `git reset --hard`, since neither
touches anything under `.git/`. `git clone` does not copy arbitrary custom
config keys, so a clone has no `repo_id` of its own and is correctly
treated as an unrelated, unknown repository rather than silently
inheriting another machine's identity — fail safe, not fail silent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from code_slayer.repo import git

CONFIG_KEY_REPO_ID = "codeslayer.repo-id"
WORKTREE_ID_FILENAME = "codeslayer-id"


class NotAGitRepositoryError(RuntimeError):
    """`path` is not inside a Git working tree."""


class UnknownIdentityError(RuntimeError):
    """Identity has not been established for this repo/worktree, and
    `resolve(..., create=False)` was asked not to establish it."""


@dataclass(frozen=True)
class RepoIdentity:
    """Everything Phase 1 needs to know about where a repo/worktree is."""

    repo_id: str
    worktree_id: str
    repo_root: Path
    git_common_dir: Path
    git_dir: Path


def _read_or_create_worktree_id(worktree_git_dir: Path) -> str:
    id_file = worktree_git_dir / WORKTREE_ID_FILENAME
    if id_file.exists():
        existing = id_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    new_id = str(uuid.uuid4())
    id_file.write_text(new_id + "\n", encoding="utf-8")
    return new_id


def _read_or_create_repo_id(repo_root: Path) -> str:
    existing = git.get_config(repo_root, CONFIG_KEY_REPO_ID)
    if existing:
        return existing
    new_id = str(uuid.uuid4())
    git.set_config(repo_root, CONFIG_KEY_REPO_ID, new_id)
    return new_id


def resolve(path: Path | str, *, create: bool = True) -> RepoIdentity:
    """Resolve identity for the repository/worktree containing `path`.

    With `create=True` (the default), establishes `repo_id`/`worktree_id`
    if they don't exist yet. With `create=False`, only reads what already
    exists and raises `UnknownIdentityError` if either piece is missing —
    for a future read-only `codeslayer inspect` that must never mutate
    `.git/` as a side effect of looking.

    Raises `NotAGitRepositoryError` if `path` is not inside a Git work tree.
    """
    path = Path(path)
    if not git.is_inside_work_tree(path):
        raise NotAGitRepositoryError(f"{path} is not inside a Git work tree")

    repo_root = git.show_toplevel(path)
    common_dir = git.git_common_dir(path)
    worktree_git_dir = git.git_dir(path)

    if create:
        repo_id = _read_or_create_repo_id(repo_root)
        worktree_id = _read_or_create_worktree_id(worktree_git_dir)
    else:
        repo_id = git.get_config(repo_root, CONFIG_KEY_REPO_ID)
        id_file = worktree_git_dir / WORKTREE_ID_FILENAME
        worktree_id = id_file.read_text(encoding="utf-8").strip() if id_file.exists() else None
        if not repo_id or not worktree_id:
            raise UnknownIdentityError(
                f"no Code Slayer identity established yet for {path} "
                "(call resolve(create=True) to establish one)"
            )

    return RepoIdentity(
        repo_id=repo_id,
        worktree_id=worktree_id,
        repo_root=repo_root,
        git_common_dir=common_dir,
        git_dir=worktree_git_dir,
    )
