"""Subprocess security model: argv-only execution, sanitized environment,
bounded output, timeout, and PID handling.

These use real subprocesses (never mocked) because the properties under
test — a process is actually killed, output is actually bounded, a timeout
actually fires — are properties of the real OS, not of this module's code.
"""

from __future__ import annotations

import os
import signal
import sys
import time

import pytest

from code_slayer.repo import identity
from code_slayer.repo.inspection import inspect_repository
from code_slayer.tools.command_tools import (
    GIT_EXECUTABLE,
    CommandRunner,
    _capture,
    environment,
    validate_command,
)
from code_slayer.tools.models import CommandRequest, ToolError
from tests.repo_helpers import git


def make_request(**overrides):
    base = dict(profile="git_rev_parse", executable="git", argv=("HEAD",),
                cwd=".", timeout=3.0, output_limit=65536)
    base.update(overrides)
    return CommandRequest(**base)


# --- environment sanitization --------------------------------------------

def test_environment_never_forwards_process_environment(monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "sk-should-never-leak")
    monkeypatch.setenv("GIT_SSH_COMMAND", "attacker-controlled")
    env = environment()
    assert "SUPER_SECRET_TOKEN" not in env
    assert env["PATH"] == os.defpath
    assert env["HOME"] == "/nonexistent"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    # A fixed, closed environment: nothing from os.environ is merged in.
    assert set(env) == {
        "PATH", "HOME", "LC_ALL", "TZ", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
        "GIT_ALLOW_PROTOCOL", "GIT_NO_LAZY_FETCH", "GIT_TERMINAL_PROMPT",
        "GIT_OPTIONAL_LOCKS", "GIT_ATTR_NOSYSTEM",
    }


# --- validate_command: fails closed on every malformed shape --------------

@pytest.mark.parametrize("overrides", [
    {"profile": "shell"}, {"executable": "bash"}, {"cwd": "/tmp"},
    {"argv": ["HEAD"]}, {"argv": ()}, {"argv": ("HEAD", "extra")},
    {"argv": (123,)}, {"argv": ("",)}, {"argv": ("a\0b",)},
    {"argv": ("x" * 5000,)}, {"timeout": 0}, {"timeout": -1}, {"timeout": 11},
    {"timeout": "3"}, {"output_limit": 0}, {"output_limit": 10**7},
    {"output_limit": 1.5},
])
def test_validate_command_rejects_malformed_shapes(overrides):
    with pytest.raises(ToolError):
        validate_command(make_request(**overrides))


def test_validate_command_rejects_non_command_request():
    with pytest.raises(ToolError, match="malformed_command"):
        validate_command("HEAD")  # type: ignore[arg-type]


def test_validate_command_accepts_well_formed_request():
    validate_command(make_request())  # must not raise


# --- _capture: real subprocess lifecycle ----------------------------------

def test_capture_returns_definite_output_and_records_pid():
    spawned = []
    output = _capture(
        [sys.executable, "-c", "print('hello')"],
        cwd=".", timeout=5, limit=65536, on_spawn=spawned.append,
    )
    assert output.stdout == b"hello\n"
    assert output.returncode == 0
    assert not output.truncated and not output.timed_out
    assert len(spawned) == 1
    # The process must have actually been reaped; its pid is not runnable.
    with pytest.raises(ProcessLookupError):
        os.kill(spawned[0], 0)


def test_capture_enforces_timeout_and_kills_the_child():
    spawned = []
    start = time.monotonic()
    output = _capture(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=".", timeout=0.5, limit=65536, on_spawn=spawned.append,
    )
    elapsed = time.monotonic() - start
    assert output.timed_out
    assert elapsed < 10, "the child must be killed promptly, not waited out"
    assert spawned, "on_spawn must fire before the timeout is enforced"
    # A killed child's pid is no longer signalable once reaped.
    with pytest.raises(ProcessLookupError):
        os.kill(spawned[0], 0)


def test_capture_bounds_output_and_kills_the_child():
    spawned = []
    output = _capture(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()"
         "; import time; time.sleep(30)"],
        cwd=".", timeout=5, limit=1024, on_spawn=spawned.append,
    )
    assert output.truncated
    assert len(output.stdout) <= 1024
    with pytest.raises(ProcessLookupError):
        os.kill(spawned[0], 0)


def test_capture_zero_or_negative_timeout_fails_closed():
    with pytest.raises(ToolError, match="command_timeout"):
        _capture(["true"], cwd=".", timeout=0, limit=1024, on_spawn=lambda pid: None)


def test_capture_runs_in_its_own_process_group():
    """start_new_session=True: killing the reported pid's process group
    must not require finding descendants separately."""
    pgids = []

    def record(pid):
        pgids.append(os.getpgid(pid))

    output = _capture(
        [sys.executable, "-c", "import os; print(os.getpgid(0) == os.getpid())"],
        cwd=".", timeout=5, limit=1024, on_spawn=record,
    )
    assert output.stdout.strip() == b"True"
    assert pgids and pgids[0] != os.getpgid(0)


# --- CommandRunner.run(): only a single literal revision argument ---------

@pytest.fixture
def repo(git_repo_with_commit):
    return git_repo_with_commit


def test_run_resolves_head_successfully(repo):
    runner = CommandRunner(repo, timeout=5, on_spawn=lambda pid: None)
    request = make_request(argv=("HEAD",))
    output = runner.run(request)
    assert output.returncode == 0
    assert output.stdout.decode().strip() == git(repo, "rev-parse", "HEAD")


def test_run_nonexistent_revision_fails_deterministically(repo):
    runner = CommandRunner(repo, timeout=5, on_spawn=lambda pid: None)
    request = make_request(argv=("not-a-real-ref-xyz",))
    output = runner.run(request)
    assert output.returncode != 0
    assert not output.timed_out and not output.truncated


def test_run_treats_dash_prefixed_argument_as_literal_not_an_option(repo):
    """--end-of-options must stop option parsing: a revision string that
    looks like a flag must never be interpreted as one."""
    runner = CommandRunner(repo, timeout=5, on_spawn=lambda pid: None)
    request = make_request(argv=("--upload-pack=/bin/sh",))
    output = runner.run(request)
    assert output.returncode != 0  # rejected as an invalid revision, not executed


def test_git_executable_is_resolved_once_from_a_fixed_path():
    assert GIT_EXECUTABLE is not None
    assert os.path.isabs(GIT_EXECUTABLE)


# --- CommandRunner.verify_identity ----------------------------------------

@pytest.fixture
def inspection(repo):
    identity.resolve(repo)
    return inspect_repository(repo, establish_identity=False)


def test_verify_identity_succeeds_for_the_real_repo(repo, inspection):
    runner = CommandRunner(repo, timeout=5, on_spawn=lambda pid: None)
    runner.verify_identity(inspection.metadata(), mutation=False, resource="README.md")


def test_verify_identity_rejects_repo_id_mismatch(repo, inspection):
    runner = CommandRunner(repo, timeout=5, on_spawn=lambda pid: None)
    tampered = {**inspection.metadata(), "repo_id": "not-the-real-repo-id"}
    with pytest.raises(ToolError, match="identity_mismatch"):
        runner.verify_identity(tampered, mutation=False, resource="README.md")


def test_verify_identity_rejects_worktree_id_mismatch(repo, inspection):
    runner = CommandRunner(repo, timeout=5, on_spawn=lambda pid: None)
    tampered = {**inspection.metadata(), "worktree_id": "not-the-real-worktree-id"}
    with pytest.raises(ToolError, match="identity_mismatch"):
        runner.verify_identity(tampered, mutation=False, resource="README.md")


def test_verify_identity_detects_head_moved_since_baseline(repo, inspection):
    runner = CommandRunner(repo, timeout=5, on_spawn=lambda pid: None)
    (repo / "new.txt").write_text("x")
    git(repo, "add", "new.txt")
    git(repo, "commit", "-qm", "moved head")
    with pytest.raises(ToolError, match="baseline_head_changed"):
        runner.verify_identity(inspection.metadata(), mutation=True, resource="untracked.txt")


def test_verify_identity_rejects_mutation_of_a_tracked_path(repo, inspection):
    runner = CommandRunner(repo, timeout=5, on_spawn=lambda pid: None)
    with pytest.raises(ToolError, match="path_is_tracked_or_index_unavailable"):
        runner.verify_identity(inspection.metadata(), mutation=True, resource="README.md")


def test_verify_identity_allows_mutation_of_an_untracked_path(repo, inspection):
    runner = CommandRunner(repo, timeout=5, on_spawn=lambda pid: None)
    runner.verify_identity(inspection.metadata(), mutation=True, resource="brand-new.txt")


def test_verify_identity_rejects_when_git_dir_is_not_canonical(repo, inspection):
    runner = CommandRunner(repo, timeout=5, on_spawn=lambda pid: None)
    tampered = {**inspection.metadata(), "git_dir": str(repo / "sub" / ".." / ".git")}
    with pytest.raises(ToolError, match="identity_mismatch"):
        runner.verify_identity(tampered, mutation=False, resource="README.md")


def test_capture_signal_terminate_uses_sigkill_on_process_group(monkeypatch):
    # _terminate must not depend on a well-behaved child honoring SIGTERM.
    from code_slayer.tools import command_tools

    seen = {}
    real_killpg = os.killpg

    def spy(pgid, sig):
        seen["sig"] = sig
        return real_killpg(pgid, sig)

    monkeypatch.setattr(command_tools.os, "killpg", spy)
    _capture(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=".", timeout=0.3, limit=1024, on_spawn=lambda pid: None,
    )
    assert seen["sig"] == signal.SIGKILL
