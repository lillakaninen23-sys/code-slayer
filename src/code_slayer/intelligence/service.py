"""`RepositoryIntelligenceService`: the one public entry point for
deterministic repository intelligence (Phase 8.1 §12).

Mirrors `runner.local_worker_runner.LocalWorkerRunner`'s own shape: one
instance per primary repository, opening (and migrating) its own
control-plane connection in `__init__`, closed explicitly via
`.close()`. Nothing outside this module ever queries
`repository_intelligence_snapshots` or the underlying `content_blobs`
snapshot payload directly — a future planner, Prompt Analyst, worker
context builder, or the WebUI application API all go through here.

Read-only guarantee: this service never writes a repository file, never
executes a discovered command, never touches trust/lease/checkpoint
state, and never authorizes cloud transport — see the module docstring
of `intelligence` itself.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from code_slayer.intelligence import builder
from code_slayer.intelligence.limits import (
    DEFAULT_CONTEXT_PACK_MAX_BYTES,
    DEFAULT_CONTEXT_PACK_MAX_FILES,
    DEFAULT_CONTEXT_PACK_PER_FILE_BYTES,
    DEFAULT_QUERY_RESULTS,
    MAX_CONTEXT_PACK_MAX_BYTES,
    MAX_CONTEXT_PACK_MAX_FILES,
    MAX_CONTEXT_PACK_PER_FILE_BYTES,
    MAX_QUERY_RESULTS,
)
from code_slayer.intelligence.models import (
    CommandCandidate,
    ContextCandidate,
    ContextPack,
    ProjectEvidence,
    Snapshot,
    snapshot_from_dict,
    snapshot_to_dict,
)
from code_slayer.intelligence.query import build_context_pack, rank
from code_slayer.repo import identity
from code_slayer.store import db as db_module
from code_slayer.store import location
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.repository_intelligence_repo import RepositoryIntelligenceRepo

SNAPSHOT_EVIDENCE_KIND = "repository_intelligence_snapshot"


@dataclass(frozen=True)
class Status:
    """`.status()`'s read-only report — never rebuilds anything."""

    indexed: bool
    current: bool
    snapshot_id: str | None
    head_sha: str | None
    working_tree_dirty: bool | None
    created_at: str | None
    file_count: int | None
    inventory_truncated: bool | None
    projects: tuple[ProjectEvidence, ...] = ()
    commands: tuple[CommandCandidate, ...] = ()


class RepositoryIntelligenceService:
    def __init__(
        self, repo_path: Path | str, *, state_root_override: str | Path | None = None,
    ) -> None:
        self._primary = identity.resolve(repo_path)
        self._state_root_override = state_root_override
        self._db_path = location.db_path(
            self._primary.repo_id, self._primary.worktree_id, override=state_root_override,
        )
        self._blobs_dir = location.blobs_dir(
            self._primary.repo_id, self._primary.worktree_id, override=state_root_override,
        )
        location.ensure_dirs(
            self._primary.repo_id, self._primary.worktree_id, override=state_root_override,
        )
        self._conn = db_module.connect(self._db_path)
        db_module.migrate(self._conn)

    def close(self) -> None:
        self._conn.close()

    # -- internals ------------------------------------------------------

    def _extra_excluded_root(self) -> Path:
        return location.state_root(override=self._state_root_override)

    def _latest_row(self):
        return RepositoryIntelligenceRepo(self._conn).latest_for_scope(
            self._primary.repo_id, self._primary.worktree_id,
        )

    def _read_snapshot(self, row) -> Snapshot:
        store = ContentStore(self._conn, self._blobs_dir)
        meta = store.get_meta(row.snapshot_content_hash)
        if meta is None or meta.source_kind != SNAPSHOT_EVIDENCE_KIND:
            raise RuntimeError(
                f"no durable repository intelligence snapshot evidence for {row.snapshot_id!r}"
            )
        content = store.read(row.snapshot_content_hash)
        if hashlib.sha256(content).hexdigest() != row.snapshot_content_hash:
            raise RuntimeError("repository intelligence snapshot blob content hash mismatch")
        return snapshot_from_dict(json.loads(content))

    def _is_current(self, row) -> bool:
        probe = builder.probe_identity(
            self._primary.repo_root, extra_excluded_root=self._extra_excluded_root(),
        )
        return (
            row is not None
            and row.head_sha == probe.head_sha
            and row.working_tree_fingerprint == probe.working_tree_fingerprint
        )

    # -- public API -------------------------------------------------------

    def inspect(self, *, force: bool = False) -> Snapshot:
        """Return the current snapshot, rebuilding only if none exists
        yet, the repository has changed since the last one, or `force`
        is set. A fresh build is always a full, deterministic,
        bounded rebuild (§10) — never incremental."""
        row = self._latest_row()
        if not force and row is not None and self._is_current(row):
            return self._read_snapshot(row)
        return self._rebuild()

    def _rebuild(self) -> Snapshot:
        built = builder.build_snapshot(
            self._primary.repo_root, extra_excluded_root=self._extra_excluded_root(),
        )
        snapshot_id = uuid.uuid4().hex
        created_at = utcnow_iso()
        snapshot = replace(built, snapshot_id=snapshot_id, created_at=created_at)
        payload = json.dumps(snapshot_to_dict(snapshot), sort_keys=True).encode("utf-8")
        store = ContentStore(self._conn, self._blobs_dir)
        with transaction(self._conn):
            blob = store.put(
                payload, media_type="application/json", source_kind=SNAPSHOT_EVIDENCE_KIND,
                exportable=False,
            )
            RepositoryIntelligenceRepo(self._conn).create_in_transaction(
                snapshot_id=snapshot_id, repo_id=snapshot.repo_id, worktree_id=snapshot.worktree_id,
                head_sha=snapshot.head_sha, working_tree_dirty=snapshot.working_tree_dirty,
                working_tree_fingerprint=snapshot.working_tree_fingerprint,
                index_version=snapshot.index_version, created_at=created_at,
                snapshot_content_hash=blob.content_hash, file_count=len(snapshot.files),
                inventory_truncated=snapshot.inventory_truncated,
            )
        return snapshot

    def status(self) -> Status:
        row = self._latest_row()
        if row is None:
            return Status(False, False, None, None, None, None, None, None)
        current = self._is_current(row)
        snapshot = self._read_snapshot(row)
        return Status(
            indexed=True, current=current, snapshot_id=row.snapshot_id, head_sha=row.head_sha,
            working_tree_dirty=row.working_tree_dirty, created_at=row.created_at,
            file_count=row.file_count, inventory_truncated=row.inventory_truncated,
            projects=snapshot.projects, commands=snapshot.commands,
        )

    def query(
        self, text: str, *, limit: int = DEFAULT_QUERY_RESULTS,
    ) -> tuple[tuple[ContextCandidate, ...], bool]:
        """Ranks against the latest *durable* snapshot — never rebuilds
        as a side effect of a query. Returns `(candidates, stale)`;
        `stale=True` means the repository has changed since this
        snapshot was taken and a caller may want `.inspect()` first."""
        limit = max(1, min(limit, MAX_QUERY_RESULTS))
        row = self._latest_row()
        if row is None:
            return (), False
        snapshot = self._read_snapshot(row)
        stale = not self._is_current(row)
        return rank(snapshot, text, limit=limit), stale

    def build_context_pack(
        self, text: str, *, max_files: int = DEFAULT_CONTEXT_PACK_MAX_FILES,
        max_bytes: int = DEFAULT_CONTEXT_PACK_MAX_BYTES,
        per_file_bytes: int = DEFAULT_CONTEXT_PACK_PER_FILE_BYTES,
    ) -> ContextPack | None:
        """`None` when nothing has ever been indexed — the caller
        decides whether to `.inspect()` first."""
        max_files = max(1, min(max_files, MAX_CONTEXT_PACK_MAX_FILES))
        max_bytes = max(1, min(max_bytes, MAX_CONTEXT_PACK_MAX_BYTES))
        per_file_bytes = max(1, min(per_file_bytes, MAX_CONTEXT_PACK_PER_FILE_BYTES))
        row = self._latest_row()
        if row is None:
            return None
        snapshot = self._read_snapshot(row)
        stale = not self._is_current(row)
        return build_context_pack(
            snapshot, self._primary.repo_root, text, stale=stale, max_files=max_files,
            max_bytes=max_bytes, per_file_bytes=per_file_bytes,
        )
