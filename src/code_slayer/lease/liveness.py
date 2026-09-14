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

## Evidence must be semantically valid, not just syntactically parseable

A recorded identity string that merely *parses* into three integers is
not automatically a usable identity: `boot_time` must be a positive
epoch, `starttime_ticks` must be non-negative (Linux's own `starttime`
field is always >= 0, counted in clock ticks since boot), and
`clock_ticks_per_second` must be positive — these are Linux's own
invariants for these fields, not an arbitrary policy choice. A triple
violating any of them — `-1:-500:0`, say — is rejected as if it had
failed to parse at all, and this validation is applied identically to a
*recorded* identity (read back from a `worker_pid_started_at`/
`child_pid_started_at` column) and to the identity this module itself
just derived from a live `/proc` read: neither side gets to bypass it.
Rejected evidence is `UNKNOWN`, never a route to `GONE` — invalid data is
not proof of anything.

One direct consequence: an already-persisted lease whose recorded
identity predates this exact-identity scheme (the previous
implementation stored a formatted timestamp, not this raw triple, and an
even earlier one used a PID alone) can never be treated as a valid exact
identity, by construction — it fails to parse into three integers at all
in the overwhelming majority of cases, and even in the vanishing case
where it coincidentally did, it would still need to satisfy the
invariants above. Such a lease's `QUIESCING` review conservatively stays
`UNKNOWN` (never resolves to `EXPIRED` on its own) until an operator
resolves it explicitly (e.g. `release()` with independent, out-of-band
proof the old owner is gone) — this module never guess-converts old
evidence into a new exact identity, which would destroy the exact-identity
guarantee this hardening pass exists to provide. No schema migration is
needed or warranted to "fix" this: it is the intended, fail-closed
behavior for evidence this module cannot trust.

## `/proc` disappearing between two observations

Checking that `/proc` exists and then opening `/proc/<pid>/stat` are two
separate observations with a gap between them. If `/proc` itself became
unavailable in that gap, a `FileNotFoundError` opening `/proc/<pid>/stat`
no longer means *this pid* is absent — it means the evidence source
itself is gone, which proves nothing about the process. To close that
gap without a retry loop, every place that treats a missing-file error as
positive absence re-confirms `/proc` itself is still present *at that
exact moment* before concluding `GONE`; if `/proc` has also vanished by
then, the result is `UNKNOWN` instead. An ordinary, definite "no such
pid" while `/proc` is otherwise healthy is unaffected and still `GONE`.
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


def _is_valid_identity(identity: tuple[int, int, int]) -> bool:
    """Linux's own invariants for these fields — not an arbitrary policy
    choice: a boot time is always a positive epoch, `starttime` is always
    non-negative (clock ticks since boot), and a clock-tick rate is
    always positive. Applied identically to a freshly-read `/proc`
    identity and to a recorded one parsed back out of storage; neither
    is trusted merely for parsing as three integers."""
    boot, starttime_ticks, hz = identity
    return boot > 0 and starttime_ticks >= 0 and hz > 0


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
        # This normally means the pid is gone -- but only if /proc itself
        # is still the evidence source that told us so. Checking that
        # /proc exists and then opening this file are two separate
        # observations with a gap between them; re-confirm /proc is still
        # present *right now*, at the moment of this failure, rather than
        # trusting an earlier, now possibly stale, observation. If /proc
        # itself has also become unavailable, this proves nothing about
        # the specific pid.
        if not _has_proc():
            return _Existence.UNKNOWN, None
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
    identity = (boot, starttime_ticks, hz)
    if not _is_valid_identity(identity):
        # The pid definitely exists, but what we read back does not
        # satisfy Linux's own invariants for these fields -- corrupt or
        # unexpected evidence, never trusted as an identity.
        return _Existence.PRESENT, None
    return _Existence.PRESENT, identity


def _format_identity(identity: tuple[int, int, int]) -> str:
    boot, ticks, hz = identity
    return f"{boot}{_ID_SEP}{ticks}{_ID_SEP}{hz}"


def _parse_identity(value: str) -> tuple[int, int, int] | None:
    """Parse a recorded identity string back into `(boot, starttime_ticks,
    hz)` — `None` if it does not even have the right shape (this is also
    where a *legacy* recorded identity, from a previous implementation
    that stored a formatted timestamp or a bare pid, is rejected: it
    essentially never happens to split into exactly three integers, and
    even in the vanishing case it did, `_is_valid_identity` below still
    has to accept it) or if it fails Linux's own invariants for these
    fields. Never guess-converts old evidence into a new exact identity —
    that would destroy the exact-identity guarantee this parser exists to
    provide; a lease's `QUIESCING` review stays `UNKNOWN` here rather than
    ever resolving from data it cannot trust."""
    parts = value.split(_ID_SEP)
    if len(parts) != 3:
        return None
    try:
        identity = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    return identity if _is_valid_identity(identity) else None


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
