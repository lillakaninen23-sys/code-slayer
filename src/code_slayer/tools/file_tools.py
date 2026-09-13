"""Descriptor-relative file primitives. Only the executor authorizes writes."""

import hashlib
import os
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath

from code_slayer.tools.models import PatchHunk, ToolError

MAX_FILE_BYTES = 1024 * 1024


def relative_path(value: str, *, allow_root: bool = False) -> str:
    if allow_root and value == ".":
        return value
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
        raise ToolError("invalid_path")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ToolError("invalid_path") from exc
    parts = value.split("/")
    if any(part in ("", ".", "..") or part.lower() == ".git" for part in parts):
        raise ToolError("unsafe_path")
    return str(PurePosixPath(value))


def within(path: str, scope: str) -> bool:
    return PurePosixPath(path).is_relative_to(PurePosixPath(scope))


@contextmanager
def parent_fd(root: Path, path: str):
    relative_path(path)
    with ExitStack() as stack:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, fd)
        device = os.fstat(fd).st_dev
        for part in PurePosixPath(path).parts[:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            stack.callback(os.close, fd)
            if os.fstat(fd).st_dev != device:
                raise ToolError("mount_boundary")
            try:
                os.stat(".git", dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ToolError("nested_repository")
        yield fd, PurePosixPath(path).name


def inspect_leaf(parent: int, name: str):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_dev != os.fstat(parent).st_dev:
        raise ToolError("not_regular_local_file")
    return info


def read_bytes(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    remaining = MAX_FILE_BYTES + 1
    while remaining:
        data = os.read(fd, min(65536, remaining))
        if not data:
            break
        chunks.append(data)
        remaining -= len(data)
    data = b"".join(chunks)
    if len(data) > MAX_FILE_BYTES:
        raise ToolError("file_too_large")
    return data


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patch_bytes(content: bytes, hunks: tuple[PatchHunk, ...]) -> bytes:
    parts = []
    position = 0
    for hunk in hunks:
        if hunk.offset < position or hunk.offset > len(content):
            raise ToolError("invalid_patch_offsets")
        if content[hunk.offset:hunk.offset + len(hunk.before)] != hunk.before:
            raise ToolError("patch_context_mismatch")
        parts.extend((content[position:hunk.offset], hunk.after))
        position = hunk.offset + len(hunk.before)
    parts.append(content[position:])
    result = b"".join(parts)
    if len(result) > MAX_FILE_BYTES:
        raise ToolError("file_too_large")
    return result


def write_bytes(fd: int, data: bytes) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise OSError("short write")
        view = view[count:]
    os.ftruncate(fd, len(data))
    os.fsync(fd)
