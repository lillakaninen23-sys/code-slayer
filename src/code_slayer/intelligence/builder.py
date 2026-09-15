"""The one place a `Snapshot` actually gets built from a real repository
(Phase 8.1). Every other module in this package only ever reads an
already-built `Snapshot` (`intelligence.query`, `intelligence.service`).

Pipeline: `repo.inspection.inspect_repository()` (already-vetted,
Git-authoritative tracked/untracked/ignored classification and identity)
→ deterministic exclusion + binary/size classification → project
detection → command discovery → Python symbol extraction → internal
import graph. A single unreadable/unparsable file is recorded and
skipped; it never aborts the rest of the build.

`probe_identity()` is the deliberately cheap counterpart used for
staleness checks (`intelligence.service.status()`/`.query()`): it
recomputes the *same* `working_tree_fingerprint` formula from `stat()`
metadata alone (path, size, mtime) — never reading a single file's
content — so checking "is the durable snapshot still current" never
costs anywhere near what actually rebuilding one does. `build_snapshot()`
computes the identical fingerprint as a side effect of the full walk it
already has to do, so a fresh build's own fingerprint always matches
what a probe taken at the same instant would have produced.
"""

from __future__ import annotations

import hashlib
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
from code_slayer.repo.inspection import inspect_repository

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


def _stat_fingerprint(candidates: list[tuple[str, bool, Path | None]]) -> str:
    parts = []
    for path, _tracked, resolved in candidates:
        if resolved is None:
            parts.append(f"{path}:missing")
            continue
        try:
            info = resolved.stat()
        except OSError:
            parts.append(f"{path}:missing")
            continue
        parts.append(f"{path}:{info.st_size}:{info.st_mtime_ns}")
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
    return Identity(
        repo_id=inspection.repo_id, worktree_id=inspection.worktree_id,
        head_sha=inspection.head, working_tree_dirty=not inspection.is_clean,
        working_tree_fingerprint=_stat_fingerprint(candidates),
    )


def build_snapshot(
    repo_path: Path | str, *, extra_excluded_root: Path | None = None,
) -> Snapshot:
    """Returns a `Snapshot` with `snapshot_id`/`created_at` left blank —
    the caller (`intelligence.service`) assigns those and persists the
    result. This function has no notion of durable storage."""
    inspection = inspect_repository(repo_path)
    candidates, truncated = _included_candidates(inspection, extra_excluded_root)
    fingerprint = _stat_fingerprint(candidates)

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
