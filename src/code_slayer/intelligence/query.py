"""Deterministic task-relevance ranking and bounded context-pack
assembly (Phase 8.1 §7/§8).

Pure functions over an already-built `Snapshot` — no filesystem access,
no re-reading the repository, no model call of any kind. Fixed integer
weights only; the same `(snapshot, query_text)` pair always produces the
same ranked candidates in the same order, on any machine, forever.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from code_slayer.intelligence.limits import (
    DEFAULT_CONTEXT_PACK_MAX_BYTES,
    DEFAULT_CONTEXT_PACK_MAX_FILES,
    DEFAULT_CONTEXT_PACK_PER_FILE_BYTES,
    DEFAULT_QUERY_RESULTS,
    MAX_QUERY_RESULTS,
)
from code_slayer.intelligence.models import ContextCandidate, ContextFile, ContextPack, Snapshot

_WEIGHT_PATH_MENTION = 100
_WEIGHT_SYMBOL_MATCH = 80
_WEIGHT_ASSOCIATED_TEST = 30
_WEIGHT_IMPORT_NEIGHBOR = 20
_WEIGHT_PROJECT_CONFIG = 10

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]*")


def _tokens(text: str) -> tuple[str, ...]:
    return tuple(_WORD.findall(text))


def rank(snapshot: Snapshot, query_text: str, *, limit: int = DEFAULT_QUERY_RESULTS) -> tuple[
    ContextCandidate, ...,
]:
    limit = max(1, min(limit, MAX_QUERY_RESULTS))
    lowered_query = query_text.lower()
    tokens = {t.lower() for t in _tokens(query_text)}
    scores: dict[str, float] = {}
    reasons: dict[str, list[str]] = {}

    def add(path: str, amount: int, reason: str) -> None:
        scores[path] = scores.get(path, 0) + amount
        reasons.setdefault(path, [])
        if reason not in reasons[path]:
            reasons[path].append(reason)

    source_paths = {f.path for f in snapshot.files if f.classification in ("source", "oversized")}

    for path in source_paths:
        basename = PurePosixPath(path).stem
        if path.lower() in lowered_query or (basename and basename.lower() in tokens):
            add(path, _WEIGHT_PATH_MENTION, f"path_mention:{path}")

    for symbol in snapshot.symbols:
        if symbol.kind == "module":
            continue
        if symbol.name.lower() in tokens or symbol.name in _tokens(query_text):
            add(symbol.path, _WEIGHT_SYMBOL_MATCH, f"symbol_match:{symbol.name}")

    if any(word in tokens for word in ("test", "tests", "testing")):
        for path in source_paths:
            if "test" in PurePosixPath(path).parts or PurePosixPath(path).stem.startswith("test_"):
                add(path, _WEIGHT_PROJECT_CONFIG, "project_config_relevance:test_keyword")

    # Import neighbors and associated tests only ever amplify a file that
    # already scored on its own merits -- never a way for an edge alone
    # to introduce a brand new candidate out of nothing. Both directions
    # of an "imports" edge count as proximity: a file already relevant
    # makes both what it depends on, and what depends on it, plausibly
    # relevant too.
    scored_paths = frozenset(path for path, score in scores.items() if score > 0)
    for edge in snapshot.edges:
        if edge.relation == "imports":
            if edge.source in scored_paths:
                add(edge.target, _WEIGHT_IMPORT_NEIGHBOR, f"import_neighbor:{edge.source}")
            if edge.target in scored_paths:
                add(edge.source, _WEIGHT_IMPORT_NEIGHBOR, f"import_neighbor:{edge.target}")
        if edge.relation == "tests_by_name" and edge.target in scored_paths:
            add(edge.source, _WEIGHT_ASSOCIATED_TEST, f"associated_test:{edge.target}")

    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return tuple(
        ContextCandidate(path, float(score), tuple(reasons[path]))
        for path, score in ranked[:limit] if score > 0
    )


def build_context_pack(
    snapshot: Snapshot, repo_root: Path, query_text: str, *, stale: bool,
    max_files: int = DEFAULT_CONTEXT_PACK_MAX_FILES,
    max_bytes: int = DEFAULT_CONTEXT_PACK_MAX_BYTES,
    per_file_bytes: int = DEFAULT_CONTEXT_PACK_PER_FILE_BYTES,
) -> ContextPack:
    from code_slayer.intelligence import paths as paths_mod

    candidates = rank(snapshot, query_text, limit=max(max_files, DEFAULT_QUERY_RESULTS))
    files: list[ContextFile] = []
    omitted: list[str] = []
    used_bytes = 0
    budget_exhausted = False

    for candidate in candidates:
        if len(files) >= max_files:
            omitted.append(candidate.path)
            budget_exhausted = True
            continue
        resolved = paths_mod.resolve_within_repo(repo_root, candidate.path)
        if resolved is None:
            omitted.append(candidate.path)
            continue
        try:
            raw = resolved.read_bytes()
        except OSError:
            omitted.append(candidate.path)
            continue
        cap = min(per_file_bytes, max_bytes - used_bytes)
        if cap <= 0:
            omitted.append(candidate.path)
            budget_exhausted = True
            continue
        text = raw.decode("utf-8", errors="replace")
        truncated = len(text.encode("utf-8")) > cap
        if truncated:
            text = text.encode("utf-8")[:cap].decode("utf-8", errors="ignore")
        used_bytes += len(text.encode("utf-8"))
        files.append(ContextFile(candidate.path, text, truncated, candidate.reasons))

    return ContextPack(
        snapshot_id=snapshot.snapshot_id, repo_id=snapshot.repo_id,
        worktree_id=snapshot.worktree_id, head_sha=snapshot.head_sha, query=query_text,
        files=tuple(files), projects=snapshot.projects, commands=snapshot.commands,
        omitted=tuple(omitted), budget_exhausted=budget_exhausted, stale=stale,
    )
