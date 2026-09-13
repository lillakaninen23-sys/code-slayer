"""A thin, explicit subprocess wrapper around the `git` binary.

No `shell=True`, ever — every call is `argv`, never an interpolated
string. No GitPython: shelling out to whatever `git` the user already has
installed gives byte-for-byte parity with real Git behavior, and Git is
security-sensitive enough that a thin, fully-tested wrapper beats a large
third-party abstraction (Foundation Plan §04).

Everything here is a read, or a narrowly-scoped `git config --local`
write. Nothing here mutates a branch, the index, or the working tree.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a `git` invocation exits non-zero."""

    def __init__(self, argv: tuple[str, ...], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{' '.join(argv)} failed ({returncode}): {stderr.strip()}")


@dataclass(frozen=True)
class GitResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def run(args: list[str], *, cwd: Path | str, check: bool = True) -> GitResult:
    """Run `git <args>` with `shell=False`, in `cwd`."""
    argv = ("git", *args)
    proc = subprocess.run(  # noqa: S603 - argv list, shell=False, fixed binary name
        list(argv),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        shell=False,
    )
    result = GitResult(argv, proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise GitError(argv, proc.returncode, proc.stderr)
    return result


def is_inside_work_tree(cwd: Path | str) -> bool:
    try:
        result = run(["rev-parse", "--is-inside-work-tree"], cwd=cwd)
    except GitError:
        return False
    return result.stdout.strip() == "true"


def _resolve_git_path(cwd: Path | str, output: str) -> Path:
    path = Path(output.strip())
    if not path.is_absolute():
        path = Path(cwd) / path
    return path.resolve()


def show_toplevel(cwd: Path | str) -> Path:
    """The working tree root."""
    result = run(["rev-parse", "--show-toplevel"], cwd=cwd)
    return Path(result.stdout.strip()).resolve()


def git_dir(cwd: Path | str) -> Path:
    """The per-worktree git dir: `.git` for the main worktree, or
    `.git/worktrees/<name>` for a linked one — never shared between
    worktrees of the same repository."""
    result = run(["rev-parse", "--git-dir"], cwd=cwd)
    return _resolve_git_path(cwd, result.stdout)


def git_common_dir(cwd: Path | str) -> Path:
    """The git dir shared by every worktree of a repository."""
    result = run(["rev-parse", "--git-common-dir"], cwd=cwd)
    return _resolve_git_path(cwd, result.stdout)


def get_config(cwd: Path | str, key: str) -> str | None:
    """Read a `--local` config value, or `None` if it is unset."""
    try:
        result = run(["config", "--local", "--get", key], cwd=cwd)
    except GitError as exc:
        if exc.returncode == 1:
            return None
        raise
    value = result.stdout.strip()
    return value or None


def set_config(cwd: Path | str, key: str, value: str) -> None:
    """Write a `--local` config value.

    `--local` is passed explicitly (not the default scope) so this always
    lands in the repository's shared config file, even on a repository
    with `extensions.worktreeConfig` enabled.
    """
    run(["config", "--local", key, value], cwd=cwd)


def head_sha(cwd: Path | str) -> str | None:
    """The current HEAD commit sha, or `None` if HEAD is unborn."""
    result = run(["rev-parse", "--verify", "-q", "HEAD"], cwd=cwd, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def current_branch(cwd: Path | str) -> str | None:
    """The current branch name, or `None` if HEAD is detached/unborn."""
    result = run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd, check=False)
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return None if branch == "HEAD" else branch


def status_porcelain(cwd: Path | str) -> str:
    """Full `git status --porcelain=v2`, including untracked files."""
    result = run(["status", "--porcelain=v2", "--untracked-files=all"], cwd=cwd)
    return result.stdout
