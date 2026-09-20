"""Fixed-argv subprocess runner. Never `shell=True`."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class ProcessError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def run_fixed(
    argv: tuple[str, ...],
    *,
    cwd: str | Path | None = None,
    timeout: float = 15.0,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    if not argv or any(not isinstance(item, str) or item == "" for item in argv):
        raise ProcessError("invalid_argv")
    try:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            timeout=timeout,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise ProcessError("executable_missing", argv[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise ProcessError("process_timeout", argv[0]) from exc
    return ProcessResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


def git_rev_parse_head(checkout: Path, *, runner=None) -> str | None:
    """Return the current checkout HEAD SHA, or None if it cannot be resolved.

    This is a live observation of Git, not proof of which revision a
    already-running process loaded. Callers that capture it at process
    start must retain that value themselves.
    """
    run = runner or run_fixed
    try:
        result = run(("git", "-C", str(checkout), "rev-parse", "HEAD"), timeout=5.0)
    except ProcessError:
        return None
    sha = (result.stdout or "").strip().lower()
    if result.returncode != 0 or len(sha) != 40:
        return None
    if any(ch not in "0123456789abcdef" for ch in sha):
        return None
    return sha


@dataclass(frozen=True)
class CheckoutSourceIdentity:
    """HEAD plus working-tree cleanliness. Not a hash of loaded bytecode.

    A dirty tree under an editable install means HEAD does not identify
    the source the process can import. That state is never VERIFIED.
    """

    commit: str | None
    dirty: bool | None
    state: str
    commit_source: str


def inspect_checkout(checkout: Path, *, runner=None) -> CheckoutSourceIdentity:
    run = runner or run_fixed
    commit = git_rev_parse_head(checkout, runner=run)
    dirty = _git_working_tree_dirty(checkout, runner=run)
    if commit is None or dirty is None:
        return CheckoutSourceIdentity(
            commit=commit,
            dirty=None,
            state="unverified",
            commit_source="UNVERIFIED",
        )
    if dirty:
        return CheckoutSourceIdentity(
            commit=commit,
            dirty=True,
            state="dirty",
            commit_source="DIRTY",
        )
    return CheckoutSourceIdentity(
        commit=commit,
        dirty=False,
        state="clean",
        commit_source="VERIFIED",
    )


def _git_working_tree_dirty(checkout: Path, *, runner=None) -> bool | None:
    """True if tracked, staged, or untracked (non-ignored) paths exist.

    Untracked files are source-identity relevant for an editable install:
    Python can import them. Ignored paths are omitted (``status`` default).
    """
    run = runner or run_fixed
    try:
        result = run(
            (
                "git",
                "-C",
                str(checkout),
                "status",
                "--porcelain=v1",
                "--untracked-files=normal",
            ),
            timeout=5.0,
        )
    except ProcessError:
        return None
    if result.returncode != 0:
        return None
    return bool((result.stdout or "").strip())
