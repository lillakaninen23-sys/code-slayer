"""End-to-end CheckpointManager behavior against real temporary Git repos.

Covers: normal checkpoint creation and its Git/journal/state identity,
dirty-repository preservation (the user's index/branch/untracked files are
never touched), baseline/ownership drift detection, policy denial, and
real-subprocess crash/restart recovery across the Git/SQLite durability
boundary — without weakening any Phase 1-4 invariant.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from code_slayer.audit.verify import verify_chain
from code_slayer.core import TaskState, TaskStateMachine
from code_slayer.policy.engine import Decision
from code_slayer.repo import checkpoint_git as cg
from code_slayer.repo import identity
from code_slayer.repo.baseline import InspectionService
from code_slayer.repo.checkpoint import CheckpointManager
from code_slayer.store.checkpoint_repo import CheckpointRepo, parse_verified
from code_slayer.store.db import connect, transaction
from code_slayer.store.lease_repo import LeaseRepo
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.tool_operations_repo import OperationStatus, ToolOperationsRepo
from code_slayer.tools.executor import ToolExecutor
from code_slayer.tools.models import ToolRequest
from tests.repo_helpers import acquire_lease, commit, filesystem_snapshot, git


def working_tree_snapshot(root):
    """`filesystem_snapshot`, excluding `.git/` internals: checkpoint
    creation legitimately adds new objects/refs there, but must never
    touch a single byte of the user's actual working-tree files."""
    return {
        path: value for path, value in filesystem_snapshot(root).items()
        if not path.startswith(".git" + os.sep) and path != ".git"
    }


def _advance(machine, task_id, *pairs):
    for expected, to_state in pairs:
        machine.transition(task_id, expected_state=expected, to_state=to_state, reason="progress")


@pytest.fixture
def context(db_conn, git_repo_with_commit, tmp_path):
    root = git_repo_with_commit
    info = identity.resolve(root)
    blobs_dir = tmp_path / "evidence"
    tmp_dir = tmp_path / "tmp"
    task = TaskRepo(db_conn).create(
        description="checkpoint flow", repo_root=str(root), repo_id=info.repo_id,
        worktree_id=info.worktree_id, config={"tool_policy": {"scope": ["."]}},
    )
    service = InspectionService(db_conn, blobs_dir=blobs_dir)
    service.start(task.task_id)
    service.capture(task.task_id)
    machine = TaskStateMachine(db_conn)
    _advance(machine, task.task_id,
              (TaskState.BASELINED, TaskState.PLANNING),
              (TaskState.PLANNING, TaskState.PLANNED),
              (TaskState.PLANNED, TaskState.IMPLEMENTING))
    lease = acquire_lease(db_conn, task)
    executor = ToolExecutor(db_conn, blobs_dir=blobs_dir, lease=lease)
    manager = CheckpointManager(db_conn, blobs_dir=blobs_dir, tmp_dir=tmp_dir, lease=lease)
    return root, task.task_id, executor, manager, blobs_dir, tmp_dir


def own_file(executor, task_id, path, content):
    result = executor.execute(task_id, ToolRequest(tool="create_file", path=path, content=content))
    assert result.status == OperationStatus.SUCCEEDED
    return result


def ready_for_checkpoint(db_conn, task_id):
    machine = TaskStateMachine(db_conn)
    _advance(machine, task_id,
              (TaskState.IMPLEMENTING, TaskState.VERIFYING),
              (TaskState.VERIFYING, TaskState.REVIEWING),
              (TaskState.REVIEWING, TaskState.READY_FOR_CHECKPOINT))


def events(conn, task_id):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM audit_events WHERE task_id = ? ORDER BY seq", (task_id,),
    )]


def event_types(conn, task_id):
    return [e["event_type"] for e in events(conn, task_id)]


# --- normal checkpoint -------------------------------------------------

def test_checkpoint_success_creates_verifiable_commit_and_state_transition(context, db_conn):
    root, task_id, executor, manager, _blobs, _tmp = context
    own_file(executor, task_id, "owned.txt", b"owned content")
    ready_for_checkpoint(db_conn, task_id)

    result = manager.create(task_id)

    assert result.decision == Decision.ALLOW
    assert result.operation_status == OperationStatus.SUCCEEDED
    assert result.commit_sha and result.tree_sha and result.checkpoint_id
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"

    listing = git(root, "ls-tree", "-r", "--name-only", result.tree_sha).splitlines()
    assert "owned.txt" in listing
    assert "README.md" in listing  # baseline content carried through
    blob = git(root, "cat-file", "-p", f"{result.commit_sha}:owned.txt")
    assert blob == "owned content"

    ref = f"refs/codeslayer/checkpoints/{task_id}/0"
    assert git(root, "rev-parse", ref) == result.commit_sha
    assert git(root, "rev-parse", "HEAD") != result.commit_sha  # branch untouched

    checkpoint = CheckpointRepo(db_conn).get(result.checkpoint_id)
    assert checkpoint.status == "COMPLETE"
    assert checkpoint.git_ref == ref
    verified = parse_verified(checkpoint)
    assert verified.commit_sha == result.commit_sha
    assert verified.tree_sha == result.tree_sha
    assert ("owned.txt", hashlib.sha256(b"owned content").hexdigest()) in verified.owned_paths

    assert "CHECKPOINT_CREATED" in event_types(db_conn, task_id)
    assert "CHECKPOINT_VALIDATED" in event_types(db_conn, task_id)
    assert verify_chain(db_conn, task_id=task_id).ok


def test_checkpoint_commit_root_parent_is_baseline_head(context, db_conn):
    root, task_id, executor, manager, *_ = context
    baseline_head = git(root, "rev-parse", "HEAD")
    own_file(executor, task_id, "owned.txt", b"x")
    ready_for_checkpoint(db_conn, task_id)
    result = manager.create(task_id)
    _tree, parents = cg.read_commit(result.commit_sha, cwd=root)
    assert parents == (baseline_head,)


def test_second_checkpoint_chains_onto_the_first(context, db_conn):
    root, task_id, executor, manager, *_ = context
    own_file(executor, task_id, "first.txt", b"one")
    ready_for_checkpoint(db_conn, task_id)
    first = manager.create(task_id)
    assert first.seq == 0

    machine = TaskStateMachine(db_conn)
    machine.transition(task_id, expected_state=TaskState.CHECKPOINTED,
                        to_state=TaskState.IMPLEMENTING, reason="more work")
    own_file(executor, task_id, "second.txt", b"two")
    ready_for_checkpoint(db_conn, task_id)
    second = manager.create(task_id)

    assert second.seq == 1
    tree_sha, parents = cg.read_commit(second.commit_sha, cwd=root)
    assert parents == (first.commit_sha,)
    listing = git(root, "ls-tree", "-r", "--name-only", tree_sha).splitlines()
    assert {"first.txt", "second.txt", "README.md"} <= set(listing)
    checkpoint = CheckpointRepo(db_conn).get(second.checkpoint_id)
    assert checkpoint.parent_checkpoint == first.checkpoint_id


# --- dirty repository preservation ---------------------------------------

def test_unrelated_untracked_file_excluded_and_preserved(context, db_conn):
    root, task_id, executor, manager, *_ = context
    (root / "unrelated.txt").write_text("user's own scratch file")
    own_file(executor, task_id, "owned.txt", b"owned")
    ready_for_checkpoint(db_conn, task_id)
    before = working_tree_snapshot(root)

    result = manager.create(task_id)

    assert result.operation_status == OperationStatus.SUCCEEDED
    listing = git(root, "ls-tree", "-r", "--name-only", result.tree_sha).splitlines()
    assert "unrelated.txt" not in listing
    assert working_tree_snapshot(root) == before
    assert (root / "unrelated.txt").read_text() == "user's own scratch file"


def test_pre_existing_staged_change_is_never_touched(context, db_conn):
    root, task_id, executor, manager, *_ = context
    # This file must exist BEFORE baseline capture to be legitimately
    # "pre-existing"; the fixture already captured baseline, so instead
    # verify the *index itself* (staged intent) survives untouched even
    # though it changes after baseline (simulating the user staging work
    # concurrently with Code Slayer's task).
    (root / "README.md").write_text("user edit, staged\n")
    git(root, "add", "README.md")
    index_before = (root / ".git" / "index").read_bytes()

    own_file(executor, task_id, "owned.txt", b"owned")
    ready_for_checkpoint(db_conn, task_id)
    manager.create(task_id)

    # The real index is byte-for-byte untouched (checkpoint construction
    # only ever writes to a private, temporary index file) — README.md's
    # staged change is still exactly as the user left it, still staged.
    assert (root / ".git" / "index").read_bytes() == index_before
    status_lines = git(root, "status", "--porcelain").splitlines()
    assert "M  README.md" in status_lines


def test_rename_case_preserved_and_excluded(context, db_conn):
    root, task_id, executor, manager, *_ = context
    git(root, "mv", "README.md", "RENAMED.md")
    own_file(executor, task_id, "owned.txt", b"owned")
    ready_for_checkpoint(db_conn, task_id)
    before = working_tree_snapshot(root)
    result = manager.create(task_id)
    assert result.operation_status == OperationStatus.SUCCEEDED
    assert working_tree_snapshot(root) == before


# --- drift detection -------------------------------------------------------

def test_head_moved_externally_denies_checkpoint(context, db_conn):
    root, task_id, executor, manager, *_ = context
    own_file(executor, task_id, "owned.txt", b"owned")
    ready_for_checkpoint(db_conn, task_id)
    (root / "extra.txt").write_text("external commit")
    commit(root, "external work")

    result = manager.create(task_id)

    assert result.decision == Decision.DENY
    assert result.reason == "baseline_drift_detected"
    assert TaskRepo(db_conn).get(task_id).state == "READY_FOR_CHECKPOINT"
    assert CheckpointRepo(db_conn).list_for_task(task_id) == []
    assert "EXTERNAL_MODIFICATION_DETECTED" in event_types(db_conn, task_id)


def test_owned_file_modified_externally_denies_checkpoint(context, db_conn):
    root, task_id, executor, manager, *_ = context
    own_file(executor, task_id, "owned.txt", b"original")
    ready_for_checkpoint(db_conn, task_id)
    (root / "owned.txt").write_text("tampered after the fact")

    result = manager.create(task_id)

    assert result.decision == Decision.DENY
    assert result.reason == "owned_content_changed"
    assert TaskRepo(db_conn).get(task_id).state == "READY_FOR_CHECKPOINT"


def test_owned_file_replaced_with_symlink_denies_checkpoint(context, db_conn):
    root, task_id, executor, manager, *_ = context
    own_file(executor, task_id, "owned.txt", b"original")
    ready_for_checkpoint(db_conn, task_id)
    (root / "owned.txt").unlink()
    (root / "elsewhere.txt").write_text("outside content")
    (root / "owned.txt").symlink_to(root / "elsewhere.txt")

    result = manager.create(task_id)

    assert result.decision == Decision.DENY
    assert result.reason == "owned_content_changed"
    assert (root / "owned.txt").is_symlink()  # untouched, not "fixed" or removed


def test_owned_file_deleted_externally_denies_checkpoint(context, db_conn):
    root, task_id, executor, manager, *_ = context
    own_file(executor, task_id, "owned.txt", b"original")
    ready_for_checkpoint(db_conn, task_id)
    (root / "owned.txt").unlink()

    result = manager.create(task_id)

    assert result.decision == Decision.DENY
    assert result.reason == "owned_content_changed"


# --- policy / state gating -------------------------------------------------

@pytest.mark.parametrize("state", [
    TaskState.IMPLEMENTING, TaskState.VERIFYING, TaskState.REVIEWING,
])
def test_wrong_state_denies_before_ready_for_checkpoint(context, db_conn, state):
    root, task_id, executor, manager, *_ = context
    own_file(executor, task_id, "owned.txt", b"x")
    machine = TaskStateMachine(db_conn)
    pairs = {
        TaskState.IMPLEMENTING: [],
        TaskState.VERIFYING: [(TaskState.IMPLEMENTING, TaskState.VERIFYING)],
        TaskState.REVIEWING: [(TaskState.IMPLEMENTING, TaskState.VERIFYING),
                               (TaskState.VERIFYING, TaskState.REVIEWING)],
    }[state]
    _advance(machine, task_id, *pairs)

    result = manager.create(task_id)

    assert result.decision == Decision.DENY
    assert result.reason == "wrong_task_state"
    count = db_conn.execute(
        "SELECT count(*) FROM tool_operations WHERE task_id = ? AND tool_name = ?",
        (task_id, "checkpoint_create"),
    ).fetchone()[0]
    assert count == 0


def test_unresolved_file_operation_blocks_checkpoint(context, db_conn):
    task_id = context[1]
    task = TaskRepo(db_conn).get(task_id)
    with transaction(db_conn):
        ToolOperationsRepo(db_conn).start_in_transaction(
            task_id=task_id, worktree_id=task.worktree_id, worker_id="external",
            worker_session_id="s", tool_name="write_file", risk_class="WRITE_OWNED",
            request_hash="deadbeef", target_resource="stuck.txt",
        )
    ready_for_checkpoint(db_conn, task_id)
    manager = context[3]

    result = manager.create(task_id)

    assert result.decision == Decision.DENY
    assert result.reason == "reconciliation_required"


def test_no_owned_paths_still_checkpoints_baseline_alone(context, db_conn):
    root, task_id, _executor, manager, *_ = context
    ready_for_checkpoint(db_conn, task_id)
    result = manager.create(task_id)
    assert result.operation_status == OperationStatus.SUCCEEDED
    assert git(root, "ls-tree", "-r", "--name-only", result.tree_sha).splitlines() == ["README.md"]


# --- crash consistency: real subprocess -----------------------------------

_LEASE_PREAMBLE = """
from code_slayer.lease.manager import LeaseHandle
# The same session, resumed in a fresh process, reusing its already-
# acquired lease (acquired_at is not part of the fencing comparison).
lease = LeaseHandle(sys.argv[6], sys.argv[5], sys.argv[7], sys.argv[8], int(sys.argv[9]), "")
"""

_CRASH_AFTER_STARTED = """
import os, sys
sys.path.insert(0, sys.argv[1])
from code_slayer.store.db import connect
from code_slayer.repo.checkpoint import CheckpointManager
conn = connect(sys.argv[2])
""" + _LEASE_PREAMBLE + """
manager = CheckpointManager(conn, blobs_dir=sys.argv[3], tmp_dir=sys.argv[4], lease=lease)
import code_slayer.repo.checkpoint_git as cg
real_build_tree = cg.build_tree
def crash(*a, **kw):
    os._exit(74)
cg.build_tree = crash
manager.create(sys.argv[5])
os._exit(1)
"""

_CRASH_DURING_GIT_WORK = """
import os, sys
sys.path.insert(0, sys.argv[1])
from code_slayer.store.db import connect
from code_slayer.repo.checkpoint import CheckpointManager
conn = connect(sys.argv[2])
""" + _LEASE_PREAMBLE + """
manager = CheckpointManager(conn, blobs_dir=sys.argv[3], tmp_dir=sys.argv[4], lease=lease)
import code_slayer.repo.checkpoint_git as cg
real_commit_tree = cg.commit_tree
def crash(*a, **kw):
    os._exit(74)
cg.commit_tree = crash
manager.create(sys.argv[5])
os._exit(1)
"""

_CRASH_AFTER_REF_BEFORE_FINALIZE = """
import os, sys
sys.path.insert(0, sys.argv[1])
from code_slayer.store.db import connect
from code_slayer.repo.checkpoint import CheckpointManager
conn = connect(sys.argv[2])
""" + _LEASE_PREAMBLE + """
manager = CheckpointManager(conn, blobs_dir=sys.argv[3], tmp_dir=sys.argv[4], lease=lease)
real_finalize = CheckpointManager._finalize
def crash(self, *a, **kw):
    os._exit(74)
CheckpointManager._finalize = crash
manager.create(sys.argv[5])
os._exit(1)
"""


def _current_lease_handle(conn, task_id):
    from code_slayer.lease.manager import LeaseHandle

    task = TaskRepo(conn).get(task_id)
    lease = LeaseRepo(conn).get(task.worktree_id)
    return LeaseHandle(
        lease.worktree_id, lease.task_id, lease.worker_id, lease.worker_session_id,
        lease.generation, lease.acquired_at,
    )


def _run_crash(script, context, db_conn):
    root, task_id, _executor, _manager, blobs_dir, tmp_dir = context
    db_path = db_conn.execute("PRAGMA database_list").fetchone()["file"]
    src = str(Path(__file__).resolve().parents[2] / "src")
    task = TaskRepo(db_conn).get(task_id)
    lease = LeaseRepo(db_conn).get(task.worktree_id)
    result = subprocess.run(
        [
            sys.executable, "-c", script, src, db_path, str(blobs_dir), str(tmp_dir), task_id,
            lease.worktree_id, lease.worker_id, lease.worker_session_id, str(lease.generation),
        ],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 74, result.stderr
    return root, task_id, blobs_dir, tmp_dir, db_path


def test_crash_after_started_before_any_git_work_reconciles_as_failed(context, db_conn, tmp_path):
    root, task_id, executor, manager, blobs_dir, tmp_dir = context
    own_file(executor, task_id, "owned.txt", b"x")
    ready_for_checkpoint(db_conn, task_id)

    root, task_id, blobs_dir, tmp_dir, db_path = _run_crash(_CRASH_AFTER_STARTED, context, db_conn)

    reopened = connect(db_path)
    try:
        op = reopened.execute(
            "SELECT * FROM tool_operations WHERE task_id = ? AND tool_name = 'checkpoint_create'",
            (task_id,),
        ).fetchone()
        assert op["status"] == OperationStatus.STARTED
        ref = f"refs/codeslayer/checkpoints/{task_id}/0"
        assert cg.resolve_ref(ref, cwd=root) is None  # no git side effect happened

        manager2 = CheckpointManager(
            reopened, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
            lease=_current_lease_handle(reopened, task_id),
        )
        reconciled = manager2.reconcile(task_id)
        assert reconciled is None  # nothing to finalize: it failed, not succeeded
        op_after = reopened.execute(
            "SELECT status FROM tool_operations WHERE operation_id = ?", (op["operation_id"],),
        ).fetchone()
        assert op_after["status"] == OperationStatus.FAILED
        assert reopened.execute(
            "SELECT state FROM tasks WHERE task_id = ?", (task_id,),
        ).fetchone()["state"] == "READY_FOR_CHECKPOINT"

        # A fresh attempt now succeeds cleanly, reusing the same seq/ref.
        retry = manager2.create(task_id)
        assert retry.operation_status == OperationStatus.SUCCEEDED
        assert retry.seq == 0
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert verify_chain(reopened, task_id=task_id).ok
    finally:
        reopened.close()


def test_crash_during_git_work_reconciles_as_failed_no_dangling_ref(context, db_conn, tmp_path):
    root, task_id, executor, manager, blobs_dir, tmp_dir = context
    own_file(executor, task_id, "owned.txt", b"x")
    ready_for_checkpoint(db_conn, task_id)

    root, task_id, blobs_dir, tmp_dir, db_path = _run_crash(
        _CRASH_DURING_GIT_WORK, context, db_conn,
    )

    reopened = connect(db_path)
    try:
        ref = f"refs/codeslayer/checkpoints/{task_id}/0"
        assert cg.resolve_ref(ref, cwd=root) is None
        manager2 = CheckpointManager(
            reopened, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
            lease=_current_lease_handle(reopened, task_id),
        )
        assert manager2.reconcile(task_id) is None
        op = reopened.execute(
            "SELECT status FROM tool_operations WHERE task_id = ? AND tool_name = ?",
            (task_id, "checkpoint_create"),
        ).fetchone()
        assert op["status"] == OperationStatus.FAILED
        retry = manager2.create(task_id)
        assert retry.operation_status == OperationStatus.SUCCEEDED
    finally:
        reopened.close()


def test_crash_after_ref_created_before_finalize_reconciles_as_succeeded(
    context, db_conn, tmp_path,
):
    root, task_id, executor, manager, blobs_dir, tmp_dir = context
    own_file(executor, task_id, "owned.txt", b"x")
    ready_for_checkpoint(db_conn, task_id)

    root, task_id, blobs_dir, tmp_dir, db_path = _run_crash(
        _CRASH_AFTER_REF_BEFORE_FINALIZE, context, db_conn,
    )

    reopened = connect(db_path)
    try:
        ref = f"refs/codeslayer/checkpoints/{task_id}/0"
        commit_sha = cg.resolve_ref(ref, cwd=root)
        assert commit_sha is not None  # the git mutation genuinely succeeded

        op = reopened.execute(
            "SELECT * FROM tool_operations WHERE task_id = ? AND tool_name = 'checkpoint_create'",
            (task_id,),
        ).fetchone()
        assert op["status"] == OperationStatus.STARTED  # finalize never committed
        assert reopened.execute(
            "SELECT state FROM tasks WHERE task_id = ?", (task_id,),
        ).fetchone()["state"] == "READY_FOR_CHECKPOINT"
        assert CheckpointRepo(reopened).list_for_task(task_id) == []

        # create() itself, called fresh — not a separate reconcile() call —
        # must pick this up automatically rather than attempting (or
        # duplicating) a new checkpoint.
        manager2 = CheckpointManager(
            reopened, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
            lease=_current_lease_handle(reopened, task_id),
        )
        reconciled = manager2.create(task_id)

        assert reconciled.operation_status == OperationStatus.SUCCEEDED
        assert reconciled.commit_sha == commit_sha
        assert reopened.execute(
            "SELECT state FROM tasks WHERE task_id = ?", (task_id,),
        ).fetchone()["state"] == "CHECKPOINTED"
        assert reopened.execute(
            "SELECT status FROM tool_operations WHERE operation_id = ?", (op["operation_id"],),
        ).fetchone()["status"] == OperationStatus.SUCCEEDED
        checkpoint = CheckpointRepo(reopened).get(reconciled.checkpoint_id)
        assert checkpoint.git_ref == ref
        assert reopened.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert verify_chain(reopened, task_id=task_id).ok

        # A second checkpoint attempt on the now-CHECKPOINTED task creates
        # nothing new — there is no more pending operation to reconcile,
        # and READY_FOR_CHECKPOINT is no longer the task's state.
        again = CheckpointManager(
            reopened, blobs_dir=blobs_dir, tmp_dir=tmp_dir,
            lease=_current_lease_handle(reopened, task_id),
        )
        result = again.create(task_id)
        assert result.decision == Decision.DENY
        assert result.reason == "wrong_task_state"
        assert CheckpointRepo(reopened).list_for_task(task_id) == [checkpoint]
    finally:
        reopened.close()


def test_reconcile_returns_none_when_nothing_pending(context):
    manager = context[3]
    task_id = context[1]
    assert manager.reconcile(task_id) is None


def test_ref_check_failure_during_reconcile_marks_unknown_not_a_guess(
    context, db_conn, monkeypatch,
):
    root, task_id, executor, manager, *_ = context
    task = TaskRepo(db_conn).get(task_id)
    with transaction(db_conn):
        op = ToolOperationsRepo(db_conn).start_in_transaction(
            task_id=task_id, worktree_id=task.worktree_id, worker_id="checkpoint-manager",
            worker_session_id="s", tool_name="checkpoint_create", risk_class="GIT_MUTATION",
            request_hash="deadbeef", target_resource=f"refs/codeslayer/checkpoints/{task_id}/0",
        )

    def boom(*a, **kw):
        raise RuntimeError("git unavailable")

    monkeypatch.setattr(cg, "resolve_ref", boom)
    result = manager.reconcile(task_id)
    assert result is None
    reloaded = ToolOperationsRepo(db_conn).get(op.operation_id)
    assert reloaded.status == OperationStatus.UNKNOWN


def test_reconcile_failure_denies_closed_instead_of_raising(context, db_conn, monkeypatch):
    """Regression: if reconciliation itself cannot even run (e.g. the
    baseline manifest is unreadable), create()/reconcile() must never
    raise past the caller — they must fail closed."""
    root, task_id, executor, manager, *_ = context
    task = TaskRepo(db_conn).get(task_id)
    with transaction(db_conn):
        ToolOperationsRepo(db_conn).start_in_transaction(
            task_id=task_id, worktree_id=task.worktree_id, worker_id="checkpoint-manager",
            worker_session_id="s", tool_name="checkpoint_create", risk_class="GIT_MUTATION",
            request_hash="deadbeef", target_resource=f"refs/codeslayer/checkpoints/{task_id}/0",
        )
    ready_for_checkpoint(db_conn, task_id)

    def boom(self, task_id):
        raise KeyError("baseline vanished")

    monkeypatch.setattr(InspectionService, "read_manifest", boom)

    reconciled = manager.reconcile(task_id)
    assert reconciled is None

    result = manager.create(task_id)
    assert result.decision == Decision.DENY
    assert result.reason == "invalid_context_or_request"


def test_finalize_failure_after_commit_reports_unknown_not_a_crash(context, db_conn, monkeypatch):
    """Regression: a failure while writing the checkpoints row/state
    transition — *after* the Git commit and ref already exist — must
    surface as an UNKNOWN outcome the caller can retry, never an
    exception raised past create()."""
    root, task_id, executor, manager, *_ = context
    own_file(executor, task_id, "owned.txt", b"x")
    ready_for_checkpoint(db_conn, task_id)

    from code_slayer.repo.checkpoint import CheckpointManager as CM

    def boom(self, **kwargs):
        raise RuntimeError("disk full writing checkpoints row")

    monkeypatch.setattr(CM, "_finalize", boom)
    result = manager.create(task_id)

    assert result.decision == Decision.ALLOW
    assert result.operation_status == OperationStatus.UNKNOWN
    assert result.commit_sha is not None
    assert TaskRepo(db_conn).get(task_id).state == "READY_FOR_CHECKPOINT"

    monkeypatch.undo()
    ref = f"refs/codeslayer/checkpoints/{task_id}/0"
    assert cg.resolve_ref(ref, cwd=root) == result.commit_sha
    op = db_conn.execute(
        "SELECT status FROM tool_operations WHERE task_id = ? AND tool_name = 'checkpoint_create'",
        (task_id,),
    ).fetchone()
    assert op["status"] == OperationStatus.STARTED

    retry = manager.create(task_id)
    assert retry.operation_status == OperationStatus.SUCCEEDED
    assert retry.commit_sha == result.commit_sha
    assert TaskRepo(db_conn).get(task_id).state == "CHECKPOINTED"
