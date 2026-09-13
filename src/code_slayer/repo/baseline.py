"""Explicit inspection service: capture evidence, then atomically baseline.

No task loop, automatic retry, reconciliation, or target mutation lives here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from code_slayer.audit.canonical import canonical_json
from code_slayer.core import StaleTaskState, TaskState, TaskStateMachine, TransitionRequest
from code_slayer.repo.inspection import RepositoryChangedError, inspect_repository
from code_slayer.repo.rules import discover_rules
from code_slayer.store.baseline_repo import (
    BaselineAlreadyExists,
    BaselineError,
    BaselineRecord,
    BaselineRepo,
)
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import transaction, utcnow_iso
from code_slayer.store.models import Task
from code_slayer.store.task_repo import TaskRepo


class InspectionService:
    def __init__(self, conn: sqlite3.Connection, *, blobs_dir: Path | str) -> None:
        self._conn = conn
        self._blobs_dir = Path(blobs_dir).resolve()
        self._tasks = TaskRepo(conn)
        self._baselines = BaselineRepo(conn)
        self._machine = TaskStateMachine(conn)

    def start(self, task_id: str) -> Task:
        """Explicit start; its STATE_TRANSITION is the inspection-start audit."""
        return self._machine.transition(
            task_id, expected_state=TaskState.CREATED, to_state=TaskState.INSPECTING,
            reason="repository inspection started",
        )

    def capture(self, task_id: str, *, path: Path | str | None = None) -> BaselineRecord:
        """Capture once, or fail leaving INSPECTING without partial DB evidence.

        ContentStore may leave unreferenced immutable files after rollback;
        no blob metadata, baseline, rules, protections, or completion event
        commits unless the BASELINED transition also commits.
        """
        task = self._tasks.get(task_id)
        if self._baselines.exists(task_id):
            raise BaselineAlreadyExists(task_id)
        if task.state != TaskState.INSPECTING:
            raise StaleTaskState(f"expected INSPECTING, found {task.state}")
        inspection = inspect_repository(path or task.repo_root)
        if (inspection.repo_id, inspection.worktree_id) != (task.repo_id, task.worktree_id):
            raise BaselineError("inspection identity does not match the task")
        self._check_external_storage(inspection)
        discovery = discover_rules(inspection)
        # Detect changes in observable metadata, candidate paths and rule
        # bytes. This is not a filesystem lock or a full content snapshot.
        checked = inspect_repository(inspection.repo_root, establish_identity=False)
        if inspection.observation_hash != checked.observation_hash:
            raise RepositoryChangedError("repository changed during baseline capture")
        if discovery.identity() != discover_rules(checked).identity():
            raise RepositoryChangedError("documents changed during baseline capture")
        recorded_at = utcnow_iso()
        manifest = {
            "format_version": 1, "task_id": task_id, "recorded_at": recorded_at,
            "inspection": inspection.metadata(), "observation_hash": inspection.observation_hash,
            "protected_paths": inspection.protected_paths(), "rules": discovery.metadata(),
        }
        with transaction(self._conn):
            # Recheck task binding under the same lock as every durable write.
            current = self._tasks.get(task_id)
            if (current.repo_id, current.worktree_id) != (
                inspection.repo_id, inspection.worktree_id,
            ):
                raise BaselineError("task identity changed during capture")
            if current.state != TaskState.INSPECTING:
                raise StaleTaskState(f"expected INSPECTING, found {current.state}")
            if self._baselines.exists(task_id):
                raise BaselineAlreadyExists(task_id)
            store = ContentStore(self._conn, self._blobs_dir)
            for document in discovery.documents:
                self._put_evidence(store, document.content, "text/markdown", "rules_snapshot")
            evidence = self._put_evidence(
                store, canonical_json(manifest).encode("utf-8"),
                "application/json", "repository_baseline",
            )
            baseline = self._baselines.record_in_transaction(
                task_id, inspection=inspection, discovery=discovery,
                manifest_hash=evidence.content_hash, recorded_at=recorded_at,
            )
            self._machine.transition_in_transaction(
                task_id, request=TransitionRequest(
                    TaskState.INSPECTING, TaskState.BASELINED, "repository baseline recorded",
                ),
            )
        return baseline

    def read_manifest(self, task_id: str) -> dict:
        record = self._baselines.get(task_id)
        if self._blobs_dir.is_relative_to(Path(self._tasks.get(task_id).repo_root).resolve()):
            raise BaselineError("evidence must be outside the target")
        store = ContentStore(self._conn, self._blobs_dir)
        data = store.read(record.manifest_hash)
        if hashlib.sha256(data).hexdigest() != record.manifest_hash:
            raise BaselineError("baseline manifest content hash mismatch")
        manifest = json.loads(data)
        if manifest.get("task_id") != task_id or manifest.get("format_version") != 1:
            raise BaselineError("incompatible or incorrectly bound baseline manifest")
        return manifest

    @staticmethod
    def _put_evidence(store, data, media_type, source_kind):
        blob = store.put(data, media_type=media_type, source_kind=source_kind, exportable=False)
        # Foundation deduplication must never reclassify an existing blob.
        # Fail rather than silently treating an exportable/other-kind blob
        # as private baseline evidence.
        if blob.exportable or blob.source_kind != source_kind:
            raise BaselineError("existing blob classification conflicts with private evidence")
        if hashlib.sha256(store.read(blob.content_hash)).hexdigest() != blob.content_hash:
            raise BaselineError("evidence content hash mismatch")
        return blob

    def _check_external_storage(self, inspection):
        roots = [Path(p) for p in (
            inspection.repo_root, inspection.git_dir, inspection.git_common_dir,
        )]
        locations = [self._blobs_dir]
        locations.extend(Path(row["file"]).resolve() for row in self._conn.execute(
            "PRAGMA database_list"
        ) if row["file"])
        if any(location.is_relative_to(root) for root in roots for location in locations):
            raise BaselineError("baseline storage must be outside the target and Git dirs")
