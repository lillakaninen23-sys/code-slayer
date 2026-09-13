"""Descriptor-relative file primitives: path safety, races, and bounds.

These exercise real filesystem behavior (real symlinks, real hardlinks,
real directory descriptors) rather than mocked exceptions, since the
properties claimed here — no symlink traversal, no ancestor-replacement
race, no nested-repository escape — depend on real OS semantics.
"""

from __future__ import annotations

import os

import pytest

from code_slayer.tools import file_tools as files
from code_slayer.tools.models import PatchHunk, ToolError

# --- relative_path -----------------------------------------------------

@pytest.mark.parametrize("value", [
    "", "\0abc", "a\\b", "../escape", "a/../b", "./a", "a/./b", "a/",
    ".git", "a/.GIT/x", "a/.Git", "/abs", "a//b",
])
def test_relative_path_rejects_unsafe_values(value):
    with pytest.raises(ToolError):
        files.relative_path(value)


def test_relative_path_root_requires_allow_root():
    with pytest.raises(ToolError):
        files.relative_path(".")
    assert files.relative_path(".", allow_root=True) == "."


def test_relative_path_accepts_normal_nested_path():
    assert files.relative_path("a/b/c.txt") == "a/b/c.txt"


def test_relative_path_rejects_non_string():
    with pytest.raises(ToolError):
        files.relative_path(123)  # type: ignore[arg-type]


def test_relative_path_rejects_surrogate_that_cannot_be_utf8_encoded():
    with pytest.raises(ToolError):
        files.relative_path("a/\udcff/b")


# --- within --------------------------------------------------------------

def test_within_is_component_aware_not_string_prefix():
    assert files.within("foo/bar.txt", "foo")
    assert not files.within("foobar.txt", "foo")
    assert files.within("a", ".")
    assert not files.within("a", "b")


# --- parent_fd: symlink / hardlink / nested-repo / ancestor-replacement --

def test_parent_fd_yields_leaf_name_and_parent_fd(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "file.txt").write_text("hi")
    with files.parent_fd(tmp_path, "sub/file.txt") as (parent, name):
        assert name == "file.txt"
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        assert info.st_size == 2


def test_parent_fd_rejects_nested_repository(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".git").mkdir()
    with pytest.raises(ToolError, match="nested_repository"):
        with files.parent_fd(tmp_path, "sub/dir/file.txt"):
            pass


def test_parent_fd_allows_git_dir_at_true_root(tmp_path):
    # The repo root itself legitimately has a .git; only descendants must
    # not introduce a second one.
    (tmp_path / ".git").mkdir()
    (tmp_path / "file.txt").write_text("hi")
    with files.parent_fd(tmp_path, "file.txt") as (parent, name):
        assert name == "file.txt"
        assert isinstance(parent, int)


def test_parent_fd_rejects_ancestor_replaced_by_symlink(tmp_path):
    root = tmp_path / "repo"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "dir").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "dir").mkdir()
    # Simulate a race: the intermediate ancestor is now a symlink escaping
    # the repo. O_NOFOLLOW on each directory-relative open must refuse it.
    import shutil

    shutil.rmtree(root / "sub")
    (root / "sub").symlink_to(outside)
    with pytest.raises(OSError):
        with files.parent_fd(root, "sub/dir/file.txt"):
            pass


def test_parent_fd_rejects_symlink_leaf(tmp_path):
    (tmp_path / "real.txt").write_text("secret")
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    with files.parent_fd(tmp_path, "link.txt") as (parent, name):
        with pytest.raises(ToolError, match="not_regular_local_file"):
            files.inspect_leaf(parent, name)


def test_inspect_leaf_rejects_directory(tmp_path):
    (tmp_path / "adir").mkdir()
    with files.parent_fd(tmp_path, "adir") as (parent, name):
        with pytest.raises(ToolError, match="not_regular_local_file"):
            files.inspect_leaf(parent, name)


def test_inspect_leaf_returns_none_for_missing_path(tmp_path):
    with files.parent_fd(tmp_path, "missing.txt") as (parent, name):
        assert files.inspect_leaf(parent, name) is None


def test_inspect_leaf_detects_hardlinks(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    os.link(tmp_path / "a.txt", tmp_path / "b.txt")
    with files.parent_fd(tmp_path, "a.txt") as (parent, name):
        info = files.inspect_leaf(parent, name)
        assert info.st_nlink == 2


# --- digest / patch_bytes / read_bytes / write_bytes ---------------------

def test_digest_is_sha256_hex():
    import hashlib
    assert files.digest(b"hello") == hashlib.sha256(b"hello").hexdigest()


def test_patch_bytes_applies_exact_context_hunk():
    content = b"hello world"
    hunk = PatchHunk(offset=6, before=b"world", after=b"there")
    assert files.patch_bytes(content, (hunk,)) == b"hello there"


def test_patch_bytes_rejects_context_mismatch():
    content = b"hello world"
    hunk = PatchHunk(offset=6, before=b"WORLD", after=b"there")
    with pytest.raises(ToolError, match="patch_context_mismatch"):
        files.patch_bytes(content, (hunk,))


def test_patch_bytes_rejects_out_of_order_or_out_of_bounds_hunks():
    content = b"0123456789"
    with pytest.raises(ToolError, match="invalid_patch_offsets"):
        files.patch_bytes(content, (
            PatchHunk(offset=5, before=b"5", after=b"x"),
            PatchHunk(offset=2, before=b"2", after=b"y"),
        ))
    with pytest.raises(ToolError, match="invalid_patch_offsets"):
        files.patch_bytes(content, (PatchHunk(offset=100, before=b"", after=b"x"),))


def test_patch_bytes_rejects_result_over_max_size(monkeypatch):
    monkeypatch.setattr(files, "MAX_FILE_BYTES", 4)
    with pytest.raises(ToolError, match="file_too_large"):
        files.patch_bytes(b"ab", (PatchHunk(offset=0, before=b"", after=b"toolong"),))


def test_read_bytes_enforces_max_size(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "MAX_FILE_BYTES", 4)
    path = tmp_path / "big.txt"
    path.write_bytes(b"0123456789")
    fd = os.open(path, os.O_RDONLY)
    try:
        with pytest.raises(ToolError, match="file_too_large"):
            files.read_bytes(fd)
    finally:
        os.close(fd)


def test_write_bytes_truncates_shorter_content(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"0123456789")
    fd = os.open(path, os.O_RDWR)
    try:
        files.write_bytes(fd, b"ab")
    finally:
        os.close(fd)
    assert path.read_bytes() == b"ab"
