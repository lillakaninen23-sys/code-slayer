"""Deterministic document discovery without parsing or executing instructions."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

from code_slayer.repo.inspection import (
    InspectionError,
    RepositoryInspection,
    path_is_within,
    safe_path,
)
from code_slayer.store.db import utcnow_iso


class DocumentRole(StrEnum):
    AUTHORITATIVE = "authoritative_instruction"
    DISCOVERED = "discovered_instruction"
    INFORMATIONAL = "informational_document"


# This is filename discovery, not a runtime/provider adapter or a parser
# for external formats, includes, commands, or personal/global rules.
DOCUMENT_NAMES = {
    "AGENTS.md": ("instruction", DocumentRole.AUTHORITATIVE),
    "CLAUDE.md": ("instruction", DocumentRole.DISCOVERED),
    "README.md": ("documentation", DocumentRole.INFORMATIONAL),
    "CONTRIBUTING.md": ("documentation", DocumentRole.INFORMATIONAL),
}
MAX_DOCUMENT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class DiscoveredDocument:
    source_path: str
    source_kind: str
    role: DocumentRole
    scope: str
    precedence_rank: int
    content_hash: str
    discovered_at: str
    content: bytes = field(repr=False)

    def metadata(self) -> dict:
        return {key: value for key, value in asdict(self).items() if key != "content"}


@dataclass(frozen=True)
class SkippedDocument:
    source_path: str
    reason: str


@dataclass(frozen=True)
class RuleDiscovery:
    documents: tuple[DiscoveredDocument, ...]
    skipped: tuple[SkippedDocument, ...]

    def metadata(self) -> dict:
        return {
            "documents": [doc.metadata() for doc in self.documents],
            "skipped": [asdict(doc) for doc in self.skipped],
        }

    def identity(self) -> tuple:
        return (
            tuple((d.source_path, d.content_hash) for d in self.documents),
            self.skipped,
        )

    def instructions_for(self, path: str) -> tuple[DiscoveredDocument, ...]:
        safe_path(path.encode("utf-8"))
        return tuple(doc for doc in self.documents
                     if doc.role == DocumentRole.AUTHORITATIVE and path_is_within(path, doc.scope))


def _read_document(root: Path, relative: str, limit: int) -> tuple[bytes | None, str | None]:
    """Open every path component without following symlinks, including races."""
    try:
        with ExitStack() as stack:
            directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            stack.callback(os.close, directory)
            parts = PurePosixPath(relative).parts
            for part in parts[:-1]:
                directory = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory,
                )
                stack.callback(os.close, directory)
                try:
                    os.stat(".git", dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    return None, "nested_repository_boundary"
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory,
            )
            stream = stack.enter_context(os.fdopen(fd, "rb"))
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return None, "not_regular_file"
            content = stream.read(limit + 1)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns,
            ):
                raise InspectionError(f"document changed during read: {relative}")
            if len(content) > limit:
                raise InspectionError(f"document exceeds byte limit: {relative}")
            return content, None
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return None, "missing_or_symlink_path"
        raise InspectionError(f"cannot safely read document: {relative}") from exc


def discover_rules(
    inspection: RepositoryInspection, *, max_document_bytes: int = MAX_DOCUMENT_BYTES,
) -> RuleDiscovery:
    """Discover tracked and non-ignored untracked documents, inside this worktree.

    Ignored trees, Git internals, gitlinks, nested repositories and symlinks
    are not traversed. File bytes are evidence candidates only until persisted.
    """
    candidates = {e.path for e in inspection.index_entries} | set(inspection.untracked_paths)
    documents = []
    skipped = []
    now = utcnow_iso()
    for source_path in sorted(candidates):
        path = PurePosixPath(source_path)
        if path.name not in DOCUMENT_NAMES or source_path.endswith("/"):
            continue
        if any(path_is_within(source_path, link.path) for link in inspection.gitlinks):
            skipped.append(SkippedDocument(source_path, "gitlink_boundary"))
            continue
        content, reason = _read_document(
            Path(inspection.repo_root), source_path, max_document_bytes,
        )
        if content is None:
            skipped.append(SkippedDocument(source_path, reason or "unavailable"))
            continue
        source_kind, role = DOCUMENT_NAMES[path.name]
        scope = str(path.parent)
        documents.append(DiscoveredDocument(
            source_path, source_kind, role, scope,
            len(path.parent.parts) if role == DocumentRole.AUTHORITATIVE else -1,
            hashlib.sha256(content).hexdigest(), now, content,
        ))
    documents.sort(key=lambda d: (d.precedence_rank, d.source_path))
    return RuleDiscovery(tuple(documents), tuple(skipped))
