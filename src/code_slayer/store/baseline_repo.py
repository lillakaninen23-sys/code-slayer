"""Schema-v1 baseline rows, protected paths, and explicit audit references."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass

from code_slayer.audit.canonical import canonical_json
from code_slayer.audit.events import EventType
from code_slayer.audit.writer import AuditWriter
from code_slayer.repo.inspection import RepositoryInspection, path_is_within, safe_path
from code_slayer.repo.rules import RuleDiscovery


class BaselineError(RuntimeError):
    """A complete, correctly bound baseline cannot be recorded or loaded."""


class BaselineAlreadyExists(BaselineError):
    """The original baseline is immutable; recapture must not replace it."""


@dataclass(frozen=True)
class BaselineRecord:
    baseline_id: int
    task_id: str
    recorded_at: str
    manifest_hash: str


class BaselineRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def exists(self, task_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM repo_baselines WHERE task_id = ?", (task_id,),
        ).fetchone() is not None

    def record_in_transaction(
        self, task_id: str, *, inspection: RepositoryInspection, discovery: RuleDiscovery,
        manifest_hash: str, recorded_at: str,
    ) -> BaselineRecord:
        """Internal persistence primitive; the inspection service owns atomicity."""
        if not self._conn.in_transaction:
            raise RuntimeError("baseline persistence requires an open write transaction")
        if self.exists(task_id):
            raise BaselineAlreadyExists(task_id)
        cursor = self._conn.execute(
            "INSERT INTO repo_baselines "
            "(task_id, recorded_at, head_sha, branch, is_clean, dirty_files_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, recorded_at, inspection.head, inspection.branch, int(inspection.is_clean),
             canonical_json([asdict(c) for c in inspection.changes])),
        )
        assert cursor.lastrowid is not None
        for path, reason in inspection.protected_paths().items():
            self._conn.execute(
                "INSERT INTO baseline_protected_paths (task_id, path, reason) VALUES (?, ?, ?)",
                (task_id, path, reason),
            )
        for document in discovery.documents:
            self._conn.execute(
                "INSERT INTO rules_snapshots "
                "(task_id, source_path, content_hash, precedence_rank, loaded_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, document.source_path, document.content_hash,
                 document.precedence_rank, document.discovered_at),
            )
        audit = AuditWriter(self._conn)
        common = {
            "baseline_id": cursor.lastrowid, "manifest_hash": manifest_hash,
            "repo_id": inspection.repo_id, "worktree_id": inspection.worktree_id,
            "head": inspection.head, "branch": inspection.branch,
            "detached": inspection.detached, "unborn": inspection.unborn,
        }
        summary = {
            "is_clean": inspection.is_clean,
            "staged_count": len(inspection.staged_modifications),
            "unstaged_count": len(inspection.unstaged_modifications),
            "untracked_count": len(inspection.untracked_paths),
            "ignored_count": len(inspection.ignored_paths),
            "masked_count": len(inspection.masked_paths),
            "protected_count": len(inspection.protected_paths()),
        }
        for event_type, payload in (
            (EventType.REPO_INSPECTED, {**common, "dirty_summary": summary}),
            (EventType.RULES_LOADED, {**common, **discovery.metadata()}),
            (EventType.BASELINE_RECORDED, {
                **common, "dirty_summary": summary,
                "protected_paths": inspection.protected_paths(),
            }),
        ):
            audit.append(
                task_id=task_id, event_type=event_type, actor_type="system", actor_id=None,
                payload=payload, occurred_at=recorded_at,
            )
        return BaselineRecord(cursor.lastrowid, task_id, recorded_at, manifest_hash)

    def get(self, task_id: str) -> BaselineRecord:
        rows = self._conn.execute(
            "SELECT id, recorded_at FROM repo_baselines WHERE task_id = ?", (task_id,),
        ).fetchall()
        if not rows:
            raise KeyError(task_id)
        if len(rows) != 1:
            raise BaselineError("multiple original baselines for one task")
        row = rows[0]
        # The existing audit table holds the explicit baseline-id -> blob
        # reference. This is a stored reference, not reconstructed file content.
        references = [json.loads(event["payload_json"]) for event in self._conn.execute(
            "SELECT payload_json FROM audit_events WHERE task_id = ? AND event_type = ?",
            (task_id, EventType.BASELINE_RECORDED),
        )]
        matching = [ref for ref in references if ref.get("baseline_id") == row["id"]]
        if len(matching) != 1 or not isinstance(matching[0].get("manifest_hash"), str):
            raise BaselineError("baseline has no unique immutable manifest reference")
        digest = matching[0]["manifest_hash"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise BaselineError("invalid manifest content hash")
        return BaselineRecord(row["id"], task_id, row["recorded_at"], digest)

    def protected_paths(self, task_id: str) -> dict[str, str]:
        return {row["path"]: row["reason"] for row in self._conn.execute(
            "SELECT path, reason FROM baseline_protected_paths WHERE task_id = ? ORDER BY path",
            (task_id,),
        )}

    def is_protected(self, task_id: str, path: str) -> bool:
        safe_path(path.encode("utf-8"))
        return any(path_is_within(path, protected)
                   for protected in self.protected_paths(task_id))
