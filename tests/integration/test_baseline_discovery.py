"""Atomic baseline/evidence/state integration against temporary targets."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from code_slayer.audit.verify import verify_chain
from code_slayer.audit.writer import AuditWriter
from code_slayer.core import StaleTaskState, TaskState, TaskStateMachine, TransitionRequest
from code_slayer.repo import baseline as baseline_module
from code_slayer.repo import identity
from code_slayer.repo.baseline import InspectionService
from code_slayer.repo.inspection import RepositoryChangedError
from code_slayer.store.baseline_repo import BaselineAlreadyExists, BaselineError, BaselineRepo
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import connect, known_schema_version, migrate, schema_version, transaction
from code_slayer.store.task_repo import TaskRepo
from tests.repo_helpers import commit, filesystem_snapshot, git


@pytest.fixture
def context(db_conn, git_repo_with_commit, tmp_path):
    root = git_repo_with_commit
    info = identity.resolve(root)
    task = TaskRepo(db_conn).create(
        task_id="baseline-task", description="inspect", repo_root=str(root),
        repo_id=info.repo_id, worktree_id=info.worktree_id,
    )
    directory = tmp_path / "evidence"
    service = InspectionService(db_conn, blobs_dir=directory)
    return root, task, service, directory


def events(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM audit_events WHERE task_id = ? ORDER BY seq", (task_id,),
    )]


def assert_no_baseline(conn, task_id, before):
    assert TaskRepo(conn).get(task_id).state == "INSPECTING"
    assert events(conn, task_id) == before
    for table in ("repo_baselines", "rules_snapshots", "baseline_protected_paths", "content_blobs"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert verify_chain(conn, task_id=task_id).ok


def test_full_baseline_state_audit_and_target_preservation(context, db_conn):
    root, task, service, _directory = context
    before_target = filesystem_snapshot(root)
    inspecting = service.start(task.task_id)
    assert inspecting.state == "INSPECTING"
    result = service.capture(task.task_id)
    updated = TaskRepo(db_conn).get(task.task_id)
    assert (updated.state, updated.current_phase) == ("BASELINED", "BASELINED")
    assert BaselineRepo(db_conn).get(task.task_id) == result
    manifest = service.read_manifest(task.task_id)
    assert manifest["format_version"] == 1
    assert manifest["task_id"] == task.task_id
    inspection = manifest["inspection"]
    assert inspection["repo_id"] == task.repo_id
    assert inspection["worktree_id"] == task.worktree_id
    assert inspection["head"] == git(root, "rev-parse", "HEAD")
    assert inspection["branch"] == git(root, "branch", "--show-current")
    assert inspection["is_clean"]
    assert manifest["protected_paths"] == {}
    row = db_conn.execute("SELECT * FROM repo_baselines").fetchone()
    assert row["head_sha"] == inspection["head"]
    assert row["branch"] == inspection["branch"]
    assert row["recorded_at"] == result.recorded_at == manifest["recorded_at"]
    assert json.loads(row["dirty_files_json"]) == []
    audit = events(db_conn, task.task_id)
    assert [e["event_type"] for e in audit] == [
        "TASK_CREATED", "STATE_TRANSITION", "REPO_INSPECTED", "RULES_LOADED",
        "BASELINE_RECORDED", "STATE_TRANSITION",
    ]
    assert json.loads(audit[1]["payload_json"])["to_state"] == "INSPECTING"
    assert json.loads(audit[-1]["payload_json"])["to_state"] == "BASELINED"
    for event in audit[2:5]:
        payload = json.loads(event["payload_json"])
        assert payload["baseline_id"] == result.baseline_id
        assert payload["manifest_hash"] == result.manifest_hash
        assert payload["head"] == inspection["head"]
    assert verify_chain(db_conn, task_id=task.task_id).ok
    assert schema_version(db_conn) == known_schema_version()
    assert db_conn.execute("SELECT count(*) FROM task_owned_paths").fetchone()[0] == 0
    assert filesystem_snapshot(root) == before_target


def test_dirty_snapshot_hygiene_and_immutable_protection(context, db_conn):
    root, task, service, directory = context
    marker = "SECRET-LIKE-DO-NOT-COPY-4e771bca"
    (root / "source.txt").write_text("original")
    (root / "old.txt").write_text("original")
    (root / ".gitignore").write_text(".env\n")
    commit(root)
    (root / "source.txt").write_text(marker)
    (root / "untracked.txt").write_text(marker)
    (root / ".env").write_text(marker)
    git(root, "mv", "old.txt", "renamed.txt")
    before_target = filesystem_snapshot(root)
    service.start(task.task_id)
    service.capture(task.task_id)
    assert filesystem_snapshot(root) == before_target
    repo = BaselineRepo(db_conn)
    protected = repo.protected_paths(task.task_id)
    assert protected == {
        ".env": "pre_existing_untracked", "old.txt": "pre_existing_dirty",
        "renamed.txt": "pre_existing_dirty", "source.txt": "pre_existing_dirty",
        "untracked.txt": "pre_existing_untracked",
    }
    assert service.read_manifest(task.task_id)["protected_paths"] == protected
    assert repo.is_protected(task.task_id, "old.txt")
    assert not repo.is_protected(task.task_id, "old.txt-backup")
    for table in ("audit_events", "repo_baselines", "baseline_protected_paths",
                  "rules_snapshots", "content_blobs"):
        assert marker not in str([tuple(r) for r in db_conn.execute(f"SELECT * FROM {table}")])
    for path in directory.rglob("*"):
        if path.is_file():
            assert marker.encode() not in path.read_bytes()
    (root / "source.txt").write_text("original")
    (root / "untracked.txt").unlink()
    assert repo.protected_paths(task.task_id) == protected
    with pytest.raises(BaselineAlreadyExists):
        service.capture(task.task_id)
    assert repo.protected_paths(task.task_id) == protected


def test_rule_evidence_retains_original_bytes_after_edit_and_reopen(context, db_conn):
    root, task, service, directory = context
    original = b"Use the original acceptance rules.\r\n"
    (root / "AGENTS.md").write_bytes(original)
    (root / "nested").mkdir()
    (root / "nested" / "AGENTS.md").write_bytes(b"Nested instruction\n")
    service.start(task.task_id)
    baseline = service.capture(task.task_id)
    manifest = service.read_manifest(task.task_id)
    rule = next(d for d in manifest["rules"]["documents"] if d["source_path"] == "AGENTS.md")
    assert rule["content_hash"] == hashlib.sha256(original).hexdigest()
    (root / "AGENTS.md").write_bytes(b"Different live rules\n")
    db_path = db_conn.execute("PRAGMA database_list").fetchone()["file"]
    reopened = connect(db_path)
    try:
        store = ContentStore(reopened, directory)
        assert store.read(rule["content_hash"]) == original
        assert store.get_meta(rule["content_hash"]).source_kind == "rules_snapshot"
        assert store.get_meta(rule["content_hash"]).exportable is False
        reader = InspectionService(reopened, blobs_dir=directory)
        assert reader.read_manifest(task.task_id) == manifest
        assert BaselineRepo(reopened).get(task.task_id) == baseline
        assert verify_chain(reopened, task_id=task.task_id).ok
    finally:
        reopened.close()
    assert original.decode().strip() not in str(events(db_conn, task.task_id))
    assert db_conn.execute("SELECT count(*) FROM rules_snapshots").fetchone()[0] == 3


@pytest.mark.parametrize("event_type", ["REPO_INSPECTED", "RULES_LOADED", "BASELINE_RECORDED",
                                        "STATE_TRANSITION"])
def test_failure_after_audit_append_rolls_back_entire_baseline(
    context, db_conn, monkeypatch, event_type,
):
    root, task, service, _ = context
    (root / "AGENTS.md").write_text("rules")
    service.start(task.task_id)
    before = events(db_conn, task.task_id)
    append = AuditWriter.append

    def fail(self, **kwargs):
        record = append(self, **kwargs)
        if kwargs["event_type"] == event_type:
            assert db_conn.execute("SELECT count(*) FROM repo_baselines").fetchone()[0] == 1
            raise RuntimeError("injected after audit append")
        return record

    with monkeypatch.context() as patch:
        patch.setattr(AuditWriter, "append", fail)
        with pytest.raises(RuntimeError, match="injected"):
            service.capture(task.task_id)
    assert_no_baseline(db_conn, task.task_id, before)
    # Unreferenced blob files may remain; a retry must safely register them.
    service.capture(task.task_id)
    assert TaskRepo(db_conn).get(task.task_id).state == "BASELINED"
    assert verify_chain(db_conn, task_id=task.task_id).ok


@pytest.mark.parametrize("table", ["repo_baselines", "baseline_protected_paths", "rules_snapshots",
                                   "content_blobs"])
def test_database_failure_at_each_baseline_write(context, db_conn, table):
    root, task, service, _ = context
    (root / "AGENTS.md").write_text("dirty rules")
    service.start(task.task_id)
    before = events(db_conn, task.task_id)
    db_conn.execute(
        f"CREATE TRIGGER fail_baseline AFTER INSERT ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        service.capture(task.task_id)
    assert_no_baseline(db_conn, task.task_id, before)


def test_capture_failure_during_discovery_has_no_durable_baseline(context, db_conn, monkeypatch):
    _root, task, service, directory = context
    service.start(task.task_id)
    before = events(db_conn, task.task_id)

    def fail(*args, **kwargs):
        raise RuntimeError("discovery failed")

    monkeypatch.setattr(baseline_module, "discover_rules", fail)
    with pytest.raises(RuntimeError, match="discovery failed"):
        service.capture(task.task_id)
    assert_no_baseline(db_conn, task.task_id, before)
    assert not directory.exists()


@pytest.mark.parametrize("what", ["rule_bytes", "new_path", "head"])
def test_observed_changes_during_capture_fail_closed(context, db_conn, monkeypatch, what):
    root, task, service, _ = context
    (root / "AGENTS.md").write_text("original rules")
    service.start(task.task_id)
    before = events(db_conn, task.task_id)
    inspect = baseline_module.inspect_repository
    calls = 0

    def change_between_reads(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            if what == "rule_bytes":
                (root / "AGENTS.md").write_text("different rules")
            elif what == "new_path":
                (root / "new.txt").write_text("new")
            else:
                commit(root)
        return inspect(*args, **kwargs)

    monkeypatch.setattr(baseline_module, "inspect_repository", change_between_reads)
    with pytest.raises(RepositoryChangedError):
        service.capture(task.task_id)
    assert_no_baseline(db_conn, task.task_id, before)


def test_task_state_changing_during_capture_is_not_overwritten(context, db_conn, monkeypatch):
    root, task, service, _ = context
    service.start(task.task_id)
    discover = baseline_module.discover_rules
    moved = False

    def interrupt(inspection):
        nonlocal moved
        if not moved:
            moved = True
            TaskStateMachine(db_conn).transition(
                task.task_id, expected_state=TaskState.INSPECTING,
                to_state=TaskState.INTERRUPTED_RESUMABLE, reason="interrupted during inspection",
            )
        return discover(inspection)

    monkeypatch.setattr(baseline_module, "discover_rules", interrupt)
    with pytest.raises(StaleTaskState):
        service.capture(task.task_id)
    assert TaskRepo(db_conn).get(task.task_id).state == "INTERRUPTED_RESUMABLE"
    assert not BaselineRepo(db_conn).exists(task.task_id)
    assert db_conn.execute("SELECT count(*) FROM content_blobs").fetchone()[0] == 0


def test_identity_mismatch_fails_without_baseline(context, db_conn, tmp_path):
    _, task, service, _ = context
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    service.start(task.task_id)
    before = events(db_conn, task.task_id)
    with pytest.raises(BaselineError, match="identity"):
        service.capture(task.task_id, path=other)
    assert_no_baseline(db_conn, task.task_id, before)


def test_capture_requires_explicit_inspection_start(context, db_conn):
    _, task, service, _ = context
    with pytest.raises(StaleTaskState):
        service.capture(task.task_id)
    assert TaskRepo(db_conn).get(task.task_id).state == "CREATED"


@pytest.mark.parametrize("location", ["worktree", "git_dir"])
def test_blob_storage_inside_target_is_rejected(context, db_conn, location):
    root, task, service, _ = context
    service.start(task.task_id)
    directory = (root if location == "worktree" else root / ".git") / "evidence"
    before = filesystem_snapshot(root)
    with pytest.raises(BaselineError, match="outside"):
        InspectionService(db_conn, blobs_dir=directory).capture(task.task_id)
    assert filesystem_snapshot(root) == before
    assert not directory.exists()


def test_database_inside_target_is_rejected(git_repo_with_commit, tmp_path):
    root = git_repo_with_commit
    info = identity.resolve(root)
    conn = connect(root / "wrong-state.db")
    try:
        migrate(conn)
        task = TaskRepo(conn).create(
            description="wrong location", repo_root=str(root),
            repo_id=info.repo_id, worktree_id=info.worktree_id,
        )
        service = InspectionService(conn, blobs_dir=tmp_path / "blobs")
        service.start(task.task_id)
        before = events(conn, task.task_id)
        with pytest.raises(BaselineError, match="outside"):
            service.capture(task.task_id)
        assert_no_baseline(conn, task.task_id, before)
    finally:
        conn.close()


def test_opaque_untracked_directory_protects_its_descendants(context, db_conn):
    root, task, service, _ = context
    nested = root / "embedded"
    nested.mkdir()
    git(nested, "init", "-q")
    (nested / "private.txt").write_text("private")
    service.start(task.task_id)
    service.capture(task.task_id)
    assert BaselineRepo(db_conn).is_protected(task.task_id, "embedded/private.txt")
    assert not BaselineRepo(db_conn).is_protected(task.task_id, "embedded-other/private.txt")


def test_two_capture_attempts_preserve_original_baseline(context, db_conn):
    _, task, service, directory = context
    service.start(task.task_id)
    first = service.capture(task.task_id)
    before = events(db_conn, task.task_id)
    path = db_conn.execute("PRAGMA database_list").fetchone()["file"]
    second_connection = connect(path)
    try:
        with pytest.raises(BaselineAlreadyExists):
            InspectionService(second_connection, blobs_dir=directory).capture(task.task_id)
    finally:
        second_connection.close()
    assert events(db_conn, task.task_id) == before
    assert BaselineRepo(db_conn).get(task.task_id) == first


@pytest.mark.parametrize(("source_kind", "exportable"), [
    ("rules_snapshot", True), ("command_output", False),
])
def test_existing_blob_classification_is_not_silently_changed(
    context, db_conn, source_kind, exportable,
):
    root, task, service, directory = context
    content = (root / "README.md").read_bytes()
    existing = ContentStore(db_conn, directory).put(
        content, media_type="text/plain", source_kind=source_kind, exportable=exportable,
    )
    service.start(task.task_id)
    before = events(db_conn, task.task_id)
    with pytest.raises(BaselineError, match="classification"):
        service.capture(task.task_id)
    assert ContentStore(db_conn, directory).get_meta(existing.content_hash) == existing
    assert events(db_conn, task.task_id) == before
    assert not BaselineRepo(db_conn).exists(task.task_id)


def test_state_machine_composition_retains_guards_and_rollback(context, db_conn):
    _, task, service, _ = context
    service.start(task.task_id)
    before = events(db_conn, task.task_id)
    machine = TaskStateMachine(db_conn)
    request = TransitionRequest(TaskState.CREATED, TaskState.BASELINED, "invalid edge")
    with pytest.raises(RuntimeError, match="open write transaction"):
        machine.transition_in_transaction(task.task_id, request=request)
    with pytest.raises(StaleTaskState), transaction(db_conn):
        machine.transition_in_transaction(task.task_id, request=request)
    assert events(db_conn, task.task_id) == before


_CRASH = """
import os, sys
sys.path.insert(0, sys.argv[1])
from code_slayer.audit.writer import AuditWriter
from code_slayer.repo.baseline import InspectionService
from code_slayer.store.db import connect
conn = connect(sys.argv[2])
append = AuditWriter.append
def crash(self, **kwargs):
    result = append(self, **kwargs)
    if kwargs['event_type'] == sys.argv[4]:
        assert conn.in_transaction
        os._exit(74)
    return result
AuditWriter.append = crash
InspectionService(conn, blobs_dir=sys.argv[3]).capture('baseline-task')
assert sys.argv[4] == 'after_commit'
os._exit(74)
"""


@pytest.mark.parametrize("point", ["RULES_LOADED", "STATE_TRANSITION", "after_commit"])
def test_process_crash_cannot_publish_partial_baseline(context, db_conn, point):
    _, task, service, directory = context
    service.start(task.task_id)
    before = events(db_conn, task.task_id)
    db_path = db_conn.execute("PRAGMA database_list").fetchone()["file"]
    src = str(Path(__file__).resolve().parents[2] / "src")
    result = subprocess.run(
        [sys.executable, "-c", _CRASH, src, db_path, str(directory), point],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 74, result.stderr
    reopened = connect(db_path)
    try:
        assert migrate(reopened) == known_schema_version()
        if point == "after_commit":
            assert TaskRepo(reopened).get(task.task_id).state == "BASELINED"
            assert BaselineRepo(reopened).get(task.task_id).manifest_hash
            assert len(events(reopened, task.task_id)) == len(before) + 4
            assert InspectionService(reopened, blobs_dir=directory).read_manifest(task.task_id)
        else:
            assert_no_baseline(reopened, task.task_id, before)
        assert verify_chain(reopened, task_id=task.task_id).ok
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()
