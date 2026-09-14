"""Unit tests for `lease.liveness`'s `GONE`/`UNKNOWN`/`ALIVE` decision
table and exact (tolerance-free) process-identity comparison.

Every failure mode is simulated deterministically by monkeypatching this
module's own `/proc` I/O — never by manipulating real, unrelated OS
processes."""

from __future__ import annotations

import io

import pytest

from code_slayer.lease import liveness
from code_slayer.lease.liveness import Liveness, check_process_liveness, process_start_time

_PID = 4321
_STAT_PATH = f"/proc/{_PID}/stat"
_PROC_STAT_PATH = "/proc/stat"
_VALID_BOOT_LINE = "btime 1700000000\n"


def _stat_bytes(starttime_ticks: int) -> bytes:
    """A syntactically valid `/proc/<pid>/stat` line whose `starttime`
    field (the 20th field after the parenthesized `comm`) is exactly
    `starttime_ticks`."""
    fields_after_comm = [
        "S", "1", str(_PID), str(_PID), "0", "-1", "4194304",
        "0", "0", "0", "0", "0", "0", "0", "0", "20", "0", "1", "0", str(starttime_ticks),
    ]
    return f"{_PID} (prog) ".encode() + " ".join(fields_after_comm).encode()


class _FakeOpen:
    """Routes exact paths to canned content or an exception; anything
    else raises loudly rather than silently touching the real filesystem."""

    def __init__(self, routes: dict[str, bytes | str | BaseException]):
        self._routes = routes

    def __call__(self, path, mode="r", *_a, **_kw):
        if path not in self._routes:
            raise AssertionError(f"unexpected open() in a liveness test: {path!r}")
        value = self._routes[path]
        if isinstance(value, BaseException):
            raise value
        if "b" in mode:
            return io.BytesIO(value if isinstance(value, bytes) else value.encode())
        return io.StringIO(value if isinstance(value, str) else value.decode())


def _patch_open(monkeypatch, routes: dict[str, bytes | str | BaseException]) -> None:
    """`open` is not normally a module attribute of `liveness` (it
    resolves through builtins) — `raising=False` lets monkeypatch install
    it as one anyway, shadowing the builtin for calls made from inside
    that module only; nothing elsewhere is affected."""
    monkeypatch.setattr(liveness, "open", _FakeOpen(routes), raising=False)


@pytest.fixture
def has_proc(monkeypatch):
    monkeypatch.setattr(liveness, "_has_proc", lambda: True)


def _identity(boot: int, ticks: int, hz: int) -> str:
    return f"{boot}:{ticks}:{hz}"


# --- positive evidence of absence -> GONE -----------------------------

def test_pid_definitely_absent_enoent_is_gone(monkeypatch, has_proc):
    _patch_open(monkeypatch, {_STAT_PATH: FileNotFoundError()})
    assert check_process_liveness(_PID, _identity(1700000000, 999, 100)) == Liveness.GONE


def test_pid_definitely_absent_esrch_style_is_gone(monkeypatch, has_proc):
    _patch_open(monkeypatch, {_STAT_PATH: ProcessLookupError()})
    assert check_process_liveness(_PID, None) == Liveness.GONE
    assert process_start_time(_PID) is None


# --- failure to read/parse evidence -> UNKNOWN, never GONE -------------

def test_pid_permission_denied_is_unknown_not_gone(monkeypatch, has_proc):
    _patch_open(monkeypatch, {_STAT_PATH: PermissionError()})
    assert check_process_liveness(_PID, _identity(1700000000, 999, 100)) == Liveness.UNKNOWN


def test_pid_stat_unreadable_other_oserror_is_unknown_not_gone(monkeypatch, has_proc):
    _patch_open(monkeypatch, {_STAT_PATH: OSError("device not ready")})
    assert check_process_liveness(_PID, _identity(1700000000, 999, 100)) == Liveness.UNKNOWN


def test_malformed_pid_stat_content_is_unknown_not_gone(monkeypatch, has_proc):
    """The pid is definitely present (its /proc entry was read) but the
    content could not be parsed into an identity — present, but not
    provably the recorded process, and not provably a different one."""
    _patch_open(monkeypatch, {_STAT_PATH: b"garbage with no comm parens at all"})
    assert check_process_liveness(_PID, _identity(1700000000, 999, 100)) == Liveness.UNKNOWN
    assert process_start_time(_PID) is None


def test_pid_stat_too_few_fields_is_unknown_not_gone(monkeypatch, has_proc):
    _patch_open(monkeypatch, {_STAT_PATH: f"{_PID} (prog) S 1".encode()})
    assert check_process_liveness(_PID, _identity(1700000000, 999, 100)) == Liveness.UNKNOWN


def test_boot_time_unreadable_is_unknown_not_gone(monkeypatch, has_proc):
    """The pid's own stat is perfectly readable (it definitely exists),
    but the system boot time could not be read -- identity cannot be
    established, which must not be mistaken for the process being gone."""
    _patch_open(monkeypatch, {
        _STAT_PATH: _stat_bytes(999),
        _PROC_STAT_PATH: OSError("cannot read /proc/stat"),
    })
    assert check_process_liveness(_PID, _identity(1700000000, 999, 100)) == Liveness.UNKNOWN
    assert process_start_time(_PID) is None


def test_boot_time_line_missing_is_unknown_not_gone(monkeypatch, has_proc):
    _patch_open(monkeypatch, {
        _STAT_PATH: _stat_bytes(999),
        _PROC_STAT_PATH: "cpu 0 0 0 0\n",  # no "btime " line at all
    })
    assert check_process_liveness(_PID, _identity(1700000000, 999, 100)) == Liveness.UNKNOWN


def test_clock_tick_rate_unreadable_is_unknown_not_gone(monkeypatch, has_proc):
    _patch_open(monkeypatch, {
        _STAT_PATH: _stat_bytes(999),
        _PROC_STAT_PATH: _VALID_BOOT_LINE,
    })
    monkeypatch.setattr(liveness, "_clock_ticks_per_second", lambda: None)
    assert check_process_liveness(_PID, _identity(1700000000, 999, 100)) == Liveness.UNKNOWN
    assert process_start_time(_PID) is None


def test_no_proc_on_this_platform_is_unknown(monkeypatch):
    monkeypatch.setattr(liveness, "_has_proc", lambda: False)
    assert check_process_liveness(_PID, _identity(1700000000, 999, 100)) == Liveness.UNKNOWN
    assert process_start_time(_PID) is None


def test_no_pid_given_is_unknown(has_proc):
    assert check_process_liveness(None, _identity(1700000000, 999, 100)) == Liveness.UNKNOWN


def test_no_recorded_identity_is_unknown_even_though_pid_exists(monkeypatch, has_proc):
    _patch_open(monkeypatch, {
        _STAT_PATH: _stat_bytes(999),
        _PROC_STAT_PATH: _VALID_BOOT_LINE,
    })
    assert check_process_liveness(_PID, None) == Liveness.UNKNOWN


def test_malformed_recorded_identity_string_is_unknown(monkeypatch, has_proc):
    _patch_open(monkeypatch, {
        _STAT_PATH: _stat_bytes(999),
        _PROC_STAT_PATH: _VALID_BOOT_LINE,
    })
    assert check_process_liveness(_PID, "not-an-identity") == Liveness.UNKNOWN
    assert check_process_liveness(_PID, "1:2") == Liveness.UNKNOWN
    assert check_process_liveness(_PID, "1:2:3:4") == Liveness.UNKNOWN
    assert check_process_liveness(_PID, "a:b:c") == Liveness.UNKNOWN


# --- definite presence + exact identity match/mismatch ------------------

def test_exact_identity_match_is_alive(monkeypatch, has_proc):
    _patch_open(monkeypatch, {
        _STAT_PATH: _stat_bytes(999),
        _PROC_STAT_PATH: _VALID_BOOT_LINE,
    })
    monkeypatch.setattr(liveness, "_clock_ticks_per_second", lambda: 100)
    recorded = _identity(1700000000, 999, 100)
    assert check_process_liveness(_PID, recorded) == Liveness.ALIVE


def test_one_tick_off_is_gone_not_alive_no_tolerance(monkeypatch, has_proc):
    """The old ~2-second tolerance is gone: even a single tick of
    difference in the recorded identity means a different process, not
    "close enough" to the current one."""
    _patch_open(monkeypatch, {
        _STAT_PATH: _stat_bytes(999),
        _PROC_STAT_PATH: _VALID_BOOT_LINE,
    })
    monkeypatch.setattr(liveness, "_clock_ticks_per_second", lambda: 100)
    recorded_one_tick_earlier = _identity(1700000000, 998, 100)
    assert check_process_liveness(_PID, recorded_one_tick_earlier) == Liveness.GONE


def test_within_old_two_second_window_is_still_gone_not_alive(monkeypatch, has_proc):
    """A recorded identity 199 ticks (1.99s at 100Hz) earlier than the
    current one -- comfortably inside the old, now-removed ~2-second
    tolerance window -- must still be GONE, not ALIVE."""
    _patch_open(monkeypatch, {
        _STAT_PATH: _stat_bytes(1000),
        _PROC_STAT_PATH: _VALID_BOOT_LINE,
    })
    monkeypatch.setattr(liveness, "_clock_ticks_per_second", lambda: 100)
    recorded_almost_two_seconds_earlier = _identity(1700000000, 801, 100)
    assert check_process_liveness(_PID, recorded_almost_two_seconds_earlier) == Liveness.GONE


def test_different_boot_same_ticks_is_gone_not_alive(monkeypatch, has_proc):
    """A reboot resets the tick counter; the same raw tick count after a
    different boot is a different process, never treated as the same."""
    _patch_open(monkeypatch, {
        _STAT_PATH: _stat_bytes(999),
        _PROC_STAT_PATH: _VALID_BOOT_LINE,
    })
    monkeypatch.setattr(liveness, "_clock_ticks_per_second", lambda: 100)
    recorded_different_boot = _identity(1699999999, 999, 100)
    assert check_process_liveness(_PID, recorded_different_boot) == Liveness.GONE


def test_process_start_time_round_trips_into_check_process_liveness(monkeypatch, has_proc):
    _patch_open(monkeypatch, {
        _STAT_PATH: _stat_bytes(12345),
        _PROC_STAT_PATH: _VALID_BOOT_LINE,
    })
    monkeypatch.setattr(liveness, "_clock_ticks_per_second", lambda: 100)
    recorded = process_start_time(_PID)
    assert recorded == "1700000000:12345:100"
    assert check_process_liveness(_PID, recorded) == Liveness.ALIVE
