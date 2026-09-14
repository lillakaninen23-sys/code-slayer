"""Best-effort process-liveness evidence for lease quiescence (Phase 6).

This module answers exactly one question, conservatively: *can we prove*
that the process which last held a lease (or spawned a still-tracked
child) is gone? It never answers "is this process the authoritative
owner" — the durable fencing generation remains the sole authority for
that (`lease.manager`). Liveness evidence only ever decides whether a
`QUIESCING` lease may safely advance to `EXPIRED`.

PID reuse means a bare `pid` is not proof of identity: after a process
exits, the OS is free to reuse its pid for something unrelated. To rule
this out, every recorded pid — a worker's own (`worker_pid`) and any
subprocess it spawned and journaled (`tool_operations.child_pid`) alike
— is paired with a start time (from `/proc`, not from our own wall
clock, which cannot reconstruct the OS's own allocation of that pid to
a specific process). `check_process_liveness` is used for both: it
compares the pid's *current* start time (if the pid exists at all right
now) against the *recorded* one:

- pid does not exist at all -> the recorded process is `GONE`.
- pid exists, current start time matches the recorded one (within a
  small tolerance for `/proc`'s tick-granularity rounding) -> `ALIVE`,
  the same process.
- pid exists, but its current start time does *not* match -> the pid has
  been reused by a later, different process; the *recorded* process is
  still `GONE`.
- anything we cannot determine (no `/proc` on this platform, permission
  denied, unparseable data, no recorded start time to compare against)
  -> `UNKNOWN`, never guessed at either way.

Only Linux's `/proc` is used; anywhere else this conservatively reports
`UNKNOWN` for every query rather than fabricate a answer.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from enum import StrEnum

_START_TIME_TOLERANCE_SECONDS = 2.0
_ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class Liveness(StrEnum):
    ALIVE = "ALIVE"
    GONE = "GONE"
    UNKNOWN = "UNKNOWN"


def _has_proc() -> bool:
    return os.path.isdir("/proc")


def _boot_time_epoch() -> float | None:
    try:
        with open("/proc/stat", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _proc_start_time_epoch(pid: int) -> float | None:
    """Epoch seconds `pid` actually started, read from `/proc/<pid>/stat`,
    or `None` if the pid does not currently exist, cannot be parsed, or
    `/proc` itself is unusable."""
    boot = _boot_time_epoch()
    if boot is None:
        return None
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    try:
        # `comm` (field 2) is parenthesized and may itself contain
        # spaces/parens; splitting on the *last* ')' skips past it safely
        # regardless of its contents.
        after_comm = raw.rsplit(b")", 1)[1]
        fields = after_comm.split()
        starttime_ticks = int(fields[19])  # field 22 overall (starttime)
    except (IndexError, ValueError):
        return None
    try:
        hz = os.sysconf("SC_CLK_TCK")
    except (ValueError, OSError):
        return None
    if hz <= 0:
        return None
    return boot + starttime_ticks / hz


def process_start_time(pid: int) -> str | None:
    """Best-effort ISO-8601 UTC start time of the *currently running*
    process at `pid`, for later liveness/reuse cross-referencing — `None`
    if unavailable (no `/proc`, pid not found, unparseable)."""
    if not _has_proc():
        return None
    epoch = _proc_start_time_epoch(pid)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, UTC).strftime(_ISO_FORMAT)


def _parse_iso_epoch(value: str) -> float | None:
    try:
        return datetime.strptime(value, _ISO_FORMAT).replace(tzinfo=UTC).timestamp()
    except (ValueError, TypeError):
        return None


def check_process_liveness(pid: int | None, recorded_start_iso: str | None) -> Liveness:
    """Reuse-safe liveness check: `ALIVE` only if `pid` exists right now
    *and* its current start time matches `recorded_start_iso`."""
    if pid is None:
        return Liveness.UNKNOWN
    if not _has_proc():
        return Liveness.UNKNOWN
    current_epoch = _proc_start_time_epoch(pid)
    if current_epoch is None:
        # `/proc/<pid>` does not exist (or is unreadable in a way that
        # looks the same): the process this pid could refer to is gone.
        return Liveness.GONE
    if recorded_start_iso is None:
        # The pid exists, but we have nothing to cross-check it against —
        # we cannot rule out pid reuse, so we cannot claim ALIVE.
        return Liveness.UNKNOWN
    recorded_epoch = _parse_iso_epoch(recorded_start_iso)
    if recorded_epoch is None:
        return Liveness.UNKNOWN
    if abs(current_epoch - recorded_epoch) <= _START_TIME_TOLERANCE_SECONDS:
        return Liveness.ALIVE
    # Same pid, different (later) start time: reused by another process.
    return Liveness.GONE
