"""End-to-end ToolExecutor behavior against real temporary Git repositories.

Covers the controlled tool/policy layer's safety properties: policy
allow/deny/require-approval, baseline/ownership/scope enforcement,
filesystem-race and symlink/hardlink defenses, the operation journal's
STARTED/SUCCEEDED/FAILED/UNKNOWN semantics (including a real subprocess
crash), content-addressed evidence semantics, and audit emission —
without weakening any Phase 1-3 invariant.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from code_slayer.audit.verify import verify_chain
from code_slayer.core import TaskState, TaskStateMachine
from code_slayer.policy.engine import Decision
from code_slayer.repo import identity
from code_slayer.repo.baseline import InspectionService
from code_slayer.store.content_store import ContentStore
from code_slayer.store.db import connect, transaction
from code_slayer.store.lease_repo import LeaseRepo
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.tool_operations_repo import OperationStatus, ToolOperationsRepo
from code_slayer.tools.executor import ToolExecutor
from code_slayer.tools.models import CommandRequest, PatchHunk, ToolRequest
from tests.repo_helpers import acquire_lease, commit, git


@pytest.fixture
def context(db_conn, git_repo_with_commit, tmp_path):
    root = git_repo_with_commit
    info = identity.resolve(root)
    directory = tmp_path / "evidence"
    task = TaskRepo(db_conn).create(
        description="tool execution", repo_root=str(root),
        repo_id=info.repo_id, worktree_id=info.worktree_id,
        config={"tool_policy": {"scope": ["."]}},
    )
    service = InspectionService(db_conn, blobs_dir=directory)
    service.start(task.task_id)
    service.capture(task.task_id)
    machine = TaskStateMachine(db_conn)
    for to_state, expected in (
        (TaskState.PLANNING, TaskState.BASELINED),
        (TaskState.PLANNED, TaskState.PLANNING),
        (TaskState.IMPLEMENTING, TaskState.PLANNED),
    ):
        machine.transition(
            task.task_id, expected_state=expected, to_state=to_state, reason="progressing",
        )
    lease = acquire_lease(db_conn, task)
    executor = ToolExecutor(db_conn, blobs_dir=directory, lease=lease)
    return root, task.task_id, executor, directory


def operations(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM tool_operations WHERE task_id = ? ORDER BY started_at", (task_id,),
    )]


def owned_paths(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM task_owned_paths WHERE task_id = ? AND deleted = 0", (task_id,),
    )]


def events(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM audit_events WHERE task_id = ? ORDER BY seq", (task_id,),
    )]


def event_types(conn, task_id):
    return [e["event_type"] for e in events(conn, task_id)]


# --- create_file -----------------------------------------------------------

def test_create_file_allowed_creates_owned_file_and_journal_entry(context, db_conn):
    root, task_id, executor, _ = context
    result = executor.execute(task_id, ToolRequest(tool="create_file", path="new.txt",
                                                    content=b"hello"))
    assert result.decision == Decision.ALLOW
    assert result.status == OperationStatus.SUCCEEDED
    assert (root / "new.txt").read_bytes() == b"hello"
    ops = operations(db_conn, task_id)
    assert len(ops) == 1
    assert ops[0]["status"] == OperationStatus.SUCCEEDED
    assert ops[0]["before_evidence"] == "ABSENT"
    assert ops[0]["risk_class"] == "WRITE_OWNED"
    owned = owned_paths(db_conn, task_id)
    assert [p["path"] for p in owned] == ["new.txt"]
    assert owned[0]["last_operation_id"] == result.operation_id
    assert event_types(db_conn, task_id)[-4:] == [
        "TOOL_REQUESTED", "POLICY_EVALUATED", "OPERATION_STARTED", "OPERATION_FINISHED",
    ]
    assert verify_chain(db_conn, task_id=task_id).ok


def test_create_file_over_existing_path_denied_before_any_journal_entry(context, db_conn):
    root, task_id, executor, _ = context
    (root / "already-there.txt").write_text("pre-existing, untracked")
    # This path is untracked at baseline time, so it is also protected —
    # but create_file must be denied as "not_a_new_path" regardless.
    result = executor.execute(
        task_id, ToolRequest(tool="create_file", path="already-there.txt", content=b"x"),
    )
    assert result.decision == Decision.DENY
    assert (root / "already-there.txt").read_text() == "pre-existing, untracked"
    assert operations(db_conn, task_id) == []


def test_create_file_path_traversal_denied(context, db_conn):
    _root, task_id, executor, _ = context
    result = executor.execute(
        task_id, ToolRequest(tool="create_file", path="../escape.txt", content=b"x"),
    )
    assert result.decision == Decision.DENY
    assert operations(db_conn, task_id) == []


def test_create_file_outside_declared_scope_denied(db_conn, git_repo_with_commit, tmp_path):
    root = git_repo_with_commit
    (root / "src").mkdir()
    info = identity.resolve(root)
    directory = tmp_path / "evidence"
    task = TaskRepo(db_conn).create(
        description="scoped", repo_root=str(root), repo_id=info.repo_id,
        worktree_id=info.worktree_id, config={"tool_policy": {"scope": ["src"]}},
    )
    service = InspectionService(db_conn, blobs_dir=directory)
    service.start(task.task_id)
    service.capture(task.task_id)
    machine = TaskStateMachine(db_conn)
    machine.transition(task.task_id, expected_state=TaskState.BASELINED,
                        to_state=TaskState.PLANNING, reason="r")
    machine.transition(task.task_id, expected_state=TaskState.PLANNING,
                        to_state=TaskState.PLANNED, reason="r")
    machine.transition(task.task_id, expected_state=TaskState.PLANNED,
                        to_state=TaskState.IMPLEMENTING, reason="r")
    executor = ToolExecutor(db_conn, blobs_dir=directory, lease=acquire_lease(db_conn, task))
    result = executor.execute(
        task.task_id, ToolRequest(tool="create_file", path="outside.txt", content=b"x"),
    )
    assert result.decision == Decision.DENY
    assert not (root / "outside.txt").exists()
    # Inside scope must still succeed.
    ok = executor.execute(
        task.task_id, ToolRequest(tool="create_file", path="src/inside.txt", content=b"x"),
    )
    assert ok.status == OperationStatus.SUCCEEDED


def test_create_file_denied_before_implementing(context, db_conn):
    root, task_id, executor, _directory = context
    # Roll the task back to PLANNED is not legal; instead verify at an
    # earlier point using a fresh task still in BASELINED.
    other_root = root
    info = identity.resolve(other_root)
    task = TaskRepo(db_conn).get(task_id)
    assert task.state == "IMPLEMENTING"
    # A read is fine before IMPLEMENTING; a mutation is not. Exercise this
    # on the same executor by moving the task to VERIFYING (a legal forward
    # edge from IMPLEMENTING) where mutation is again denied.
    TaskStateMachine(db_conn).transition(
        task_id, expected_state=TaskState.IMPLEMENTING, to_state=TaskState.VERIFYING,
        reason="verifying",
    )
    result = executor.execute(
        task_id, ToolRequest(tool="create_file", path="too-late.txt", content=b"x"),
    )
    assert result.decision == Decision.DENY
    assert not (root / "too-late.txt").exists()
    del info


# --- write_file / apply_patch ----------------------------------------------

def _create(executor, task_id, path, content):
    result = executor.execute(task_id, ToolRequest(tool="create_file", path=path, content=content))
    assert result.status == OperationStatus.SUCCEEDED
    return result


def test_write_file_updates_owned_content_and_ownership_marker(context, db_conn):
    root, task_id, executor, _ = context
    created = _create(executor, task_id, "owned.txt", b"v1")
    import hashlib

    result = executor.execute(task_id, ToolRequest(
        tool="write_file", path="owned.txt", content=b"v2",
        expected_hash=hashlib.sha256(b"v1").hexdigest(),
    ))
    assert result.status == OperationStatus.SUCCEEDED
    assert (root / "owned.txt").read_bytes() == b"v2"
    owned = owned_paths(db_conn, task_id)
    assert owned[0]["last_operation_id"] == result.operation_id
    assert owned[0]["last_operation_id"] != created.operation_id


def test_write_file_wrong_expected_hash_fails_without_mutating(context, db_conn):
    root, task_id, executor, _ = context
    _create(executor, task_id, "owned.txt", b"v1")
    result = executor.execute(task_id, ToolRequest(
        tool="write_file", path="owned.txt", content=b"v2", expected_hash="0" * 64,
    ))
    assert result.status == OperationStatus.FAILED
    assert (root / "owned.txt").read_bytes() == b"v1"


def test_write_file_on_non_owned_existing_path_requires_approval_and_does_not_execute(
    context, db_conn,
):
    root, task_id, executor, _ = context
    (root / "external.txt").write_text("owned by someone else")
    commit(root)  # tracked, so it is not "pre_existing" at this fresh baseline...
    # Actually this file did not exist at baseline time (baseline captured
    # before this commit), so it is genuinely external, unowned, existing.
    import hashlib

    result = executor.execute(task_id, ToolRequest(
        tool="write_file", path="external.txt", content=b"overwritten",
        expected_hash=hashlib.sha256(b"owned by someone else").hexdigest(),
    ))
    assert result.decision == Decision.REQUIRE_APPROVAL
    assert (root / "external.txt").read_text() == "owned by someone else"
    assert operations(db_conn, task_id) == []


def test_apply_patch_exact_context_succeeds(context, db_conn):
    root, task_id, executor, _ = context
    _create(executor, task_id, "p.txt", b"hello world")
    import hashlib

    result = executor.execute(task_id, ToolRequest(
        tool="apply_patch", path="p.txt", expected_hash=hashlib.sha256(b"hello world").hexdigest(),
        hunks=(PatchHunk(offset=6, before=b"world", after=b"there"),),
    ))
    assert result.status == OperationStatus.SUCCEEDED
    assert (root / "p.txt").read_bytes() == b"hello there"


def test_apply_patch_context_mismatch_fails_without_mutating(context, db_conn):
    root, task_id, executor, _ = context
    _create(executor, task_id, "p.txt", b"hello world")
    import hashlib

    result = executor.execute(task_id, ToolRequest(
        tool="apply_patch", path="p.txt", expected_hash=hashlib.sha256(b"hello world").hexdigest(),
        hunks=(PatchHunk(offset=6, before=b"WORLD", after=b"there"),),
    ))
    assert result.status == OperationStatus.FAILED
    assert (root / "p.txt").read_bytes() == b"hello world"


# --- read_file: exact-content evidence, never mislabeled --------------------

def test_read_file_persists_exact_content_as_its_own_evidence_kind(context, db_conn):
    """Phase 7.5b: read_file's bytes ARE persisted (unlike Phase 4), but
    only ever under their own `tool_read_output` classification — never
    mislabeled as `command_output`, and addressable by the exact digest
    `output_hash` already carries."""
    root, task_id, executor, directory = context
    _create(executor, task_id, "readme_target.txt", b"the quick brown fox")

    import hashlib
    expected_hash = hashlib.sha256(b"the quick brown fox").hexdigest()
    result = executor.execute(task_id, ToolRequest(tool="read_file", path="readme_target.txt"))

    assert result.status == OperationStatus.SUCCEEDED
    assert result.output_hash == expected_hash
    blob = ContentStore(db_conn, directory).get_meta(expected_hash)
    assert blob is not None, "read_file's exact bytes must be retrievable as durable evidence"
    assert blob.source_kind == "tool_read_output"
    assert blob.exportable is False
    assert ContentStore(db_conn, directory).read(expected_hash) == b"the quick brown fox"
    op = operations(db_conn, task_id)[-1]
    assert op["after_evidence"] == expected_hash
    del root


def test_read_file_never_persists_a_command_output_blob(context, db_conn):
    root, task_id, executor, directory = context
    _create(executor, task_id, "readme_target.txt", b"the quick brown fox")

    import hashlib
    expected_hash = hashlib.sha256(b"the quick brown fox").hexdigest()
    executor.execute(task_id, ToolRequest(tool="read_file", path="readme_target.txt"))

    row = db_conn.execute(
        "SELECT source_kind FROM content_blobs WHERE content_hash = ?", (expected_hash,),
    ).fetchone()
    assert row["source_kind"] == "tool_read_output", (
        "read_file's bytes must never be classified as command_output"
    )
    del root


def test_read_file_of_empty_file_still_persists_retrievable_evidence(context, db_conn):
    """Unlike `command_output`'s stdout/stderr convention (empty capture
    stores nothing), an empty *file* is a legitimate read result a caller
    must still be able to fetch by hash — it is not "no evidence"."""
    root, task_id, executor, directory = context
    _create(executor, task_id, "empty.txt", b"")

    import hashlib
    expected_hash = hashlib.sha256(b"").hexdigest()
    result = executor.execute(task_id, ToolRequest(tool="read_file", path="empty.txt"))

    assert result.status == OperationStatus.SUCCEEDED
    assert result.output_hash == expected_hash
    assert ContentStore(db_conn, directory).read(expected_hash) == b""
    del root


def test_read_file_reuses_preexisting_blob_under_a_different_classification(context, db_conn):
    """Unlike `run_command`'s `command_output` evidence (which fails
    closed on a classification conflict -- see
    `test_run_command_evidence_classification_conflict_fails_without_
    mislabeling`), read_file evidence tolerates dedup landing on a
    pre-existing blob under a *different* label: content-addressing
    already guarantees identical bytes for an identical hash, and this
    call never relabels or mutates that existing row -- it must simply
    still succeed and remain retrievable, exactly the scenario a
    baseline-time `rules_snapshot` of a recognized document (e.g.
    README.md) produces before any worker ever reads it."""
    root, task_id, executor, directory = context
    _create(executor, task_id, "conflict.txt", b"same bytes")
    pre_existing = ContentStore(db_conn, directory).put(
        b"same bytes", media_type="text/plain", source_kind="rules_snapshot", exportable=True,
    )
    result = executor.execute(task_id, ToolRequest(tool="read_file", path="conflict.txt"))
    assert result.status == OperationStatus.SUCCEEDED
    assert result.output_hash == pre_existing.content_hash
    # The pre-existing blob's own classification is left completely
    # untouched -- no relabeling, no second row.
    still = ContentStore(db_conn, directory).get_meta(pre_existing.content_hash)
    assert still.source_kind == "rules_snapshot"
    assert still.exportable is True
    assert ContentStore(db_conn, directory).read(pre_existing.content_hash) == b"same bytes"
    del root


def test_read_file_allowed_on_protected_pre_existing_path(context, db_conn):
    """Protection guards against Code Slayer overwriting pre-existing user
    changes; it does not (in this phase) restrict reads."""
    root, task_id, executor, _ = context
    (root / "dirty.txt").write_text("already here before baseline")
    # This file exists only after baseline was captured in the fixture, so
    # re-capture is impossible; instead verify read works on a file that is
    # untracked at request time regardless of protection classification.
    result = executor.execute(task_id, ToolRequest(tool="read_file", path="dirty.txt"))
    assert result.status == OperationStatus.SUCCEEDED


# --- run_command -------------------------------------------------------------

def test_run_command_success_persists_command_output_blob(context, db_conn):
    root, task_id, executor, directory = context
    head = git(root, "rev-parse", "HEAD")
    result = executor.execute(task_id, ToolRequest(
        tool="run_command", command=CommandRequest(
            profile="git_rev_parse", executable="git", argv=("HEAD",),
        ),
    ))
    assert result.status == OperationStatus.SUCCEEDED
    assert result.output_hash is not None
    blob = ContentStore(db_conn, directory).get_meta(result.output_hash)
    assert blob is not None
    assert blob.source_kind == "command_output"
    assert blob.exportable is False
    assert ContentStore(db_conn, directory).read(result.output_hash).decode().strip() == head


def test_run_command_invalid_revision_is_a_deterministic_failure(context, db_conn):
    _root, task_id, executor, _ = context
    result = executor.execute(task_id, ToolRequest(
        tool="run_command", command=CommandRequest(
            profile="git_rev_parse", executable="git", argv=("not-a-real-ref",),
        ),
    ))
    assert result.status == OperationStatus.FAILED
    assert result.reason == "command_completed"


def test_run_command_evidence_classification_conflict_fails_without_mislabeling(context, db_conn):
    root, task_id, executor, directory = context
    head = git(root, "rev-parse", "HEAD")
    stdout_bytes = (head + "\n").encode()
    pre_existing = ContentStore(db_conn, directory).put(
        stdout_bytes, media_type="text/plain", source_kind="rules_snapshot", exportable=True,
    )
    result = executor.execute(task_id, ToolRequest(
        tool="run_command", command=CommandRequest(
            profile="git_rev_parse", executable="git", argv=("HEAD",),
        ),
    ))
    assert result.status in (OperationStatus.FAILED, OperationStatus.UNKNOWN)
    # The pre-existing blob's classification must survive untouched.
    still = ContentStore(db_conn, directory).get_meta(pre_existing.content_hash)
    assert still.source_kind == "rules_snapshot"
    assert still.exportable is True


# --- unknown capability / malformed config ----------------------------------

def test_unknown_capability_denied_before_any_journal_entry(context, db_conn):
    _root, task_id, executor, _ = context
    result = executor.execute(task_id, ToolRequest(tool="delete_repository", path="x"))
    assert result.decision == Decision.DENY
    assert operations(db_conn, task_id) == []


def test_malformed_top_level_config_json_denies_without_raising(context, db_conn):
    """Regression: a JSON list (or any non-dict) at the config_json top
    level must not raise AttributeError out of execute() — it must deny."""
    _root, task_id, executor, _ = context
    db_conn.execute("UPDATE tasks SET config_json = ? WHERE task_id = ?", ("[]", task_id))
    result = executor.execute(task_id, ToolRequest(tool="read_file", path="README.md"))
    assert result.decision == Decision.DENY
    assert operations(db_conn, task_id) == []


@pytest.mark.parametrize("config_json", ["123", '"a string"', "true", "null"])
def test_other_non_dict_config_shapes_deny_without_raising(context, db_conn, config_json):
    _root, task_id, executor, _ = context
    db_conn.execute("UPDATE tasks SET config_json = ? WHERE task_id = ?", (config_json, task_id))
    result = executor.execute(task_id, ToolRequest(tool="read_file", path="README.md"))
    assert result.decision == Decision.DENY


def test_missing_tool_policy_key_denies(context, db_conn):
    _root, task_id, executor, _ = context
    db_conn.execute("UPDATE tasks SET config_json = ? WHERE task_id = ?", ("{}", task_id))
    result = executor.execute(task_id, ToolRequest(tool="read_file", path="README.md"))
    assert result.decision == Decision.DENY


def test_empty_scope_list_denies(context, db_conn):
    _root, task_id, executor, _ = context
    db_conn.execute(
        "UPDATE tasks SET config_json = ? WHERE task_id = ?",
        (json.dumps({"tool_policy": {"scope": []}}), task_id),
    )
    result = executor.execute(task_id, ToolRequest(tool="read_file", path="README.md"))
    assert result.decision == Decision.DENY


# --- protected baseline paths -------------------------------------------------

def test_protected_pre_existing_dirty_path_denies_mutation(db_conn, git_repo, tmp_path):
    root = git_repo
    (root / "tracked.txt").write_text("v1")
    commit(root, "seed")
    (root / "tracked.txt").write_text("dirty before baseline")
    info = identity.resolve(root)
    directory = tmp_path / "evidence"
    task = TaskRepo(db_conn).create(
        description="protected", repo_root=str(root), repo_id=info.repo_id,
        worktree_id=info.worktree_id, config={"tool_policy": {"scope": ["."]}},
    )
    service = InspectionService(db_conn, blobs_dir=directory)
    service.start(task.task_id)
    service.capture(task.task_id)
    machine = TaskStateMachine(db_conn)
    machine.transition(task.task_id, expected_state=TaskState.BASELINED,
                        to_state=TaskState.PLANNING, reason="r")
    machine.transition(task.task_id, expected_state=TaskState.PLANNING,
                        to_state=TaskState.PLANNED, reason="r")
    machine.transition(task.task_id, expected_state=TaskState.PLANNED,
                        to_state=TaskState.IMPLEMENTING, reason="r")
    executor = ToolExecutor(db_conn, blobs_dir=directory, lease=acquire_lease(db_conn, task))
    import hashlib

    result = executor.execute(task.task_id, ToolRequest(
        tool="write_file", path="tracked.txt", content=b"overwritten by code slayer",
        expected_hash=hashlib.sha256(b"dirty before baseline").hexdigest(),
    ))
    assert result.decision == Decision.DENY
    assert (root / "tracked.txt").read_text() == "dirty before baseline"
    # Reading it, however, is still allowed (protection only guards writes).
    read = executor.execute(task.task_id, ToolRequest(tool="read_file", path="tracked.txt"))
    assert read.status == OperationStatus.SUCCEEDED


# --- filesystem races: hardlinks -----------------------------------------

def test_hardlinked_owned_path_denies_mutation_before_journal_entry(context, db_conn):
    root, task_id, executor, _ = context
    _create(executor, task_id, "linked.txt", b"v1")
    os.link(root / "linked.txt", root / "linked-alias.txt")
    before = operations(db_conn, task_id)
    import hashlib

    result = executor.execute(task_id, ToolRequest(
        tool="write_file", path="linked.txt", content=b"v2",
        expected_hash=hashlib.sha256(b"v1").hexdigest(),
    ))
    assert result.decision == Decision.DENY
    assert result.reason == "hardlink_mutation_denied"
    assert operations(db_conn, task_id) == before
    assert (root / "linked.txt").read_bytes() == b"v1"


# --- unresolved operations block further mutation ---------------------------

def test_unresolved_started_operation_blocks_further_mutation(context, db_conn):
    root, task_id, executor, _ = context
    task = TaskRepo(db_conn).get(task_id)
    with transaction(db_conn):
        ToolOperationsRepo(db_conn).start_in_transaction(
            task_id=task_id, worktree_id=task.worktree_id, worker_id="external",
            worker_session_id="s", tool_name="write_file", risk_class="WRITE_OWNED",
            request_hash="deadbeef", target_resource="other.txt",
        )
    result = executor.execute(
        task_id, ToolRequest(tool="create_file", path="blocked.txt", content=b"x"),
    )
    assert result.decision == Decision.DENY
    assert result.reason == "reconciliation_required"
    assert not (root / "blocked.txt").exists()


def test_invalid_ownership_evidence_denies(context, db_conn):
    """An ownership row pointing at a non-SUCCEEDED (or mismatched)
    operation must never be trusted as proof of ownership."""
    root, task_id, executor, _ = context
    created = _create(executor, task_id, "tampered.txt", b"v1")
    db_conn.execute(
        "UPDATE tool_operations SET status = 'FAILED' WHERE operation_id = ?",
        (created.operation_id,),
    )
    import hashlib

    result = executor.execute(task_id, ToolRequest(
        tool="write_file", path="tampered.txt", content=b"v2",
        expected_hash=hashlib.sha256(b"v1").hexdigest(),
    ))
    assert result.decision == Decision.DENY
    assert result.reason == "invalid_ownership_evidence"
    assert (root / "tampered.txt").read_bytes() == b"v1"


# --- crash consistency: real subprocess -------------------------------------

_CRASH_BEFORE_MUTATION = """
import os, sys
sys.path.insert(0, sys.argv[1])
from code_slayer.store.db import connect
from code_slayer.lease.manager import LeaseHandle
from code_slayer.tools import command_tools
from code_slayer.tools.executor import ToolExecutor
from code_slayer.tools.models import ToolRequest

conn = connect(sys.argv[2])
real_verify = command_tools.CommandRunner.verify_identity
def crash(self, *a, **kw):
    # STARTED is already durably committed by this point; no filesystem
    # mutation has happened yet. Crash here to exercise that boundary.
    os._exit(74)
command_tools.CommandRunner.verify_identity = crash
# The same session, resumed in a fresh process, reusing its already-
# acquired lease (acquired_at is not part of the fencing comparison).
lease = LeaseHandle(sys.argv[5], sys.argv[4], sys.argv[6], sys.argv[7], int(sys.argv[8]), "")
executor = ToolExecutor(conn, blobs_dir=sys.argv[3], lease=lease)
executor.execute(sys.argv[4], ToolRequest(tool="create_file", path="crash.txt", content=b"x"))
os._exit(1)  # should never reach here
"""


def test_process_crash_after_started_before_mutation_leaves_unresolved_journal(
    context, db_conn,
):
    root, task_id, executor, directory = context
    before = events(db_conn, task_id)
    db_path = db_conn.execute("PRAGMA database_list").fetchone()["file"]
    src = str(Path(__file__).resolve().parents[2] / "src")
    task = TaskRepo(db_conn).get(task_id)
    lease = LeaseRepo(db_conn).get(task.worktree_id)  # the fixture's own current lease
    result = subprocess.run(
        [
            sys.executable, "-c", _CRASH_BEFORE_MUTATION, src, db_path, str(directory), task_id,
            lease.worktree_id, lease.worker_id, lease.worker_session_id, str(lease.generation),
        ],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 74, result.stderr

    reopened = connect(db_path)
    try:
        ops = [dict(r) for r in reopened.execute(
            "SELECT * FROM tool_operations WHERE task_id = ? AND target_resource = 'crash.txt'",
            (task_id,),
        )]
        assert len(ops) == 1
        assert ops[0]["status"] == OperationStatus.STARTED
        unresolved = ToolOperationsRepo(reopened).list_unresolved(task_id=task_id)
        assert [o.operation_id for o in unresolved] == [ops[0]["operation_id"]]
        assert not (root / "crash.txt").exists(), (
            "the crash happened before any filesystem mutation; the file must not exist"
        )
        owned = [dict(r) for r in reopened.execute(
            "SELECT * FROM task_owned_paths WHERE task_id = ? AND path = 'crash.txt'", (task_id,),
        )]
        assert owned == [], "a crash before mutation must never grant false ownership"
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        reopened.close()

    # The worktree must not silently accept further mutations while this
    # operation is unresolved (no blind replay of an uncertain outcome).
    retry = executor.execute(
        task_id, ToolRequest(tool="create_file", path="crash.txt", content=b"y"),
    )
    assert retry.decision == Decision.DENY
    assert retry.reason == "reconciliation_required"
    assert not (root / "crash.txt").exists()

    # Only after explicit reconciliation (never automatic) can work resume.
    stuck_op = [o for o in operations(db_conn, task_id) if o["target_resource"] == "crash.txt"][0]
    ToolOperationsRepo(db_conn).finish(
        stuck_op["operation_id"], status=OperationStatus.FAILED,
        result={"reconciled": "confirmed no filesystem mutation occurred"},
    )
    resumed = executor.execute(
        task_id, ToolRequest(tool="create_file", path="crash.txt", content=b"z"),
    )
    assert resumed.status == OperationStatus.SUCCEEDED
    assert (root / "crash.txt").read_bytes() == b"z"
    assert len(events(db_conn, task_id)) > len(before)
    assert verify_chain(db_conn, task_id=task_id).ok
