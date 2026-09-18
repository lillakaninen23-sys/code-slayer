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
