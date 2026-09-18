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
