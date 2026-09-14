"""Best-effort process-liveness evidence for lease quiescence (Phase 6).

This module answers exactly one question, conservatively: *can we prove*
that the process which last held a lease (or spawned a still-tracked
child) is gone? It never answers "is this process the authoritative
owner" — the durable fencing generation remains the sole authority for
that (`lease.manager`). Liveness evidence only ever decides whether a
`QUIESCING` lease may safely advance to `EXPIRED`.

## `GONE` vs. `UNKNOWN` — not interchangeable

`GONE` is returned *only* when there is positive, reliable evidence that
the recorded process identity no longer exists: `/proc/<pid>` itself is
absent (`ENOENT`/`ESRCH`) right now, or the same pid exists but under a
*different*, exactly-established identity (see reuse protection below).
Every other failure to establish liveness — `/proc` unavailable on this
platform, a permission/read failure, an incomplete or malformed
`/proc/<pid>/stat`, a system boot time or clock-tick rate that could not
be read or parsed, no recorded identity to compare against at all — is
`UNKNOWN`, never `GONE`. Failing to *prove* liveness is not proof of
death: this module never collapses "I could not read the evidence" into
"the process is gone." `LeaseManager` in turn treats `UNKNOWN` exactly
like `ALIVE` for the purpose of completing quiescence — only a definite
`GONE` (for both the worker and any recorded child) ever permits
`QUIESCING -> EXPIRED`.

## Process identity: exact, not approximate

A bare `pid` is not proof of identity: after a process exits, the OS is
free to reuse that same integer for something unrelated. To rule this
out, every recorded pid — a worker's own (`worker_pid`) and any
subprocess it spawned and journaled (`tool_operations.child_pid`) alike —
is paired with an *exact* process-start identity read from `/proc`, never
from our own wall clock (which cannot reconstruct the OS's own allocation
of a pid to a specific process).

That identity is the raw `(boot_time, starttime_ticks, clock_ticks_per_second)`
triple exactly as `/proc/stat`'s `btime` line and `/proc/<pid>/stat`'s
`starttime` field (field 22) report them — deliberately *not* a computed,
formatted, and re-parsed floating-point timestamp. Converting to seconds
and back through a formatted string is lossy (`/proc`'s tick-granularity
divided against a large epoch value loses precision in a `float`, and
re-parsing a rounded string reconstructs a slightly different value even
for the exact same process); comparing the raw integers `/proc` itself
reports avoids that entirely, so two recordings compare equal if and only
if they name the exact same boot and the exact same tick count — never
"close enough". There is no time tolerance anywhere in this comparison:
a pid whose current start identity differs from the recorded one at all,
by any amount, is a reused pid, not the recorded process.

- pid does not exist at all right now (positive evidence: `/proc/<pid>`
  is absent) -> the recorded process is `GONE`.
- pid exists, and its *current* exact start identity matches the
  *recorded* one exactly -> `ALIVE`, genuinely the same process.
- pid exists, but its current start identity does not exactly match the
  recorded one -> the pid has been reused by a later, different process;
  the *recorded* process is still `GONE`.
- anything that could not be positively established either way (no
  `/proc` on this platform, permission denied, an unreadable or
  unparseable `/proc/<pid>/stat`, an unreadable boot time or clock-tick
  rate, no recorded identity to compare against) -> `UNKNOWN`, never
  guessed at either way.

Only Linux's `/proc` is used; anywhere else this conservatively reports
`UNKNOWN` for every query rather than fabricate an answer.
"""

from __future__ import annotations

import os
from enum import Enum, StrEnum

_ID_SEP = ":"


class Liveness(StrEnum):
    ALIVE = "ALIVE"
    GONE = "GONE"
    UNKNOWN = "UNKNOWN"


class _Existence(Enum):
    """What we could positively establish about a pid right now — kept
    separate from `Liveness` because "present" alone says nothing yet
    about whether it is the *recorded* process (that needs an identity
    comparison the caller performs)."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


def _has_proc() -> bool:
    return os.path.isdir("/proc")


def _boot_time_epoch_seconds() -> int | None:
    """The kernel's own boot time, in whole seconds since the epoch, as
    `/proc/stat`'s `btime` line always reports it (an integer — never a
    fraction) — or `None` if it could not be read or parsed."""
    try:
        with open("/proc/stat", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _clock_ticks_per_second() -> int | None:
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return None
    return hz if isinstance(hz, int) and hz > 0 else None


def _pid_existence_and_identity(
    pid: int,
) -> tuple[_Existence, tuple[int, int, int] | None]:
    """Positively establish whether `pid` currently exists, and — only
    when it does and every piece of evidence needed is readable and
    parseable — its exact `(boot, starttime_ticks, hz)` identity.

    `identity` is `None` whenever it could not be exactly established,
    *including* when `existence` is `PRESENT`: a pid can be definitely
    present yet have an unreadable/malformed/incomplete stat record, a
    system boot time that could not be read, or a clock-tick rate that
    could not be determined — none of that is evidence the process is
    gone, only that its identity cannot be proven right now."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read()
    except (FileNotFoundError, ProcessLookupError):
        # Positive evidence: no such process exists right now.
        return _Existence.ABSENT, None
    except OSError:
        # Permission denied, or some other unreadable condition — this
        # has NOT proven the process is gone, only that we could not
        # read its evidence.
        return _Existence.UNKNOWN, None
    try:
        # `comm` (field 2) is parenthesized and may itself contain
        # spaces/parens; splitting on the *last* ')' skips past it safely
        # regardless of its contents.
        after_comm = raw.rsplit(b")", 1)[1]
        fields = after_comm.split()
        starttime_ticks = int(fields[19])  # field 22 overall (starttime)
    except (IndexError, ValueError):
        # The pid definitely exists (we just read its own /proc entry),
        # but its identity could not be parsed out of it.
        return _Existence.PRESENT, None
    boot = _boot_time_epoch_seconds()
    hz = _clock_ticks_per_second()
    if boot is None or hz is None:
        return _Existence.PRESENT, None
    return _Existence.PRESENT, (boot, starttime_ticks, hz)


def _format_identity(identity: tuple[int, int, int]) -> str:
    boot, ticks, hz = identity
    return f"{boot}{_ID_SEP}{ticks}{_ID_SEP}{hz}"


def _parse_identity(value: str) -> tuple[int, int, int] | None:
    parts = value.split(_ID_SEP)
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def process_start_time(pid: int) -> str | None:
    """The exact, `/proc`-derived process-start identity of the
    *currently running* process at `pid`, for later liveness/reuse
    cross-referencing — `None` if this cannot be positively established
    (no `/proc`, pid not found, unreadable, or unparseable in any way).

    This is deliberately *not* a formatted wall-clock timestamp: see the
    module docstring for why the raw `/proc` fields are encoded directly
    rather than converted through a lossy floating-point round trip.
    """
    if not _has_proc():
        return None
    existence, identity = _pid_existence_and_identity(pid)
    if existence != _Existence.PRESENT or identity is None:
        return None
    return _format_identity(identity)


def check_process_liveness(pid: int | None, recorded_start_iso: str | None) -> Liveness:
    """Reuse-safe liveness check: `ALIVE` only if `pid` exists right now
    *and* its current exact start identity matches `recorded_start_iso`
    exactly — no tolerance. See the module docstring for the full
    `GONE`/`ALIVE`/`UNKNOWN` decision table."""
    if pid is None:
        return Liveness.UNKNOWN
    if not _has_proc():
        return Liveness.UNKNOWN
    existence, identity = _pid_existence_and_identity(pid)
    if existence == _Existence.ABSENT:
        return Liveness.GONE
    if existence == _Existence.UNKNOWN:
        return Liveness.UNKNOWN
    # existence == PRESENT
    if identity is None:
        # The pid definitely exists, but we could not establish *its*
        # exact identity — we can therefore neither confirm nor rule out
        # that it is the recorded process.
        return Liveness.UNKNOWN
    if recorded_start_iso is None:
        # The pid exists, but we have nothing to cross-check it against —
        # we cannot rule out pid reuse, so we cannot claim ALIVE.
        return Liveness.UNKNOWN
    recorded_identity = _parse_identity(recorded_start_iso)
    if recorded_identity is None:
        return Liveness.UNKNOWN
    if identity == recorded_identity:
        return Liveness.ALIVE
    # Same pid, a different exact identity: reused by a later process.
    return Liveness.GONE
