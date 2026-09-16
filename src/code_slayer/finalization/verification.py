"""Repository-grounded verification commands: discovery, safe execution,
and durable `tool_operations` evidence.

## Runs against an isolated materialized tree, never the live worktree

`ground_verification_commands()`/`run_verification_commands()` both take a
`tree_root` / `verification_root` directory — this is, by construction,
`finalization.service.Finalizer`'s isolated materialization of exactly
`verified_tree_sha` (see that module's "Verification executes against an
isolated tree" section), never the live, uncontrolled repository working
directory. Ignored, untracked, or unowned-but-modified live content simply
does not exist in that directory at all, so it cannot influence `pytest`/
`ruff`/`mypy`/any other verification command's result — closing the gap
where such content could make verification pass (or fail) for reasons
absent from the checkpointed content. This module itself never reads
`repo_root` or touches live worktree state; it only ever receives the
already-isolated directory as a plain path.

## Planner text is never execution authority

`planning.planner.PlannerOutput.discovered_commands` is a model's own
*assertion* about what commands exist — never trusted here, never even
read by this module. `ground_verification_commands()` instead calls
`intelligence.commands.discover_commands()` directly against the isolated
tree, at verification time, exactly the same deterministic,
never-executes-anything, code-owned discovery Phase 8.1 already built for
planning context — meaning a config file (`pyproject.toml`, `Makefile`,
...) that itself is neither baseline-committed nor owned is invisible
here too, exactly like any other uncheckpointable content. A command
becomes eligible to run only if it is:

1. produced by that fresh, live-repository discovery (never asserted by a
   model), AND
2. `confidence == "high"` (a `Makefile` target, `package.json` script, or
   `pyproject.toml` tool section directly names it as runnable — not a
   bare `tox.ini`/`noxfile.py` presence, which only implies *some* command
   exists under environment resolution this module does not replicate),
   AND
3. its exact command string has a fixed, code-owned argv decomposition in
   `_ARGV_BY_COMMAND` below — never a shell string, never split from
   arbitrary text. A command that is discovered but has no entry here is
   left as a candidate only; it is never executed. This is deliberately a
   small, closed set for this first vertical slice (`npm run <script>`/
   `make <target>` are discoverable but not yet executable — see the
   Deterministic Finalization report's risk/edge-case list).

## Verification never mutates what it verifies

`_ARGV_BY_COMMAND`'s argv is deliberately **not** a literal parse of the
discovered command string: `discover_commands()` names `"ruff format ."`/
`"black ."` because that is the real, repository-declared formatter
invocation other contexts (planning) legitimately care about, but running
either of those verbatim during `VERIFYING` would *rewrite* the worker's
own not-yet-verified changes before any verdict is reached — mutating the
very thing being verified, and doing so with no `tool_operations`
before/after ownership evidence at all (this module's evidence rows are
GIT_READ, not WRITE_OWNED). Every formatter entry below is mapped to its
tool's own read-only "check" invocation (`--check`) instead; the
`VerificationCommandResult.command` field still carries the discovered
label (`"ruff format ."`) for readability/audit continuity, while `argv`
carries the actual, safe, non-mutating invocation that was really run.

## Evidence, not a general command runner

Every executed command is captured with a fixed timeout and output cap, no
shell, a minimal explicit environment, and durably journaled through
`tool_operations` exactly like every other Code Slayer side effect: a
`STARTED` row commits before the subprocess runs, and the terminal row
commits only after the subprocess is fully resolved — an interrupted
capture is recorded `UNKNOWN`, never guessed as `SUCCEEDED`/`FAILED`.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.finalization.types import VerificationCommandResult
from code_slayer.intelligence.commands import discover_commands
from code_slayer.intelligence.models import CommandCandidate
from code_slayer.lease.liveness import process_start_time
from code_slayer.lease.manager import LeaseHandle, LeaseManager
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.tool_operations_repo import (
    OperationStatus,
    ToolOperationsRepo,
    compute_request_hash,
)
from code_slayer.tools.models import RiskClass

TOOL_NAME = "verify_command"

# The complete, fixed, code-owned set of commands this module will ever
# execute -- keyed by the exact string `intelligence.commands.
# discover_commands()` produces, mapped to a safe argv tuple (never a shell
# string, never split from arbitrary text, and -- for formatters -- never
# the mutating invocation the discovered label names; see "Verification
# never mutates what it verifies" above). Extending this set is a
# deliberate code change, never request/plan data.
_ARGV_BY_COMMAND: dict[str, tuple[str, ...]] = {
    "pytest": ("pytest",),
    "ruff check .": ("ruff", "check", "."),
    "ruff format .": ("ruff", "format", "--check", "."),
    "mypy .": ("mypy", "."),
    "black .": ("black", "--check", "."),
}

# Root-level files `discover_commands()` knows how to read. Only these
# fixed, top-level names are ever opened -- no repository walk.
_CANDIDATE_ROOT_FILES = (
    "package.json", "pyproject.toml", "Makefile", "makefile", "GNUmakefile",
    "tox.ini", "noxfile.py",
)

_MAX_ROOT_FILE_BYTES = 1_000_000
_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_OUTPUT_LIMIT = 65536


class FinalizationError(RuntimeError):
    """Stable reason code; never includes command output or environment
    bytes."""


def _safe_read_text(root: Path, name: str) -> str | None:
    try:
        path = root / name
        if not path.is_file():
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > _MAX_ROOT_FILE_BYTES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def ground_verification_commands(tree_root: Path) -> tuple[CommandCandidate, ...]:
    """Recompute the repository-derived command set against `tree_root`
    and return only the executable subset: `high` confidence and present
    in `_ARGV_BY_COMMAND`. Deterministic, read-only, never executes
    anything itself. `tree_root` is expected to be the isolated
    materialization of `verified_tree_sha` — a config file that exists
    only in the live worktree (never baseline-committed, never owned) is
    correctly invisible here."""
    paths = {name for name in _CANDIDATE_ROOT_FILES if (tree_root / name).is_file()}
    candidates = discover_commands(paths, lambda name: _safe_read_text(tree_root, name))
    return tuple(
        c for c in candidates if c.confidence == "high" and c.command in _ARGV_BY_COMMAND
    )


@dataclass(frozen=True)
class _CaptureOutput:
    stdout: bytes
    stderr: bytes
    returncode: int
    truncated: bool
    timed_out: bool


def _environment() -> dict[str, str]:
    # Deliberately narrow but functional: verification commands are real
    # project tools (pytest/ruff/mypy/...) that must resolve against the
    # project's own interpreter/virtualenv, unlike the git-only, PATH-
    # nuked profile in `tools.command_tools` -- but never the caller's
    # full environment (no ambient secrets forwarded).
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME", "/nonexistent"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC",
    }
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "UV_PROJECT_ENVIRONMENT"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def _capture(
    argv: tuple[str, ...], *, cwd: Path, timeout: float, limit: int, on_spawn,
) -> _CaptureOutput:
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        list(argv), cwd=str(cwd), env=_environment(), shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    buffers = [bytearray(), bytearray()]
    truncated = timed_out = False
    try:
        on_spawn(process.pid)
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((process.stdout, process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    available = max(0, limit - sum(map(len, buffers)))
                    buffers[key.data].extend(chunk[:available])
                    if len(chunk) > available:
                        truncated = True
                        break
                if truncated:
                    break
            if truncated or timed_out:
                _terminate(process)
            else:
                try:
                    process.wait(timeout=max(0.001, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _terminate(process)
    except BaseException:
        _terminate(process)
        raise
    finally:
        process.stdout.close()
        process.stderr.close()
    return _CaptureOutput(
        bytes(buffers[0]), bytes(buffers[1]), process.returncode, truncated, timed_out,
    )


def _terminate(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


def run_verification_commands(
    conn, *, task_id: str, verification_root: Path, lease: LeaseHandle,
    tree_sha: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS, output_limit: int = _DEFAULT_OUTPUT_LIMIT,
    now_fn=utcnow_iso,
) -> tuple[VerificationCommandResult, ...]:
    """Ground and execute the fixed, code-owned verification-command set
    for one task, durably journaling every attempt through
    `tool_operations` exactly like every other Code Slayer side effect.

    `verification_root` MUST be the isolated materialization of
    `verified_tree_sha` (see the module docstring and `finalization.
    service.Finalizer._materialize_verification_tree`) — this function
    never distinguishes an isolated tree from a live repo root, so
    verifying that distinction is entirely the caller's responsibility;
    `tree_sha`, when given, is recorded on every command's audit payload
    purely as provenance (which exact tree these commands ran against),
    never consulted for any decision here.

    Fencing is revalidated immediately before each command's `STARTED` row
    commits and again immediately before its terminal row commits -- a
    takeover mid-run must not let a now-stale caller record a result."""
    if not isinstance(lease, LeaseHandle):
        raise FinalizationError("malformed_lease_handle")
    operations = ToolOperationsRepo(conn)
    audit = AuditWriter(conn)
    leases = LeaseManager(conn)
    results: list[VerificationCommandResult] = []
    for candidate in ground_verification_commands(verification_root):
        argv = _ARGV_BY_COMMAND[candidate.command]
        operation_id = str(uuid.uuid4())
        request_hash = compute_request_hash(TOOL_NAME, {
            "task_id": task_id, "command": candidate.command, "argv": list(argv),
        })
        with transaction(conn):
            if not leases.is_current(lease):
                raise FinalizationError("stale_fencing_token")
            operations.start_in_transaction(
                task_id=task_id, worktree_id=lease.worktree_id,
                worker_id=lease.worker_id, worker_session_id=lease.worker_session_id,
                lease_generation=lease.generation, tool_name=TOOL_NAME,
                risk_class=RiskClass.READ_ONLY.value, request_hash=request_hash,
                target_resource=candidate.command, operation_id=operation_id,
            )
            audit.append(
                task_id=task_id, event_type=EventType.COMMAND_STARTED, actor_type="system",
                actor_id="finalizer",
                payload={
                    "operation_id": operation_id, "command": candidate.command,
                    "purpose": candidate.purpose, "evidence_source": candidate.evidence_source,
                    "verification_tree_sha": tree_sha,
                },
            )
        # STARTED is committed before the subprocess runs. An interrupted
        # capture (an exception escaping `_capture`) is genuinely
        # uncertain -- never guessed as FAILED; only a `_capture` call
        # that actually returns a `_CaptureOutput` yields a deterministic
        # SUCCEEDED/FAILED.
        try:
            output = _capture(
                argv, cwd=verification_root, timeout=timeout, limit=output_limit,
                on_spawn=lambda pid, _op_id=operation_id: operations.record_child_pid(
                    _op_id, pid, process_start_time(pid),
                ),
            )
            status = (
                OperationStatus.SUCCEEDED
                if output.returncode == 0 and not output.truncated and not output.timed_out
                else OperationStatus.FAILED
            )
            reason = (
                "command_timeout" if output.timed_out
                else "output_limit" if output.truncated else "command_completed"
            )
            returncode = output.returncode
            truncated = output.truncated
            timed_out = output.timed_out
        except Exception:
            status = OperationStatus.UNKNOWN
            reason = "execution_or_capture_failed"
            returncode = None
            truncated = False
            timed_out = False
        with transaction(conn):
            if not leases.is_current(lease):
                raise FinalizationError("stale_fencing_token")
            operations.finish_in_transaction(
                operation_id, status=status,
                result={
                    "reason": reason, "returncode": returncode, "truncated": truncated,
                    "timed_out": timed_out, "reconcile_required": status == OperationStatus.UNKNOWN,
                },
            )
            audit.append(
                task_id=task_id, event_type=EventType.COMMAND_FINISHED, actor_type="system",
                actor_id="finalizer",
                payload={
                    "operation_id": operation_id, "command": candidate.command,
                    "status": status, "returncode": returncode, "reason": reason,
                    "verification_tree_sha": tree_sha,
                },
            )
            if candidate.purpose == "test":
                audit.append(
                    task_id=task_id,
                    event_type=(
                        EventType.TEST_PASS if status == OperationStatus.SUCCEEDED
                        else EventType.TEST_FAIL
                    ),
                    actor_type="system", actor_id="finalizer",
                    payload={"operation_id": operation_id, "command": candidate.command},
                )
        results.append(VerificationCommandResult(
            command=candidate.command, purpose=candidate.purpose,
            evidence_source=candidate.evidence_source, confidence=candidate.confidence,
            argv=argv, operation_id=operation_id, status=status, returncode=returncode,
            truncated=truncated, timed_out=timed_out, reason=reason,
        ))
    return tuple(results)
