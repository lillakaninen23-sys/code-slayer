"""Narrow Git plumbing for durable checkpoints.

Unlike `repo/git.py` (reads and one narrowly-scoped `git config --local`
write) and `tools/command_tools.py` (one user-facing read-only revision
lookup), this module performs the specific, fixed-shape plumbing writes a
checkpoint needs: writing blob/tree/commit objects and moving exactly one
dedicated ref per checkpoint. It never touches the repository's real
index, `HEAD`, or any user branch — every index-shaped operation is
redirected to a private, temporary index file via `GIT_INDEX_FILE`, and
`commit-tree`/`update-ref` do not consult the index or `HEAD` at all.

No shell, no string interpolation, no untrusted argv content: every
argument here is either a fixed literal, a path/SHA this module produced
by calling `git` itself, or a byte string piped through stdin (never
passed as an argv token). Bounded timeout, no network, no inherited
process environment.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# The SHA-1 empty-tree object id is a Git constant (the same in every
# repository, independent of content) — not computed from local data.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_TIMEOUT = 15.0


class CheckpointGitError(RuntimeError):
    """A checkpoint plumbing command failed or returned an unexpected shape."""


def _environment(*, index_path: Path | None = None) -> dict[str, str]:
    env = {
        "PATH": os.defpath, "HOME": "/nonexistent", "LC_ALL": "C", "TZ": "UTC",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ALLOW_PROTOCOL": "", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0", "GIT_ATTR_NOSYSTEM": "1",
        # Fixed, explicit commit identity: this is Code Slayer's own
        # object, never impersonating the repository's configured user.
        "GIT_AUTHOR_NAME": "Code Slayer", "GIT_AUTHOR_EMAIL": "codeslayer@localhost",
        "GIT_COMMITTER_NAME": "Code Slayer", "GIT_COMMITTER_EMAIL": "codeslayer@localhost",
    }
    if index_path is not None:
        env["GIT_INDEX_FILE"] = str(index_path)
    return env


def _run(
    argv: list[str], *, cwd: Path, input_bytes: bytes | None = None,
    index_path: Path | None = None,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "--no-pager", "--no-optional-locks", *argv],
            cwd=str(cwd), input=input_bytes, capture_output=True, shell=False,
            env=_environment(index_path=index_path), timeout=_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise CheckpointGitError(f"git {argv[0]} timed out") from exc


def utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def object_exists(sha: str, *, cwd: Path) -> bool:
    """Whether `sha` names an object actually present in the object database."""
    result = _run(["cat-file", "-e", sha], cwd=cwd)
    return result.returncode == 0


def read_commit(commit_sha: str, *, cwd: Path) -> tuple[str, tuple[str, ...]]:
    """Read a commit object's own tree and parent ids back from the object
    database — ground truth for recovery, never re-derived from anything
    Code Slayer merely intended to write."""
    result = _run(["cat-file", "-p", commit_sha], cwd=cwd)
    if result.returncode != 0:
        raise CheckpointGitError(f"cannot read commit object {commit_sha!r}")
    tree_sha: str | None = None
    parents: list[str] = []
    for line in result.stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("tree "):
            tree_sha = line[len("tree "):].strip()
        elif line.startswith("parent "):
            parents.append(line[len("parent "):].strip())
        elif line == "" or not line[:1].isalpha():
            break
    if tree_sha is None:
        raise CheckpointGitError(f"commit object {commit_sha!r} has no tree header")
    return tree_sha, tuple(parents)


def resolve_tree_sha(commit_ish: str | None, *, cwd: Path) -> str:
    """The tree object id of `commit_ish`, or the empty tree if `None`
    (an unborn-HEAD baseline)."""
    if commit_ish is None:
        return EMPTY_TREE_SHA
    result = _run(["rev-parse", "--verify", "-q", f"{commit_ish}^{{tree}}"], cwd=cwd)
    if result.returncode != 0:
        raise CheckpointGitError(f"cannot resolve tree of {commit_ish!r}")
    return result.stdout.decode("ascii").strip()


def hash_blob(data: bytes, *, cwd: Path) -> str:
    """Write `data` as a blob object and return its content id.

    Bytes travel over stdin, never as an argv token or a path Git itself
    reads from disk — this hashes exactly the bytes the caller already
    read and verified, not whatever currently happens to be at a path.
    """
    result = _run(["hash-object", "-w", "-t", "blob", "--stdin"], cwd=cwd, input_bytes=data)
    if result.returncode != 0:
        raise CheckpointGitError("git hash-object failed")
    return result.stdout.decode("ascii").strip()


@dataclass(frozen=True)
class TreeEdit:
    path: str
    blob_sha: str | None  # None means "remove this path from the tree"


def build_tree(
    base_tree_sha: str, edits: tuple[TreeEdit, ...], *, cwd: Path, index_path: Path,
) -> str:
    """Return the tree id of `base_tree_sha` with `edits` applied.

    Uses a private, temporary index file (`GIT_INDEX_FILE`) that is
    created fresh here and never the repository's real `.git/index` — the
    user's actual staged intent is never read, touched, or replaced.
    """
    for edit in edits:
        # Defense in depth, independent of whatever validated `edit.path`
        # before it reached here: an embedded control character would
        # split one `--index-info` line into two, letting a path smuggle
        # in an unrelated extra tree entry of its own choosing.
        if any(ord(char) < 0x20 for char in edit.path):
            raise CheckpointGitError(f"unsafe path in checkpoint tree edit: {edit.path!r}")
    if index_path.exists():
        index_path.unlink()
    read = _run(["read-tree", base_tree_sha], cwd=cwd, index_path=index_path)
    if read.returncode != 0:
        raise CheckpointGitError("git read-tree failed")
    additions = "".join(
        f"100644 {edit.blob_sha}\t{edit.path}\n" for edit in edits if edit.blob_sha is not None
    )
    if additions:
        result = _run(
            ["update-index", "--index-info"], cwd=cwd,
            input_bytes=additions.encode("utf-8"), index_path=index_path,
        )
        if result.returncode != 0:
            raise CheckpointGitError("git update-index (add) failed")
    for edit in edits:
        if edit.blob_sha is None:
            result = _run(
                ["update-index", "--force-remove", "--", edit.path],
                cwd=cwd, index_path=index_path,
            )
            if result.returncode != 0:
                raise CheckpointGitError("git update-index (remove) failed")
    result = _run(["write-tree"], cwd=cwd, index_path=index_path)
    if result.returncode != 0:
        raise CheckpointGitError("git write-tree failed")
    return result.stdout.decode("ascii").strip()


def commit_tree(tree_sha: str, parents: tuple[str, ...], message: str, *, cwd: Path) -> str:
    """Create a commit object with an explicit tree/parents; touches no ref."""
    argv = ["commit-tree", tree_sha]
    for parent in parents:
        argv.extend(["-p", parent])
    argv.extend(["-m", message])
    result = _run(argv, cwd=cwd)
    if result.returncode != 0:
        raise CheckpointGitError("git commit-tree failed")
    return result.stdout.decode("ascii").strip()


def resolve_ref(ref: str, *, cwd: Path) -> str | None:
    """The commit id `ref` currently points at, or `None` if it does not exist."""
    result = _run(["rev-parse", "--verify", "-q", ref], cwd=cwd)
    if result.returncode != 0:
        return None
    return result.stdout.decode("ascii").strip()


def create_ref(ref: str, sha: str, *, cwd: Path) -> None:
    """Atomically create `ref` pointing at `sha`; fails if it already exists.

    `update-ref <ref> <new> ''` is Git's own compare-and-swap: the empty
    old-value means "must not already exist" — this never force-moves a
    ref, and never touches `HEAD` or any branch.
    """
    result = _run(["update-ref", ref, sha, ""], cwd=cwd)
    if result.returncode != 0:
        raise CheckpointGitError(f"git update-ref failed to create {ref!r}")
