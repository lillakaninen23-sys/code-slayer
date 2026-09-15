"""The one place a `Snapshot` actually gets built from a real repository
(Phase 8.1). Every other module in this package only ever reads an
already-built `Snapshot` (`intelligence.query`, `intelligence.service`).

Pipeline: `repo.inspection.inspect_repository()` (already-vetted,
Git-authoritative tracked/untracked/ignored classification and identity)
→ deterministic exclusion + binary/size classification → project
detection → command discovery → Python symbol extraction → internal
import graph. A single unreadable/unparsable file is recorded and
skipped; it never aborts the rest of the build.

## Content-safe working-tree identity (Phase 8.1a)

`probe_identity()` is the cheap counterpart used for staleness checks
(`intelligence.service.status()`/`.query()`), and `build_snapshot()`
computes the identical value as a side effect of the full walk it
already has to do — so a fresh build's own identity always matches what
a probe taken at the same instant would produce. Both call
`_working_tree_identity()`, which is deliberately **not** a bare
`stat()`-based fingerprint (path/size/mtime alone cannot distinguish a
file whose content changed and was restored to the same size with its
mtime touched back — see `docs/REPOSITORY_INTELLIGENCE.md`):

- **Clean worktree** (`inspection.is_clean` — no tracked modification,
  no staged change, no untracked path, no assume-unchanged/skip-
  worktree masking): `head_sha` *alone* is the identity. Git's own
  object model already guarantees `head_sha` is a complete content
  identity for every currently-tracked file — nothing here reads or
  stats a single file. This is the fast path for the common case.
- **Dirty worktree**: `head_sha` plus, for every dirty-relevant path
  that is also part of this build's own candidate set (tracked
  modification/staged change/rename, untracked, or masked), an actual
  content-derived token (`intelligence.builder._dirty_token()`) —
  never that path's size/mtime. A clean tracked file contributes
  nothing extra: `head_sha` already accounts for it. Only the dirty
  subset is ever read from disk, however large the rest of the
  repository is.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from code_slayer.intelligence import commands as commands_mod
from code_slayer.intelligence import detection, graph, paths
from code_slayer.intelligence import symbols as symbols_mod
from code_slayer.intelligence.limits import (
    BINARY_SNIFF_BYTES,
    INDEX_VERSION,
    MAX_INVENTORY_FILES,
    MAX_TEXT_FILE_BYTES,
    MAX_TOTAL_INDEXED_BYTES,
)
from code_slayer.intelligence.models import FileRecord, Snapshot, SymbolError
from code_slayer.repo.inspection import RepositoryInspection, inspect_repository

_LANGUAGE_BY_EXTENSION: dict[str, str] = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".rs": "rust", ".go": "go", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp", ".md": "markdown", ".rst": "restructuredtext",
    ".json": "json", ".toml": "toml", ".yaml": "yaml", ".yml": "yaml", ".sh": "shell",
    ".bash": "shell", ".sql": "sql", ".html": "html", ".css": "css", ".scss": "scss",
    ".xml": "xml", ".cfg": "ini", ".ini": "ini",
}
_LANGUAGE_BY_BASENAME: dict[str, str] = {
    "Makefile": "makefile", "makefile": "makefile", "GNUmakefile": "makefile",
    "Dockerfile": "dockerfile", "CMakeLists.txt": "cmake",
}


def _language_for(path: str) -> str | None:
    name = PurePosixPath(path).name
    if name in _LANGUAGE_BY_BASENAME:
        return _LANGUAGE_BY_BASENAME[name]
    return _LANGUAGE_BY_EXTENSION.get(PurePosixPath(path).suffix)


def _looks_binary(raw: bytes) -> bool:
    return b"\0" in raw[:BINARY_SNIFF_BYTES]


def _candidate_paths(inspection) -> list[tuple[str, bool]]:
    """`(path, tracked)` for every real (stage-0, non-conflicted) index
    entry plus every untracked-but-not-ignored path — deliberately
    excludes `ignored_paths` by default (§2: "avoid indexing huge
    dependency/vendor trees"; a repository's own `.gitignore` already
    curated these). Sorted for a fully deterministic build order."""
    by_path: dict[str, bool] = {
        entry.path: True for entry in inspection.index_entries if entry.stage == 0
    }
    for path in inspection.untracked_paths:
        by_path.setdefault(path, False)
    return sorted(by_path.items())


def _included_candidates(
    inspection, extra_excluded_root: Path | None,
) -> tuple[list[tuple[str, bool]], bool]:
    repo_root = Path(inspection.repo_root)
    excluded_root = extra_excluded_root.resolve() if extra_excluded_root is not None else None
    candidates = []
    for path, tracked in _candidate_paths(inspection):
        if paths.has_excluded_component(path):
            continue
        resolved = paths.resolve_within_repo(repo_root, path)
        if resolved is not None and excluded_root and resolved.is_relative_to(excluded_root):
            continue
        candidates.append((path, tracked, resolved))
    truncated = len(candidates) > MAX_INVENTORY_FILES
    if truncated:
        candidates = candidates[:MAX_INVENTORY_FILES]
    return [(p, t, r) for p, t, r in candidates], truncated


def _dirty_relevant_paths(inspection: RepositoryInspection) -> set[str]:
    """Every candidate path whose content `head_sha` alone does not prove:
    a tracked modification/staged change/rename (both the new and the old
    side, so a rename always changes identity even if the destination
    happens to collide with something else), every untracked path
    (`inspection.changes` already includes these as `kind="untracked"`),
    and every assume-unchanged/skip-worktree masked path — masking makes
    Git's own status machinery *hide* real content drift, so a masked
    path's claimed "unchanged" state is never trusted as identity
    evidence."""
    relevant: set[str] = set()
    for change in inspection.changes:
        relevant.add(change.path)
        if change.original_path:
            relevant.add(change.original_path)
    relevant.update(inspection.masked_paths)
    return relevant


def _dirty_token(repo_root: Path, path: str, resolved: Path | None) -> str:
    """A real content-derived token for one dirty/untracked/masked
    candidate path — never that path's size or mtime. Hashing is
    memory-bounded and streamed (`hashlib.file_digest`) rather than
    reading the whole file into memory, and is deliberately **not**
    subject to `MAX_TEXT_FILE_BYTES`/`MAX_TOTAL_INDEXED_BYTES` — those
    bounds govern what content gets copied into the text index, a
    separate concern from proving whether a path's content changed at
    all. An oversized or binary dirty file therefore still changes this
    token when its content changes, even though its bytes are never (or
    only partially) indexed as text.

    Symlink-ness is checked on `repo_root / path` — the *pre-resolution*
    path — because `paths.resolve_within_repo()` (which produced
    `resolved`) already fully resolves symlinks; checking on the
    resolved path can never observe that the original entry was a
    symlink at all. A symlink's target string is folded into the token
    so retargeting a symlink changes identity even when the new target
    happens to resolve to identical bytes."""
    raw_path = repo_root / path
    try:
        is_symlink = raw_path.is_symlink()
    except OSError:
        is_symlink = False
    if is_symlink:
        try:
            target = os.readlink(raw_path)
        except OSError:
            return "symlink:unreadable"
        if resolved is None:
            return f"symlink:{target}:unresolved"
        try:
            with resolved.open("rb") as handle:
                digest = hashlib.file_digest(handle, "sha256").hexdigest()
        except (OSError, ValueError):
            return f"symlink:{target}:unreadable"
        return f"symlink:{target}:{digest}"
    if resolved is None:
        # Covers both a tracked deletion and a path that escaped the
        # repository root — either way, content identity cannot be
        # "unchanged" and must not be silently ignored.
        return "missing"
    try:
        with resolved.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
    except (OSError, ValueError):
        return "unreadable"
    return f"content:{digest}"


def _working_tree_identity(
    inspection: RepositoryInspection,
    repo_root: Path,
    candidates: list[tuple[str, bool, Path | None]],
) -> str:
    """`head_sha` alone when the worktree is fully clean — Git's own
    object model already makes `head_sha` a complete content identity
    for every tracked file, so nothing here reads or stats a single
    file. Otherwise `head_sha` plus a sorted, deterministic
    content-derived token (`_dirty_token`) for every candidate path
    Git's own evidence marks dirty/untracked/masked
    (`_dirty_relevant_paths`). A clean tracked file contributes nothing
    beyond `head_sha` — it is provably unchanged without reading it.
    `candidates` is already sorted by path, so iteration order (and
    therefore the resulting hash) is fully deterministic."""
    head = inspection.head or "unborn"
    if inspection.is_clean:
        return f"head:{head}"
    dirty_paths = _dirty_relevant_paths(inspection)
    parts = [f"head:{head}"]
    for path, _tracked, resolved in candidates:
        if path not in dirty_paths:
            continue
        parts.append(f"{path}:{_dirty_token(repo_root, path, resolved)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Identity:
    """The cheap identity `probe_identity()` returns — enough to decide
    whether a durable snapshot is still current, without rebuilding it."""

    repo_id: str
    worktree_id: str
    head_sha: str | None
    working_tree_dirty: bool
    working_tree_fingerprint: str


def probe_identity(repo_path: Path | str, *, extra_excluded_root: Path | None = None) -> Identity:
    inspection = inspect_repository(repo_path)
    candidates, _truncated = _included_candidates(inspection, extra_excluded_root)
    repo_root = Path(inspection.repo_root)
    return Identity(
        repo_id=inspection.repo_id, worktree_id=inspection.worktree_id,
        head_sha=inspection.head, working_tree_dirty=not inspection.is_clean,
        working_tree_fingerprint=_working_tree_identity(inspection, repo_root, candidates),
    )


def build_snapshot(
    repo_path: Path | str, *, extra_excluded_root: Path | None = None,
) -> Snapshot:
    """Returns a `Snapshot` with `snapshot_id`/`created_at` left blank —
    the caller (`intelligence.service`) assigns those and persists the
    result. This function has no notion of durable storage."""
    inspection = inspect_repository(repo_path)
    candidates, truncated = _included_candidates(inspection, extra_excluded_root)
    repo_root = Path(inspection.repo_root)
    fingerprint = _working_tree_identity(inspection, repo_root, candidates)

    files: list[FileRecord] = []
    text_cache: dict[str, str] = {}
    imports_by_path: dict[str, tuple] = {}
    symbol_records = []
    symbol_errors: list[SymbolError] = []
    indexed_bytes = 0
    all_paths = {path for path, _tracked, _resolved in candidates}

    for path, tracked, resolved in candidates:
        language = _language_for(path)
        if resolved is None:
            files.append(FileRecord(path, language, 0, tracked, "unreadable"))
            continue
        try:
            size = resolved.stat().st_size
        except OSError:
            files.append(FileRecord(path, language, 0, tracked, "unreadable"))
            continue
        if size > MAX_TEXT_FILE_BYTES:
            files.append(FileRecord(path, language, size, tracked, "oversized"))
            continue
        try:
            raw = resolved.read_bytes()
        except OSError:
            files.append(FileRecord(path, language, size, tracked, "unreadable"))
            continue
        if _looks_binary(raw):
            files.append(FileRecord(
                path, language, size, tracked, "binary", hashlib.sha256(raw).hexdigest(),
            ))
            continue
        if indexed_bytes + len(raw) > MAX_TOTAL_INDEXED_BYTES:
            files.append(FileRecord(path, language, size, tracked, "oversized"))
            continue
        indexed_bytes += len(raw)
        content_hash = hashlib.sha256(raw).hexdigest()
        files.append(FileRecord(path, language, size, tracked, "source", content_hash))
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        text_cache[path] = text
        try:
            result = symbols_mod.extract(path, text)
        except symbols_mod.SymbolExtractionError as exc:
            symbol_errors.append(SymbolError(path, str(exc)))
            continue
        if result is not None:
            symbol_records.extend(result.symbols)
            if result.imports:
                imports_by_path[path] = result.imports

    def read_text(name: str) -> str | None:
        return text_cache.get(name)

    projects = detection.detect_projects(all_paths, read_text)
    discovered_commands = commands_mod.discover_commands(all_paths, read_text)
    python_paths = {path for path in all_paths if path.endswith(".py")}
    edges = graph.build_edges(imports_by_path, python_paths)

    return Snapshot(
        snapshot_id="", repo_id=inspection.repo_id, worktree_id=inspection.worktree_id,
        head_sha=inspection.head, branch=inspection.branch,
        working_tree_dirty=not inspection.is_clean,
        working_tree_fingerprint=fingerprint, index_version=INDEX_VERSION, created_at="",
        files=tuple(files), inventory_truncated=truncated, projects=projects,
        commands=discovered_commands, symbols=tuple(symbol_records),
        symbol_errors=tuple(symbol_errors), edges=edges, indexed_text_bytes=indexed_bytes,
    )
