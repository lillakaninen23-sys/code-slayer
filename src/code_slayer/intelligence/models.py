"""In-memory repository-intelligence shapes (Phase 8.1).

Every dataclass here is a plain, JSON-serializable evidence record —
never a live filesystem/git handle, never a model opinion. `Snapshot`
is the complete, bounded result of one `intelligence.builder.build_
snapshot()` call; `snapshot_to_dict()`/`snapshot_from_dict()` are its
sole (de)serialization boundary, since a `Snapshot` is what actually
gets persisted, content-addressed, via `intelligence.service.
RepositoryIntelligenceService`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class FileRecord:
    """One inventoried file. `content_hash` is `None` whenever bytes
    were never actually read (binary, oversized, or unreadable) — a
    `FileRecord` existing at all is not a claim that its content was
    indexed, only that its path/metadata is known."""

    path: str
    language: str | None
    size: int
    tracked: bool
    classification: str  # "source" | "binary" | "oversized" | "unreadable"
    content_hash: str | None = None


@dataclass(frozen=True)
class ProjectEvidence:
    """One detected project/language characteristic — never asserted
    without at least one concrete `evidence_paths` entry a caller could
    go and look at themselves."""

    kind: str  # "python" | "node" | "rust" | "go" | "dotnet" | "java"
    evidence_paths: tuple[str, ...]
    facts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CommandCandidate:
    """One discovered, never-executed candidate command."""

    command: str
    purpose: str  # "test" | "lint" | "format" | "typecheck" | "build"
    evidence_source: str
    confidence: str  # "high" | "medium" | "low"


@dataclass(frozen=True)
class SymbolRecord:
    kind: str  # "module" | "class" | "function" | "async_function" | "method"
    name: str
    qualname: str
    path: str
    lineno: int
    end_lineno: int | None


@dataclass(frozen=True)
class SymbolError:
    path: str
    error: str


@dataclass(frozen=True)
class GraphEdge:
    """One deterministic, evidence-backed relation between two files.
    `evidence` names exactly what produced this edge (e.g. an AST import
    statement, or a test/source naming-convention match) — never a
    filename-similarity guess alone."""

    source: str
    target: str
    relation: str  # "imports" | "tests_by_name"
    evidence: str


@dataclass(frozen=True)
class Snapshot:
    """The complete, bounded result of one repository-intelligence
    build — everything else in this package (`query`, `context_pack`)
    reads only from an already-built `Snapshot`, never the live
    filesystem again."""

    snapshot_id: str
    repo_id: str
    worktree_id: str
    head_sha: str | None
    branch: str | None
    working_tree_dirty: bool
    working_tree_fingerprint: str
    index_version: str
    created_at: str
    files: tuple[FileRecord, ...]
    inventory_truncated: bool
    projects: tuple[ProjectEvidence, ...]
    commands: tuple[CommandCandidate, ...]
    symbols: tuple[SymbolRecord, ...]
    symbol_errors: tuple[SymbolError, ...]
    edges: tuple[GraphEdge, ...]
    indexed_text_bytes: int


@dataclass(frozen=True)
class ContextCandidate:
    """One ranked, explainable relevance result — `reasons` must always
    be non-empty when `score > 0`; a file is never "just relevant" with
    no citable evidence."""

    path: str
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ContextFile:
    path: str
    content: str
    truncated: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ContextPack:
    """A bounded, ready-to-use context bundle for a future worker/
    planner/Prompt Analyst — never the whole repository, always
    traceable back to exactly which snapshot it was built from."""

    snapshot_id: str
    repo_id: str
    worktree_id: str
    head_sha: str | None
    query: str
    files: tuple[ContextFile, ...]
    projects: tuple[ProjectEvidence, ...]
    commands: tuple[CommandCandidate, ...]
    omitted: tuple[str, ...]
    budget_exhausted: bool
    stale: bool


def snapshot_to_dict(snapshot: Snapshot) -> dict:
    return {"format_version": 1, **asdict(snapshot)}


def snapshot_from_dict(data: dict) -> Snapshot:
    if data.get("format_version") != 1:
        raise ValueError(f"unsupported repository intelligence snapshot format: {data!r}")
    return Snapshot(
        snapshot_id=data["snapshot_id"], repo_id=data["repo_id"],
        worktree_id=data["worktree_id"], head_sha=data["head_sha"], branch=data["branch"],
        working_tree_dirty=data["working_tree_dirty"],
        working_tree_fingerprint=data["working_tree_fingerprint"],
        index_version=data["index_version"], created_at=data["created_at"],
        files=tuple(FileRecord(**f) for f in data["files"]),
        inventory_truncated=data["inventory_truncated"],
        projects=tuple(ProjectEvidence(**p) for p in data["projects"]),
        commands=tuple(CommandCandidate(**c) for c in data["commands"]),
        symbols=tuple(SymbolRecord(**s) for s in data["symbols"]),
        symbol_errors=tuple(SymbolError(**e) for e in data["symbol_errors"]),
        edges=tuple(GraphEdge(**e) for e in data["edges"]),
        indexed_text_bytes=data["indexed_text_bytes"],
    )
