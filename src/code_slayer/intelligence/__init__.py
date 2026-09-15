"""Deterministic repository intelligence (Phase 8.1 —
`docs/ROADMAP.md#engineering-intelligence`).

Read-only, evidence-only repository understanding — file inventory,
detected project/language evidence, discovered (never executed) test/
lint/build commands, Python symbol extraction, an internal import
graph, and a deterministic relevance query over all of it — durable
across a process restart and bound to an exact repository identity/HEAD
so stale information is never silently served as current.

See `intelligence.service.RepositoryIntelligenceService` for the public
entry point every other component (future Prompt Analyst, planner,
worker context builder, the WebUI application API) should use — nothing
outside this package queries `repository_intelligence_snapshots` or the
underlying content-addressed snapshot blob directly.
"""

from code_slayer.intelligence.models import (
    CommandCandidate,
    ContextCandidate,
    ContextPack,
    FileRecord,
    GraphEdge,
    ProjectEvidence,
    Snapshot,
    SymbolRecord,
)
from code_slayer.intelligence.service import RepositoryIntelligenceService

__all__ = [
    "CommandCandidate",
    "ContextCandidate",
    "ContextPack",
    "FileRecord",
    "GraphEdge",
    "ProjectEvidence",
    "RepositoryIntelligenceService",
    "Snapshot",
    "SymbolRecord",
]
