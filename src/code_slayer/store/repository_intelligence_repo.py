"""Durable repository-intelligence snapshot pointers (Phase 8.1, schema
v7's `repository_intelligence_snapshots`).

Thin, transactional persistence primitive only — like every other repo
in this package, it does not decide whether a snapshot is current or
stale; `intelligence.service.RepositoryIntelligenceService` does, using
the same `store.db.transaction()` discipline every other repository
already follows.
"""

from __future__ import annotations

import sqlite3

from code_slayer.store.models import RepositoryIntelligenceSnapshotRow


class RepositoryIntelligenceRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create_in_transaction(
        self, *, snapshot_id: str, repo_id: str, worktree_id: str, head_sha: str | None,
        working_tree_dirty: bool, working_tree_fingerprint: str, index_version: str,
        created_at: str, snapshot_content_hash: str, file_count: int,
        inventory_truncated: bool,
    ) -> RepositoryIntelligenceSnapshotRow:
        if not self._conn.in_transaction:
            raise RuntimeError("repository intelligence snapshot creation requires a transaction")
        self._conn.execute(
            "INSERT INTO repository_intelligence_snapshots "
            "(snapshot_id, repo_id, worktree_id, head_sha, working_tree_dirty, "
            " working_tree_fingerprint, index_version, created_at, snapshot_content_hash, "
            " file_count, inventory_truncated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id, repo_id, worktree_id, head_sha, int(working_tree_dirty),
                working_tree_fingerprint, index_version, created_at, snapshot_content_hash,
                file_count, int(inventory_truncated),
            ),
        )
        return self.get(snapshot_id)

    def get(self, snapshot_id: str) -> RepositoryIntelligenceSnapshotRow:
        row = self._conn.execute(
            "SELECT * FROM repository_intelligence_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return _row_to_snapshot(row)

    def latest_for_scope(
        self, repo_id: str, worktree_id: str,
    ) -> RepositoryIntelligenceSnapshotRow | None:
        """The most recently created snapshot for this exact
        `(repo_id, worktree_id)` scope, or `None` if it has never been
        indexed. "Most recent" is `rowid` order (insertion order), never
        `created_at` alone — a wall-clock string comparison could in
        principle tie or disagree with real insertion order; `rowid` on
        an `INTEGER PRIMARY KEY`-less append-only table is monotonic by
        construction."""
        row = self._conn.execute(
            "SELECT * FROM repository_intelligence_snapshots "
            "WHERE repo_id = ? AND worktree_id = ? ORDER BY rowid DESC LIMIT 1",
            (repo_id, worktree_id),
        ).fetchone()
        return _row_to_snapshot(row) if row is not None else None


def _row_to_snapshot(row: sqlite3.Row) -> RepositoryIntelligenceSnapshotRow:
    return RepositoryIntelligenceSnapshotRow(
        snapshot_id=row["snapshot_id"], repo_id=row["repo_id"], worktree_id=row["worktree_id"],
        head_sha=row["head_sha"], working_tree_dirty=bool(row["working_tree_dirty"]),
        working_tree_fingerprint=row["working_tree_fingerprint"],
        index_version=row["index_version"], created_at=row["created_at"],
        snapshot_content_hash=row["snapshot_content_hash"], file_count=row["file_count"],
        inventory_truncated=bool(row["inventory_truncated"]),
    )
