"""One read-only Git profile; bounded processes and an explicit environment."""

import os
import selectors
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from code_slayer.tools.models import CommandRequest, ToolError

# Never search the user's PATH or accept an executable supplied by a request.
GIT_EXECUTABLE = shutil.which("git", path=os.defpath)


def environment() -> dict[str, str]:
    return {
        "PATH": os.defpath, "HOME": "/nonexistent", "LC_ALL": "C", "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_ATTR_NOSYSTEM": "1",
    }


@dataclass(frozen=True)
class CommandOutput:
    stdout: bytes
    stderr: bytes
    returncode: int
    truncated: bool = False
    timed_out: bool = False


def validate_command(request: CommandRequest) -> None:
    if not isinstance(request, CommandRequest):
        raise ToolError("malformed_command")
    if request.profile != "git_rev_parse" or request.executable != "git":
        raise ToolError("command_not_allowlisted")
    if request.cwd != ".":
        raise ToolError("command_cwd_must_be_root")
    if type(request.argv) is not tuple or len(request.argv) != 1:
        raise ToolError("malformed_command_argv")
    if not isinstance(request.argv[0], str) or not request.argv[0] or "\0" in request.argv[0]:
        raise ToolError("malformed_command_argv")
    try:
        if len(request.argv[0].encode("utf-8")) > 4096:
            raise ToolError("malformed_command_argv")
    except UnicodeError as exc:
        raise ToolError("malformed_command_argv") from exc
    if type(request.timeout) not in (int, float) or not 0 < request.timeout <= 10:
        raise ToolError("invalid_timeout")
    if type(request.output_limit) is not int or not 1 <= request.output_limit <= 262144:
        raise ToolError("invalid_output_limit")


def _capture(argv, *, cwd, timeout, limit, on_spawn) -> CommandOutput:
    if timeout <= 0:
        raise ToolError("command_timeout")
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        argv, cwd=str(cwd), env=environment(), shell=False, stdin=subprocess.DEVNULL,
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
    return CommandOutput(
        bytes(buffers[0]), bytes(buffers[1]), process.returncode, truncated, timed_out,
    )


def _terminate(process) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=2)


class CommandRunner:
    def __init__(self, root: Path, *, timeout: float, on_spawn) -> None:
        self.root = root
        self.deadline = time.monotonic() + timeout
        self.on_spawn = on_spawn

    def _git(self, argv, *, limit=65536) -> CommandOutput:
        if GIT_EXECUTABLE is None:
            raise ToolError("trusted_git_unavailable")
        return _capture(
            [GIT_EXECUTABLE, "--no-pager", "--no-optional-locks", "-c", "core.fsmonitor=false",
             "-c", "core.hooksPath=" + os.devnull, *argv],
            cwd=self.root, timeout=self.deadline - time.monotonic(), limit=limit,
            on_spawn=self.on_spawn,
        )

    def verify_identity(self, metadata: dict, *, mutation: bool, resource: str) -> None:
        for argv, expected in (
            (["rev-parse", "--show-toplevel"], metadata["repo_root"]),
            (["rev-parse", "--absolute-git-dir"], metadata["git_dir"]),
            (["config", "--local", "--get", "codeslayer.repo-id"], metadata["repo_id"]),
        ):
            result = self._git(argv)
            if result.returncode or result.truncated or result.timed_out:
                raise ToolError("identity_unavailable")
            if result.stdout.decode("utf-8").rstrip("\n") != expected:
                raise ToolError("identity_mismatch")
        directory = Path(metadata["git_dir"])
        if directory.resolve() != directory:
            raise ToolError("identity_mismatch")
        fd = os.open(directory / "codeslayer-id", os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if os.read(fd, 256).decode("utf-8").strip() != metadata["worktree_id"]:
                raise ToolError("identity_mismatch")
        finally:
            os.close(fd)
        if mutation:
            result = self._git(["rev-parse", "--verify", "-q", "HEAD"])
            if result.truncated or result.timed_out or result.returncode not in (0, 1):
                raise ToolError("baseline_head_unavailable")
            head = result.stdout.decode("ascii").strip() if result.returncode == 0 else None
            if head != metadata["head"]:
                raise ToolError("baseline_head_changed")
            result = self._git(["ls-files", "--error-unmatch", "--", resource])
            if result.returncode != 1 or result.truncated or result.timed_out:
                raise ToolError("path_is_tracked_or_index_unavailable")

    def run(self, request: CommandRequest) -> CommandOutput:
        validate_command(request)
        # Revision is a single argv datum after --end-of-options. No shell
        # metacharacter matching, executable lookup, or user option expansion.
        return self._git(
            ["rev-parse", "--verify", "--end-of-options", request.argv[0]],
            limit=request.output_limit,
        )
