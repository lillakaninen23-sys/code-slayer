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

import os
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


def _environment() -> dict[str, str]:
    # cwd, not inherited Git overrides, selects the repository. Disable
    # lazy fetching and all transports even in partial/promisor clones.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "LC_ALL": "C",
    })
    return env


def run(args: list[str], *, cwd: Path | str, check: bool = True) -> GitResult:
    """Run `git <args>` with `shell=False`, in `cwd`."""
    argv = ("git", *args)
    proc = subprocess.run(  # noqa: S603 - argv list, shell=False, fixed binary name
        list(argv),
        cwd=str(cwd),
        capture_output=True,
        text=True,
        shell=False,
        env=_environment(),
    )
    result = GitResult(argv, proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise GitError(argv, proc.returncode, proc.stderr)
    return result


def read_bytes(args: list[str], *, cwd: Path | str) -> bytes:
    """Read Git metadata without text conversion, optional writes, or helpers.

    Inspection uses only these read commands. Local filter commands are
    neutralized rather than executed to compute status. Consequently such
    files may conservatively appear dirty without their clean filter.
    Submodule working trees are never traversed by inspection status.
    """
    if not args or args[0] not in {"status", "ls-files", "ls-tree", "rev-parse"}:
        raise GitError(tuple(args), -1, "not an inspection read command")
    config_argv = (
        "git", "config", "--includes", "--null", "--name-only", "--get-regexp",
        r"^filter\..*\.(clean|smudge|process|required)$",
    )
    config = subprocess.run(
        config_argv, cwd=str(cwd), capture_output=True, shell=False, env=_environment(),
    )
    if config.returncode not in (0, 1):
        raise GitError(
            config_argv, config.returncode, config.stderr.decode("utf-8", errors="replace"),
        )
    options = [
        "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false", "-c", "core.hooksPath=" + os.devnull,
        "-c", "core.attributesFile=" + os.devnull,
        "-c", "status.submoduleSummary=false", "-c", "submodule.recurse=false",
        "-c", "status.renameLimit=1000",
    ]
    for key in sorted(set(config.stdout.decode("utf-8").rstrip("\0").split("\0")) - {""}):
        options.extend(["-c", key + ("=false" if key.endswith(".required") else "=")])
    argv = ("git", *options, *args)
    proc = subprocess.run(
        argv, cwd=str(cwd), capture_output=True, shell=False, env=_environment(),
    )
    if proc.returncode:
        raise GitError(argv, proc.returncode, proc.stderr.decode("utf-8", errors="replace"))
    return proc.stdout


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
