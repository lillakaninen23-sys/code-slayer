"""Read-only Git observations; no target content is copied into metadata."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

from code_slayer.audit.canonical import canonical_json
from code_slayer.repo import git, identity
from code_slayer.store.db import utcnow_iso


class InspectionError(RuntimeError):
    """Inspection cannot safely describe this repository."""


class RepositoryChangedError(InspectionError):
    """The observed metadata changed during capture."""


def safe_path(raw: bytes) -> str:
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InspectionError("non-UTF-8 Git path cannot be stored in schema v1 TEXT") from exc
    parts = path.rstrip("/").split("/")
    if not path or any(p in ("", ".", "..") or p.lower() == ".git" for p in parts):
        raise InspectionError("unsafe repository-relative Git path")
    return path


@dataclass(frozen=True)
class PathChange:
    path: str
    index_status: str
    worktree_status: str
    kind: str = "tracked"
    original_path: str | None = None
    similarity: str | None = None
    submodule: str = "N..."


@dataclass(frozen=True)
class IndexEntry:
    path: str
    mode: str
    object_id: str
    stage: int


@dataclass(frozen=True)
class Gitlink:
    path: str
    head_object_id: str | None
    index_object_ids: tuple[str, ...]


@dataclass(frozen=True)
class RepositoryInspection:
    repo_root: str
    repo_id: str
    worktree_id: str
    git_dir: str
    git_common_dir: str
    worktree_dir: str
    head: str | None
    branch: str | None
    detached: bool
    unborn: bool
    changes: tuple[PathChange, ...]
    index_entries: tuple[IndexEntry, ...]
    untracked_paths: tuple[str, ...]
    ignored_paths: tuple[str, ...]
    masked_paths: tuple[str, ...]
    gitlinks: tuple[Gitlink, ...]
    operation_markers: tuple[str, ...]
    shallow: bool
    object_format: str
    observed_at: str

    @property
    def is_clean(self) -> bool:
        # Ignored files do not make Git dirty, but are still protected.
        return not self.changes and not self.masked_paths

    @property
    def tracked_modifications(self) -> tuple[str, ...]:
        return tuple(c.path for c in self.changes if c.kind != "untracked")

    @property
    def staged_modifications(self) -> tuple[str, ...]:
        return tuple(c.path for c in self.changes if c.index_status not in (".", "?"))

    @property
    def unstaged_modifications(self) -> tuple[str, ...]:
        return tuple(c.path for c in self.changes if c.worktree_status not in (".", "?"))

    @property
    def deleted_paths(self) -> tuple[str, ...]:
        return tuple(c.path for c in self.changes
                     if "D" in (c.index_status, c.worktree_status))

    def metadata(self) -> dict:
        return {**asdict(self), "is_clean": self.is_clean}

    @property
    def observation_hash(self) -> str:
        metadata = self.metadata()
        del metadata["observed_at"]
        return hashlib.sha256(canonical_json(metadata).encode("utf-8")).hexdigest()

    def protected_paths(self) -> dict[str, str]:
        paths = {p: "pre_existing_untracked" for p in (*self.untracked_paths, *self.ignored_paths)}
        for change in self.changes:
            if change.kind != "untracked":
                paths[change.path] = "pre_existing_dirty"
                if change.original_path:
                    paths[change.original_path] = "pre_existing_dirty"
        for path in self.masked_paths:
            paths[path] = "pre_existing_dirty"
        return dict(sorted(paths.items()))


def _status(data: bytes) -> tuple[dict[str, str], list[PathChange]]:
    records = iter(data.split(b"\0"))
    headers = {}
    changes = []
    try:
        for record in records:
            if not record:
                continue
            if record.startswith(b"# "):
                key, value = record[2:].decode("utf-8").split(" ", 1)
                headers[key] = value
            elif record.startswith(b"? "):
                changes.append(PathChange(safe_path(record[2:]), "?", "?", "untracked"))
            elif record[:2] in (b"1 ", b"2 ", b"u "):
                count = {b"1": 8, b"2": 9, b"u": 10}[record[:1]]
                fields = record.split(b" ", count)
                if len(fields) != count + 1 or len(fields[1]) != 2:
                    raise InspectionError("malformed porcelain status record")
                renamed = record.startswith(b"2 ")
                changes.append(PathChange(
                    safe_path(fields[-1]), chr(fields[1][0]), chr(fields[1][1]),
                    "rename" if renamed else "unmerged" if record[:1] == b"u" else "tracked",
                    safe_path(next(records)) if renamed else None,
                    fields[-2].decode("ascii") if renamed else None,
                    fields[2].decode("ascii"),
                ))
            else:
                raise InspectionError("unknown porcelain status record")
    except (ValueError, StopIteration, UnicodeError) as exc:
        raise InspectionError("unreadable Git status") from exc
    return headers, changes


def inspect_repository(
    path: Path | str, *, establish_identity: bool = True,
) -> RepositoryInspection:
    info = identity.resolve(path, create=establish_identity)
    root = info.repo_root

    def read(args):
        return git.read_bytes(args, cwd=root)

    status_args = ["status", "--porcelain=v2", "-z", "--branch", "--untracked-files=all",
                   "--ignore-submodules=all", "--find-renames=50%"]
    first_status = read(status_args)
    headers, changes = _status(first_status)
    if "branch.oid" not in headers or "branch.head" not in headers:
        raise InspectionError("missing HEAD/branch status identity")
    head = headers["branch.oid"]
    head = None if head == "(initial)" else head
    detached = headers["branch.head"] == "(detached)"
    branch = None if detached else headers["branch.head"]
    index_data = read(["ls-files", "--stage", "-z"])
    entries = []
    for row in index_data.split(b"\0"):
        if row:
            fields, name = row.split(b"\t", 1)
            mode, oid, stage = fields.decode("ascii").split(" ")
            entries.append(IndexEntry(safe_path(name), mode, oid, int(stage)))
    masked = []
    for row in read(["ls-files", "-v", "-z"]).split(b"\0"):
        if row and (row[:1].islower() or row[:1] == b"S"):
            masked.append(safe_path(row[2:]))
    ignored = tuple(sorted(safe_path(p) for p in read(
        ["ls-files", "--others", "--ignored", "--exclude-standard", "-z"]
    ).split(b"\0") if p))

    head_links = {}
    if head:
        for row in read(["ls-tree", "-r", "--full-tree", "-z", head]).split(b"\0"):
            if row:
                fields, name = row.split(b"\t", 1)
                mode, _kind, oid = fields.decode("ascii").split(" ")
                if mode == "160000":
                    head_links[safe_path(name)] = oid
    link_paths = set(head_links) | {e.path for e in entries if e.mode == "160000"}
    links = []
    for name in sorted(link_paths):
        indexed = tuple(e.object_id for e in entries if e.path == name and e.mode == "160000")
        old = head_links.get(name)
        links.append(Gitlink(name, old, indexed))
        # --ignore-submodules=all avoids traversing another repository.
        # Compare Git's index/HEAD objects explicitly to retain staged changes.
        if indexed != ((old,) if old else ()) and not any(c.path == name for c in changes):
            changes.append(PathChange(name, "A" if not old else "D" if not indexed else "M",
                                      ".", "gitlink", submodule="S..."))
    markers = tuple(name for name in (
        "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply",
        "BISECT_LOG", "sequencer",
    ) if (info.git_dir / name).exists())
    if first_status != read(status_args) or index_data != read(["ls-files", "--stage", "-z"]):
        raise RepositoryChangedError("Git status/index changed during inspection")
    return RepositoryInspection(
        str(root), info.repo_id, info.worktree_id, str(info.git_dir), str(info.git_common_dir),
        str(root), head, branch, detached, head is None,
        tuple(sorted(changes, key=lambda c: c.path)),
        tuple(sorted(entries, key=lambda e: (e.path, e.stage))),
        tuple(sorted(c.path for c in changes if c.kind == "untracked")), ignored,
        tuple(sorted(set(masked))), tuple(links), markers,
        read(["rev-parse", "--is-shallow-repository"]).strip() == b"true",
        read(["rev-parse", "--show-object-format"]).decode("ascii").strip(), utcnow_iso(),
    )


def path_is_within(path: str, scope: str) -> bool:
    """Component-aware scope matching, never string-prefix matching."""
    return PurePosixPath(path).is_relative_to(PurePosixPath(scope))
