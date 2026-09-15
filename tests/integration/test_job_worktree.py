"""Isolated, disposable job worktrees (Phase 7.5c) against real temporary
Git repositories.

Covers: Code-Slayer-owned (never model-chosen) job worktree creation
pinned to an explicit, frozen base revision; shared repo_id/distinct
worktree_id identity and the state (db/blobs/tmp) separation that falls
out of it "for free" via `store.location`; a real `ToolExecutor` mutation
confined entirely to the job worktree, provably invisible to and without
side effect on the primary worktree; a real `CheckpointManager` checkpoint
built from that mutation, likewise without touching the primary worktree;
existing lease/policy/path-confinement behavior holding unmodified inside
a job worktree; and conservative, refusal-first cleanup.
"""

from __future__ import annotations

import inspect
import os

import pytest

from code_slayer.audit.verify import verify_chain
from code_slayer.core import TaskState, TaskStateMachine
from code_slayer.policy.engine import Decision
from code_slayer.repo import identity
from code_slayer.repo.baseline import InspectionService
from code_slayer.repo.checkpoint import CheckpointManager
from code_slayer.repo.job_worktree import (
    JobWorktreeSetupIncompleteError,
    create_job_worktree,
    read_job_worktree_metadata,
    release_job_worktree,
)
from code_slayer.repo.job_worktree_git import JobWorktreeGitError
from code_slayer.store.db import connect
from code_slayer.store.task_repo import TaskRepo
from code_slayer.store.tool_operations_repo import OperationStatus, ToolOperationsRepo
from code_slayer.tools.executor import ToolExecutor
from code_slayer.tools.models import ToolRequest
from tests.repo_helpers import acquire_lease, filesystem_snapshot, git

README_BYTES = b"hello\n"


def working_tree_snapshot(root):
    """`filesystem_snapshot`, excluding `.git/` internals: a checkpoint
    created from a job worktree legitimately adds new objects to the
    SHARED object database under `.git/` (Git's own object/ref storage is
    shared across every linked worktree of one repository -- this phase's
    own docs call that out as acceptable), but must never touch a single
    byte of the primary worktree's actual working-tree files."""
    return {
        path: value for path, value in filesystem_snapshot(root).items()
        if not path.startswith(".git" + os.sep) and path != ".git"
    }


def _advance(machine, task_id, *pairs):
    for expected, to_state in pairs:
        machine.transition(task_id, expected_state=expected, to_state=to_state, reason="progress")


def _make_task(conn, handle, *, description="job worktree task"):
    task = TaskRepo(conn).create(
        description=description, repo_root=str(handle.path), repo_id=handle.repo_id,
        worktree_id=handle.worktree_id, config={"tool_policy": {"scope": ["."]}},
    )
    service = InspectionService(conn, blobs_dir=handle.blobs_dir)
    service.start(task.task_id)
    service.capture(task.task_id)
    return task


def _to_implementing(conn, task_id):
    machine = TaskStateMachine(conn)
    _advance(machine, task_id,
              (TaskState.BASELINED, TaskState.PLANNING),
              (TaskState.PLANNING, TaskState.PLANNED),
              (TaskState.PLANNED, TaskState.IMPLEMENTING))


def _to_ready_for_checkpoint(conn, task_id):
    machine = TaskStateMachine(conn)
    _advance(machine, task_id,
              (TaskState.IMPLEMENTING, TaskState.VERIFYING),
              (TaskState.VERIFYING, TaskState.REVIEWING),
              (TaskState.REVIEWING, TaskState.READY_FOR_CHECKPOINT))


@pytest.fixture
def primary(git_repo_with_commit):
    return git_repo_with_commit


@pytest.fixture
def job(primary):
    """A freshly created job worktree, with a task already baselined and
    advanced to IMPLEMENTING, plus a real ToolExecutor holding a valid
    lease -- ready for a mutation."""
    handle = create_job_worktree(primary)
    conn = connect(handle.db_path)
    task = _make_task(conn, handle)
    _to_implementing(conn, task.task_id)
    lease = acquire_lease(conn, task)
    executor = ToolExecutor(conn, blobs_dir=handle.blobs_dir, lease=lease)
    yield primary, handle, conn, task.task_id, executor, lease
    conn.close()


# --- 1/2/3/4/5. creation, base revision, identity, state isolation ---------

def test_managed_job_worktree_created_successfully(primary):
    handle = create_job_worktree(primary)
    assert handle.path.is_dir()
    assert (handle.path / "README.md").read_bytes() == README_BYTES
    assert (handle.path / ".git").exists()
    assert not handle.path.is_relative_to(primary)


def test_explicit_base_commit_is_used(primary):
    primary_head = git(primary, "rev-parse", "HEAD")
    (primary / "second.txt").write_text("second commit content\n")
    git(primary, "add", "second.txt")
    git(primary, "commit", "-qm", "second commit")
    new_head = git(primary, "rev-parse", "HEAD")
    assert new_head != primary_head

    handle = create_job_worktree(primary, base_revision=primary_head)
    assert handle.base_revision == primary_head
    assert git(handle.path, "rev-parse", "HEAD") == primary_head
    # Pinned to the OLD revision -- the later commit's content must be
    # entirely absent, proving this is not "whatever HEAD is now".
    assert not (handle.path / "second.txt").exists()

    # Detached, not a branch: nothing was created or moved on the user's
    # branches, and the job worktree itself has no branch checked out.
    assert git(handle.path, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert git(primary, "rev-parse", "HEAD") == new_head  # primary untouched


def test_base_revision_defaults_to_primary_head(primary):
    head = git(primary, "rev-parse", "HEAD")
    handle = create_job_worktree(primary)
    assert handle.base_revision == head


def test_repo_id_matches_primary(primary):
    primary_identity = identity.resolve(primary)
    handle = create_job_worktree(primary)
    assert handle.repo_id == primary_identity.repo_id


def test_worktree_id_differs_from_primary(primary):
    primary_identity = identity.resolve(primary)
    handle = create_job_worktree(primary)
    assert handle.worktree_id != primary_identity.worktree_id


def test_state_db_and_blobs_and_tmp_differ_from_primary(primary, tmp_path):
    from code_slayer.store import location

    primary_identity = identity.resolve(primary)
    handle = create_job_worktree(primary)

    primary_db = location.db_path(primary_identity.repo_id, primary_identity.worktree_id)
    primary_blobs = location.blobs_dir(primary_identity.repo_id, primary_identity.worktree_id)
    primary_tmp = location.tmp_dir(primary_identity.repo_id, primary_identity.worktree_id)

    assert handle.db_path != primary_db
    assert handle.blobs_dir != primary_blobs
    assert handle.tmp_dir != primary_tmp
    assert handle.db_path.exists()
    assert not primary_db.exists()  # never created merely by creating a job worktree


def test_worker_or_model_cannot_select_arbitrary_job_worktree_path():
    """`create_job_worktree()` has no path-shaped parameter at all --
    structurally, no caller (a worker, a prompt, a model response) can
    ever choose where a job worktree lives."""
    params = set(inspect.signature(create_job_worktree).parameters)
    assert params == {"primary_path", "base_revision", "state_root_override"}


def test_job_worktree_metadata_sidecar_is_durable(primary):
    handle = create_job_worktree(primary)
    metadata = read_job_worktree_metadata(handle.state_dir)
    assert metadata is not None
    assert metadata["repo_id"] == handle.repo_id
    assert metadata["worktree_id"] == handle.worktree_id
    assert metadata["base_revision"] == handle.base_revision
    assert metadata["path"] == str(handle.path)
    assert metadata["primary_repo_root"] == str(primary)


def test_job_worktree_created_audit_event_recorded(primary):
    handle = create_job_worktree(primary)
    conn = connect(handle.db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM audit_events WHERE event_type = 'JOB_WORKTREE_CREATED'"
        )]
        assert len(rows) == 1
        assert rows[0]["task_id"] is None
        assert verify_chain(conn, task_id=None).ok
    finally:
        conn.close()


def test_separate_job_worktrees_receive_distinct_ids_and_state(primary):
    first = create_job_worktree(primary)
    second = create_job_worktree(primary)
    assert first.worktree_id != second.worktree_id
    assert first.path != second.path
    assert first.db_path != second.db_path
    assert first.repo_id == second.repo_id == identity.resolve(primary).repo_id


# --- 7/8. real ToolExecutor mutation, confined to the job worktree ---------

def test_real_tool_executor_mutation_succeeds_inside_job_worktree(job):
    _primary, handle, _conn, task_id, executor, _lease = job
    result = executor.execute(
        task_id, ToolRequest(tool="create_file", path="job-output.txt", content=b"from the job"),
    )
    assert result.status == OperationStatus.SUCCEEDED
    assert (handle.path / "job-output.txt").read_bytes() == b"from the job"


def test_mutation_does_not_appear_in_primary(job):
    primary, _handle, _conn, task_id, executor, _lease = job
    executor.execute(
        task_id, ToolRequest(tool="create_file", path="job-output.txt", content=b"from the job"),
    )
    assert not (primary / "job-output.txt").exists()


# --- 9-13. primary worktree proven byte-for-byte untouched ------------------

def test_primary_head_branch_index_and_tracked_contents_unchanged(job):
    primary, _handle, _conn, task_id, executor, _lease = job
    (primary / "pre-existing-untracked.txt").write_text("do not touch me\n")
    before_head = git(primary, "rev-parse", "HEAD")
    before_branch = git(primary, "rev-parse", "--abbrev-ref", "HEAD")
    before_index = git(primary, "ls-files", "--stage")
    before_snapshot = filesystem_snapshot(primary)

    executor.execute(
        task_id, ToolRequest(tool="create_file", path="job-output.txt", content=b"from the job"),
    )

    assert git(primary, "rev-parse", "HEAD") == before_head
    assert git(primary, "rev-parse", "--abbrev-ref", "HEAD") == before_branch
    assert git(primary, "ls-files", "--stage") == before_index
    assert filesystem_snapshot(primary) == before_snapshot
    # Explicitly re-assert the pre-existing untracked file specifically.
    assert (primary / "pre-existing-untracked.txt").read_text() == "do not touch me\n"


# --- 14/15. checkpoint from job worktree mutation, primary still untouched -

def test_checkpoint_created_successfully_from_job_worktree_mutation(job):
    _primary, handle, conn, task_id, executor, lease = job
    created = executor.execute(
        task_id, ToolRequest(tool="create_file", path="checked.txt", content=b"checkpoint me"),
    )
    assert created.status == OperationStatus.SUCCEEDED
    _to_ready_for_checkpoint(conn, task_id)

    manager = CheckpointManager(
        conn, blobs_dir=handle.blobs_dir, tmp_dir=handle.tmp_dir, lease=lease,
    )
    result = manager.create(task_id)
    assert result.decision == Decision.ALLOW
    assert result.operation_status == OperationStatus.SUCCEEDED
    assert result.commit_sha is not None

    # The dedicated ref exists, reachable from the job worktree's own
    # repository (shared object store with the primary).
    ref = f"refs/codeslayer/checkpoints/{task_id}/0"
    assert git(handle.path, "rev-parse", "--verify", ref) == result.commit_sha
    assert verify_chain(conn, task_id=task_id).ok


def test_checkpoint_creation_does_not_alter_primary_head_index_or_worktree(job):
    primary, handle, conn, task_id, executor, lease = job
    executor.execute(
        task_id, ToolRequest(tool="create_file", path="checked.txt", content=b"checkpoint me"),
    )
    _to_ready_for_checkpoint(conn, task_id)
    before_head = git(primary, "rev-parse", "HEAD")
    before_index = git(primary, "ls-files", "--stage")
    before_snapshot = working_tree_snapshot(primary)

    manager = CheckpointManager(
        conn, blobs_dir=handle.blobs_dir, tmp_dir=handle.tmp_dir, lease=lease,
    )
    result = manager.create(task_id)
    assert result.operation_status == OperationStatus.SUCCEEDED

    assert git(primary, "rev-parse", "HEAD") == before_head
    assert git(primary, "ls-files", "--stage") == before_index
    assert working_tree_snapshot(primary) == before_snapshot
    # No fast-forward/merge: the checkpoint ref is not primary's HEAD.
    assert git(primary, "rev-parse", "HEAD") != result.commit_sha


# --- 16/17/18. existing lease/policy/path-confinement hold unmodified -----

def test_stale_or_invalid_lease_still_blocks_mutation_in_job_worktree(job):
    from code_slayer.lease.manager import LeaseManager

    _primary, _handle, conn, task_id, executor, lease = job
    released = LeaseManager(conn).release(lease)
    assert released.decision == Decision.ALLOW

    result = executor.execute(
        task_id, ToolRequest(tool="create_file", path="should-not-exist.txt", content=b"x"),
    )
    assert result.decision == Decision.DENY
    assert result.reason == "stale_fencing_token"


def test_policy_denial_still_blocks_mutation_outside_declared_scope(primary):
    handle = create_job_worktree(primary)
    conn = connect(handle.db_path)
    try:
        (handle.path / "src").mkdir()
        task = TaskRepo(conn).create(
            description="scoped job", repo_root=str(handle.path), repo_id=handle.repo_id,
            worktree_id=handle.worktree_id, config={"tool_policy": {"scope": ["src"]}},
        )
        InspectionService(conn, blobs_dir=handle.blobs_dir).start(task.task_id)
        InspectionService(conn, blobs_dir=handle.blobs_dir).capture(task.task_id)
        _to_implementing(conn, task.task_id)
        lease = acquire_lease(conn, task)
        executor = ToolExecutor(conn, blobs_dir=handle.blobs_dir, lease=lease)

        result = executor.execute(
            task.task_id, ToolRequest(tool="create_file", path="outside.txt", content=b"x"),
        )
        assert result.decision == Decision.DENY
        assert result.reason == "outside_task_scope"
        assert not (handle.path / "outside.txt").exists()
    finally:
        conn.close()


def test_absolute_and_traversal_paths_cannot_escape_the_job_worktree(job):
    _primary, handle, _conn, task_id, executor, _lease = job

    absolute = executor.execute(
        task_id, ToolRequest(tool="create_file", path="/etc/codeslayer-escape-test", content=b"x"),
    )
    assert absolute.decision == Decision.DENY
    assert not (handle.path / "etc" / "codeslayer-escape-test").exists()

    traversal = executor.execute(
        task_id, ToolRequest(tool="create_file", path="../escape.txt", content=b"x"),
    )
    assert traversal.decision == Decision.DENY
    assert not (handle.path.parent / "escape.txt").exists()


# --- 20. cleanup: refusal-first ---------------------------------------------

def test_cleanup_succeeds_when_never_used(primary):
    handle = create_job_worktree(primary)
    result = release_job_worktree(handle)
    assert result.ok
    assert not handle.path.exists()
    assert handle.state_dir.exists()  # audit trail deliberately kept


def test_cleanup_refuses_active_lease(job):
    _primary, handle, _conn, _task_id, _executor, _lease = job
    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "active_lease"
    assert handle.path.exists()  # refused -- nothing removed


def test_cleanup_refuses_active_task(primary):
    handle = create_job_worktree(primary)
    conn = connect(handle.db_path)
    try:
        task = _make_task(conn, handle)
        _to_implementing(conn, task.task_id)
        lease = acquire_lease(conn, task)
        from code_slayer.lease.manager import LeaseManager

        LeaseManager(conn).release(lease)  # release the lease...
        # ...but the task itself is still non-terminal (IMPLEMENTING).
        result = release_job_worktree(handle)
        assert not result.ok
        assert result.reason == "active_task"
        assert handle.path.exists()
    finally:
        conn.close()


def test_cleanup_refuses_unresolved_operation(job):
    _primary, handle, conn, task_id, _executor, lease = job
    from code_slayer.store.db import transaction

    task = TaskRepo(conn).get(task_id)
    with transaction(conn):
        ToolOperationsRepo(conn).start_in_transaction(
            task_id=task_id, worktree_id=task.worktree_id, worker_id=lease.worker_id,
            worker_session_id=lease.worker_session_id, lease_generation=lease.generation,
            tool_name="write_file", risk_class="WRITE_OWNED", request_hash="deadbeef",
            target_resource="other.txt",
        )
    from code_slayer.lease.manager import LeaseManager

    LeaseManager(conn).release(lease)
    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "unresolved_operations"


def test_cleanup_refuses_uncheckpointed_mutation(job):
    from code_slayer.lease.manager import LeaseManager

    _primary, handle, conn, task_id, executor, lease = job
    created = executor.execute(
        task_id, ToolRequest(tool="create_file", path="never-checkpointed.txt", content=b"x"),
    )
    assert created.status == OperationStatus.SUCCEEDED
    _to_ready_for_checkpoint(conn, task_id)
    LeaseManager(conn).release(lease)

    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "active_task"  # READY_FOR_CHECKPOINT is non-terminal too

    # Even once the task is driven to a terminal state without ever
    # checkpointing, the owned-but-uncheckpointed content must still
    # block cleanup.
    TaskStateMachine(conn).transition(
        task_id, expected_state=TaskState.READY_FOR_CHECKPOINT, to_state=TaskState.FAILED,
        reason="abandoned without checkpointing", failure_decision=True,
    )
    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "uncheckpointed_changes"
    assert handle.path.exists()


def test_cleanup_succeeds_after_checkpoint_and_terminal_task(job):
    from code_slayer.lease.manager import LeaseManager

    _primary, handle, conn, task_id, executor, lease = job
    executor.execute(
        task_id, ToolRequest(tool="create_file", path="checked.txt", content=b"checkpoint me"),
    )
    _to_ready_for_checkpoint(conn, task_id)
    manager = CheckpointManager(
        conn, blobs_dir=handle.blobs_dir, tmp_dir=handle.tmp_dir, lease=lease,
    )
    checkpointed = manager.create(task_id)
    assert checkpointed.operation_status == OperationStatus.SUCCEEDED
    TaskStateMachine(conn).transition(
        task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.COMPLETED,
        reason="done", completion_decision=True,
    )
    LeaseManager(conn).release(lease)

    result = release_job_worktree(handle)
    assert result.ok
    assert not handle.path.exists()


def test_cleanup_refuses_content_owned_after_the_latest_checkpoint(job):
    """A checkpoint existing at all is not enough: content owned *after*
    the latest checkpoint (never covered by it) must still block cleanup,
    even though this same task does have at least one real checkpoint."""
    from code_slayer.lease.manager import LeaseManager

    _primary, handle, conn, task_id, executor, lease = job
    executor.execute(
        task_id, ToolRequest(tool="create_file", path="checked.txt", content=b"checkpoint me"),
    )
    _to_ready_for_checkpoint(conn, task_id)
    manager = CheckpointManager(
        conn, blobs_dir=handle.blobs_dir, tmp_dir=handle.tmp_dir, lease=lease,
    )
    checkpointed = manager.create(task_id)
    assert checkpointed.operation_status == OperationStatus.SUCCEEDED

    # Resume mutating after the checkpoint, without ever checkpointing again.
    TaskStateMachine(conn).transition(
        task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.IMPLEMENTING,
        reason="more work",
    )
    executor.execute(
        task_id, ToolRequest(tool="create_file", path="never-checkpointed.txt", content=b"y"),
    )
    LeaseManager(conn).release(lease)

    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "active_task"  # back in IMPLEMENTING, non-terminal

    # Even once driven to a terminal state, the post-checkpoint content
    # must still block cleanup.
    TaskStateMachine(conn).transition(
        task_id, expected_state=TaskState.IMPLEMENTING, to_state=TaskState.FAILED,
        reason="abandoned after further uncheckpointed work", failure_decision=True,
    )
    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "uncheckpointed_changes"
    assert handle.path.exists()


# --- Git-level dirtiness: an independent fail-safe over real on-disk state -

@pytest.fixture
def checkpointed(job):
    """A job worktree carrying one real checkpointed mutation, its task
    already driven to COMPLETED and its lease released -- the point at
    which the existing (bookkeeping-only) checks would already consider
    cleanup safe. Each dirty-worktree test starts here and then disturbs
    the actual working tree in some way the bookkeeping never sees."""
    from code_slayer.lease.manager import LeaseManager

    primary, handle, conn, task_id, executor, lease = job
    executor.execute(
        task_id, ToolRequest(tool="create_file", path="checked.txt", content=b"checkpoint me"),
    )
    _to_ready_for_checkpoint(conn, task_id)
    manager = CheckpointManager(
        conn, blobs_dir=handle.blobs_dir, tmp_dir=handle.tmp_dir, lease=lease,
    )
    checkpointed_result = manager.create(task_id)
    assert checkpointed_result.operation_status == OperationStatus.SUCCEEDED
    TaskStateMachine(conn).transition(
        task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.COMPLETED,
        reason="done", completion_decision=True,
    )
    LeaseManager(conn).release(lease)
    return primary, handle, conn


def test_clean_checkpointed_worktree_can_still_be_released(checkpointed):
    """The baseline this whole section disturbs from: with nothing
    touched after the checkpoint, cleanup must still succeed."""
    _primary, handle, _conn = checkpointed
    result = release_job_worktree(handle)
    assert result.ok
    assert not handle.path.exists()


def test_modified_tracked_file_after_checkpoint_refuses_cleanup(checkpointed):
    primary, handle, _conn = checkpointed
    del primary
    (handle.path / "checked.txt").write_bytes(b"modified after the checkpoint")

    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "git_worktree_dirty"


def test_deleted_tracked_file_after_checkpoint_refuses_cleanup(checkpointed):
    _primary, handle, _conn = checkpointed
    (handle.path / "checked.txt").unlink()

    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "git_worktree_dirty"


def test_staged_change_after_checkpoint_refuses_cleanup(checkpointed):
    _primary, handle, _conn = checkpointed
    (handle.path / "checked.txt").write_bytes(b"staged content")
    git(handle.path, "add", "checked.txt")

    result = release_job_worktree(handle)
    assert not result.ok
    # Phase 7.7b: a staged addition is caught by the real-index check
    # (`job_worktree_git.staged_changes()`) before the working-tree-vs-
    # checkpoint comparison even runs, so the more specific reason wins.
    assert result.reason == "git_worktree_staged_changes"


def test_untracked_file_after_checkpoint_refuses_cleanup(checkpointed):
    _primary, handle, _conn = checkpointed
    (handle.path / "untracked-after-checkpoint.txt").write_text("surprise\n")

    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "git_worktree_dirty"


def test_out_of_band_file_absent_from_task_owned_paths_still_refuses(checkpointed):
    """The exact finding this hardening addresses: a file that Code
    Slayer's own bookkeeping (`task_owned_paths`) never heard about at
    all -- written directly to the filesystem, not through
    `ToolExecutor` -- must still block cleanup."""
    _primary, handle, conn = checkpointed
    (handle.path / "human-was-here.txt").write_text("a human, not ToolExecutor\n")

    owned = {row["path"] for row in conn.execute(
        "SELECT path FROM task_owned_paths WHERE deleted = 0",
    )}
    assert "human-was-here.txt" not in owned  # confirms the bookkeeping truly never saw it

    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "git_worktree_dirty"


def test_dirty_worktree_refusal_leaves_worktree_and_state_intact(checkpointed):
    _primary, handle, conn = checkpointed
    (handle.path / "untracked-after-checkpoint.txt").write_text("surprise\n")

    result = release_job_worktree(handle)
    assert not result.ok

    assert handle.path.exists()
    assert (handle.path / "checked.txt").read_bytes() == b"checkpoint me"
    assert (handle.path / "untracked-after-checkpoint.txt").exists()
    assert handle.state_dir.exists()
    assert handle.db_path.exists()
    # The durable checkpoint evidence itself is untouched.
    rows = conn.execute("SELECT status FROM checkpoints").fetchall()
    assert [row["status"] for row in rows] == ["COMPLETE"]


def test_dirty_worktree_refusal_leaves_primary_untouched(checkpointed):
    primary, handle, _conn = checkpointed
    before_head = git(primary, "rev-parse", "HEAD")
    before_index = git(primary, "ls-files", "--stage")
    before_snapshot = working_tree_snapshot(primary)

    (handle.path / "untracked-after-checkpoint.txt").write_text("surprise\n")
    result = release_job_worktree(handle)
    assert not result.ok

    assert git(primary, "rev-parse", "HEAD") == before_head
    assert git(primary, "ls-files", "--stage") == before_index
    assert working_tree_snapshot(primary) == before_snapshot


def test_cleanup_refuses_when_state_setup_incomplete(primary, tmp_path):
    """A handle whose database was never created (setup never completed)
    must refuse cleanup rather than guess."""
    handle = create_job_worktree(primary)
    handle.db_path.unlink()
    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "state_incomplete_cannot_verify_safety"
    assert handle.path.exists()


# --- interruption / recovery: partial setup never silently discarded ------

def test_setup_incomplete_leaves_worktree_recoverable_not_deleted(primary, monkeypatch):
    import code_slayer.repo.job_worktree as job_worktree_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated setup failure after worktree creation")

    monkeypatch.setattr(job_worktree_module.db_module, "migrate", _boom)
    with pytest.raises(JobWorktreeSetupIncompleteError) as excinfo:
        create_job_worktree(primary)

    # Git's own registry is untouched and still proves the worktree
    # exists -- ground truth independent of Code Slayer's own bookkeeping.
    from code_slayer.repo.job_worktree_git import list_worktrees

    assert str(excinfo.value.path) in list_worktrees(cwd=primary)
    assert excinfo.value.path.is_dir()


def test_git_worktree_add_failure_creates_nothing(tmp_path):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    with pytest.raises(identity.NotAGitRepositoryError):
        create_job_worktree(not_a_repo)


def test_unresolvable_base_revision_raises_before_any_worktree_created(primary):
    with pytest.raises(JobWorktreeGitError):
        create_job_worktree(primary, base_revision="not-a-real-revision")
    # Nothing under Code Slayer's job-worktree area was created.
    from code_slayer.store import location

    job_worktrees_root = location.state_root() / "job-worktrees"
    assert not job_worktrees_root.exists() or not any(job_worktrees_root.rglob("*"))


# --- full audit chain sanity -------------------------------------------------

def test_audit_chain_valid_across_job_worktree_lifecycle(job):
    _primary, handle, conn, task_id, executor, lease = job
    executor.execute(
        task_id, ToolRequest(tool="create_file", path="checked.txt", content=b"checkpoint me"),
    )
    _to_ready_for_checkpoint(conn, task_id)
    CheckpointManager(conn, blobs_dir=handle.blobs_dir, tmp_dir=handle.tmp_dir, lease=lease).create(
        task_id,
    )
    assert verify_chain(conn, task_id=task_id).ok
    assert verify_chain(conn, task_id=None).ok


# --- Phase 7.7b: job-worktree cleanup integrity hardening ------------------
#
# Real-index staged-change detection, ignored-file detection, and the
# cleanup/delete race close the three findings an independent Phase 7
# audit reported against the Phase 7.5c/hardening cleanup logic above.
# See `repo.job_worktree.release_job_worktree()`'s own docstring for the
# two-phase (claim, then verify-and-remove) protocol these tests exercise.

def test_staged_modification_then_restored_bytes_refuses_cleanup(checkpointed):
    """The exact Phase 7.7b finding #1: stage a real edit, then restore
    the working-tree bytes to exactly what the checkpoint tree has. A
    working-tree-vs-tree-only comparison (run against a private,
    temporary index, as the pre-7.7b check did) sees nothing wrong; the
    job worktree's own REAL index still carries the staged edit, and a
    later `git commit` would use it -- cleanup must not discard that.

    Uses `README.md`, not `checked.txt`: a checkpoint commit is built
    directly from blobs/trees (`repo.checkpoint_git`), never by staging
    into or committing onto the job worktree's own real index/HEAD (see
    this module's docstring, "No promotion into the primary worktree"),
    so `checked.txt` -- created only through `ToolExecutor`, never `git
    add`ed for real -- has no prior real-HEAD content to "stage a
    modification, then restore" against. `README.md` is genuinely
    tracked in the job worktree's real (frozen, pinned) `HEAD`, inherited
    from the primary repository's base revision.
    """
    _primary, handle, _conn = checkpointed
    original = (handle.path / "README.md").read_bytes()
    (handle.path / "README.md").write_bytes(b"a real edit, staged")
    git(handle.path, "add", "README.md")
    (handle.path / "README.md").write_bytes(original)  # restore working-tree bytes

    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "git_worktree_staged_changes"
    assert handle.path.exists()
    assert (handle.path / "README.md").read_bytes() == original


def test_staged_new_file_refuses_cleanup(checkpointed):
    """Regression test #2: a brand new file, staged, refuses cleanup --
    already exercised as a side effect of
    `test_staged_change_after_checkpoint_refuses_cleanup` above, restated
    here as its own named case per the required regression list."""
    _primary, handle, _conn = checkpointed
    (handle.path / "brand-new.txt").write_bytes(b"never seen before")
    git(handle.path, "add", "brand-new.txt")

    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "git_worktree_staged_changes"
    assert (handle.path / "brand-new.txt").exists()


def test_staged_deletion_refuses_cleanup(checkpointed):
    """Regression test #3: `git rm --cached` stages a deletion without
    touching the working-tree file at all -- the real index disagrees
    with HEAD even though the working tree does not. Uses `README.md`
    (genuinely tracked in the job worktree's real HEAD) for the same
    reason `test_staged_modification_then_restored_bytes_refuses_cleanup`
    does -- `checked.txt` was never really staged/committed in the first
    place, so it has nothing real to `git rm --cached`."""
    _primary, handle, _conn = checkpointed
    git(handle.path, "rm", "--cached", "README.md")

    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "git_worktree_staged_changes"
    assert (handle.path / "README.md").exists()  # --cached never touches the file


def test_ignored_untracked_file_after_checkpoint_refuses_cleanup(job):
    """Regression test #6, the exact Phase 7.7b finding #2: a file
    matched by `.gitignore` is real filesystem content a `git worktree
    remove` would destroy just the same as an untracked one. Checkpoints
    `.gitignore` itself first so only the ignored file's own status is
    under test."""
    from code_slayer.lease.manager import LeaseManager

    _primary, handle, conn, task_id, executor, lease = job
    executor.execute(
        task_id, ToolRequest(tool="create_file", path=".gitignore", content=b"ignored.log\n"),
    )
    _to_ready_for_checkpoint(conn, task_id)
    manager = CheckpointManager(
        conn, blobs_dir=handle.blobs_dir, tmp_dir=handle.tmp_dir, lease=lease,
    )
    checkpointed_result = manager.create(task_id)
    assert checkpointed_result.operation_status == OperationStatus.SUCCEEDED
    TaskStateMachine(conn).transition(
        task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.COMPLETED,
        reason="done", completion_decision=True,
    )
    LeaseManager(conn).release(lease)

    (handle.path / "ignored.log").write_text("should not be silently discarded\n")
    # Confirm this file really is ignored, not merely untracked -- a
    # plain `git status` (no `--ignored`, the pre-Phase-7.7b query shape)
    # genuinely cannot see it, which is exactly the finding.
    plain_status = git(handle.path, "status", "--porcelain=v2", "--untracked-files=all")
    assert "ignored.log" not in plain_status

    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "git_worktree_dirty"
    assert (handle.path / "ignored.log").exists()


def test_ignored_nested_file_after_checkpoint_refuses_cleanup(job):
    """Regression test #7: an ignored file nested several directories
    deep is reported individually (plain `--ignored`, never
    `--ignored=matching`, which would collapse it to just the top-level
    directory and make it non-enumerable)."""
    from code_slayer.lease.manager import LeaseManager

    _primary, handle, conn, task_id, executor, lease = job
    executor.execute(
        task_id, ToolRequest(tool="create_file", path=".gitignore", content=b"ignored.log\n"),
    )
    _to_ready_for_checkpoint(conn, task_id)
    manager = CheckpointManager(
        conn, blobs_dir=handle.blobs_dir, tmp_dir=handle.tmp_dir, lease=lease,
    )
    checkpointed_result = manager.create(task_id)
    assert checkpointed_result.operation_status == OperationStatus.SUCCEEDED
    TaskStateMachine(conn).transition(
        task_id, expected_state=TaskState.CHECKPOINTED, to_state=TaskState.COMPLETED,
        reason="done", completion_decision=True,
    )
    LeaseManager(conn).release(lease)

    nested = handle.path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "ignored.log").write_text("nested, still real content\n")

    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "git_worktree_dirty"
    assert (nested / "ignored.log").exists()


def _hook_before_removal_checks(monkeypatch, handle, callback):
    """Patch the first Git-plumbing call `release_job_worktree()` makes
    once its cleanup claim is durably committed (`jwg.staged_changes()`)
    so `callback()` runs exactly inside that claimed-but-not-yet-removed
    window, then delegates to the real implementation. This is how the
    race tests below observe -- and prove closed -- the exact window the
    Phase 7.7b finding #3 (check/delete race) is about, without needing
    real concurrent threads/processes for what is fundamentally a
    same-database serialization guarantee."""
    import code_slayer.repo.job_worktree as job_worktree_module

    real_staged_changes = job_worktree_module.jwg.staged_changes

    def _wrapped(path):
        callback()
        return real_staged_changes(path)

    monkeypatch.setattr(job_worktree_module.jwg, "staged_changes", _wrapped)


def test_task_creation_blocked_while_cleanup_claim_is_held(checkpointed, monkeypatch):
    """Regression test #10: a task creation attempt landing in the
    window between cleanup's safety decision and the actual `git
    worktree remove` must be refused, not silently allowed to proceed
    against a worktree that is about to disappear."""
    from code_slayer.store.task_repo import WorktreeCleanupInProgressError

    primary, handle, conn = checkpointed
    before_head = git(primary, "rev-parse", "HEAD")
    attempts = []

    def _race():
        other_conn = connect(handle.db_path)
        try:
            try:
                TaskRepo(other_conn).create(
                    description="racing task", repo_root=str(handle.path),
                    repo_id=handle.repo_id, worktree_id=handle.worktree_id,
                )
                attempts.append("unexpectedly_succeeded")
            except WorktreeCleanupInProgressError:
                attempts.append("blocked")
        finally:
            other_conn.close()

    _hook_before_removal_checks(monkeypatch, handle, _race)

    result = release_job_worktree(handle)
    assert attempts == ["blocked"]
    assert result.ok
    assert result.reason == "removed"
    assert not handle.path.exists()
    assert TaskRepo(conn).get_active_for_worktree(handle.worktree_id) is None
    assert git(primary, "rev-parse", "HEAD") == before_head  # primary untouched


def test_lease_acquisition_blocked_while_cleanup_claim_is_held(checkpointed, monkeypatch):
    """Regression test #11: a fresh lease acquisition landing in that
    same window must be denied for the same reason -- the other half of
    the ownership-establishment surface alongside task creation."""
    from code_slayer.lease.manager import LeaseManager

    primary, handle, conn = checkpointed
    before_head = git(primary, "rev-parse", "HEAD")
    attempts = []

    def _race():
        other_conn = connect(handle.db_path)
        try:
            outcome = LeaseManager(other_conn).acquire(
                worktree_id=handle.worktree_id, task_id="racing-task-id",
                worker_id="racer", worker_session_id="racer-session",
            )
            attempts.append(outcome)
        finally:
            other_conn.close()

    _hook_before_removal_checks(monkeypatch, handle, _race)

    result = release_job_worktree(handle)
    assert len(attempts) == 1
    assert attempts[0].decision == Decision.DENY
    assert attempts[0].reason == "job_worktree_cleanup_in_progress"
    assert attempts[0].handle is None
    assert result.ok
    assert result.reason == "removed"
    assert not handle.path.exists()
    assert _active_lease_or_none(conn, handle.worktree_id) is None
    assert git(primary, "rev-parse", "HEAD") == before_head  # primary untouched


def _active_lease_or_none(conn, worktree_id):
    from code_slayer.store.lease_repo import LeaseRepo, LeaseStatus

    lease = LeaseRepo(conn).get(worktree_id)
    if lease is not None and lease.status in (LeaseStatus.ACTIVE, LeaseStatus.QUIESCING):
        return lease
    return None


def test_no_new_operation_can_start_while_cleanup_claim_is_held(checkpointed, monkeypatch):
    """Regression test #12: since a genuinely new operation can only ever
    start under a freshly-granted, currently-current lease (`lease.
    manager.LeaseManager.is_current()`'s fencing gate, unmodified by this
    phase), and fresh-epoch grants are refused throughout the claimed
    window (previous test), no unresolved operation can appear during
    cleanup either. This test confirms the observable consequence: zero
    `tool_operations` rows are added for this worktree while the claim is
    held."""
    from code_slayer.lease.manager import LeaseManager

    _primary, handle, conn = checkpointed
    before_count = conn.execute(
        "SELECT COUNT(*) AS n FROM tool_operations WHERE worktree_id = ?",
        (handle.worktree_id,),
    ).fetchone()["n"]
    attempts = []

    def _race():
        other_conn = connect(handle.db_path)
        try:
            outcome = LeaseManager(other_conn).acquire(
                worktree_id=handle.worktree_id, task_id="racing-task-id",
                worker_id="racer", worker_session_id="racer-session",
            )
            attempts.append(outcome.decision)
            # No valid handle was ever granted, so there is no lease to
            # execute a tool call under -- ToolExecutor itself can never
            # be reached with authority over this worktree in this window.
        finally:
            other_conn.close()

    _hook_before_removal_checks(monkeypatch, handle, _race)

    result = release_job_worktree(handle)
    assert attempts == [Decision.DENY]
    assert result.ok

    after_count = conn.execute(
        "SELECT COUNT(*) AS n FROM tool_operations WHERE worktree_id = ?",
        (handle.worktree_id,),
    ).fetchone()["n"]
    assert after_count == before_count


def test_failed_git_removal_preserves_state_and_permits_safe_retry(checkpointed, monkeypatch):
    """Regression tests #13 and #14: if `git worktree remove` itself
    fails after the claim is recorded, durable lifecycle/evidence state
    must not be lost, the worktree must not be marked released, and a
    later call must be able to pick the removal back up safely."""
    import code_slayer.repo.job_worktree as job_worktree_module
    from code_slayer.repo.job_worktree_git import JobWorktreeGitError

    _primary, handle, conn = checkpointed

    def _boom(path, *, cwd, force=True):
        raise JobWorktreeGitError("simulated git worktree remove failure")

    monkeypatch.setattr(job_worktree_module.jwg, "remove_worktree", _boom)

    result = release_job_worktree(handle)
    assert not result.ok
    assert result.reason == "git_worktree_remove_failed"
    assert handle.path.exists()  # nothing lost
    assert (handle.path / "checked.txt").read_bytes() == b"checkpoint me"

    claim = conn.execute(
        "SELECT * FROM job_worktree_cleanup_claims WHERE worktree_id = ?",
        (handle.worktree_id,),
    ).fetchone()
    assert claim is not None
    assert claim["status"] == "REMOVING"  # never silently marked released

    failed_events = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events "
        "WHERE event_type = 'JOB_WORKTREE_CLEANUP_REMOVAL_FAILED'"
    ).fetchone()["n"]
    assert failed_events == 1
    released_events = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE event_type = 'JOB_WORKTREE_RELEASED'"
    ).fetchone()["n"]
    assert released_events == 0

    # Durable checkpoint evidence is untouched by the failed attempt.
    rows = conn.execute("SELECT status FROM checkpoints").fetchall()
    assert [row["status"] for row in rows] == ["COMPLETE"]

    # A new task still cannot be created while this failed claim stands.
    from code_slayer.store.task_repo import WorktreeCleanupInProgressError

    with pytest.raises(WorktreeCleanupInProgressError):
        TaskRepo(conn).create(
            description="should stay blocked", repo_root=str(handle.path),
            repo_id=handle.repo_id, worktree_id=handle.worktree_id,
        )

    monkeypatch.undo()
    retry = release_job_worktree(handle)
    assert retry.ok
    assert retry.reason == "removed"
    assert not handle.path.exists()

    claim_after = conn.execute(
        "SELECT * FROM job_worktree_cleanup_claims WHERE worktree_id = ?",
        (handle.worktree_id,),
    ).fetchone()
    assert claim_after is None
    released_events_after = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE event_type = 'JOB_WORKTREE_RELEASED'"
    ).fetchone()["n"]
    assert released_events_after == 1


def test_retry_recovers_when_git_removal_actually_succeeded_before_a_crash(
    checkpointed, monkeypatch,
):
    """A stricter version of #14: the crash happens strictly *after* Git's
    own removal genuinely succeeded (simulating a process crash between
    `jwg.remove_worktree()` returning and this module recording that
    fact) -- a naive retry must not treat "already gone" as a new
    failure, and must not attempt to remove an already-removed path."""
    import code_slayer.repo.job_worktree as job_worktree_module

    _primary, handle, conn = checkpointed
    real_remove = job_worktree_module.jwg.remove_worktree

    def _remove_then_crash(path, *, cwd, force=True):
        real_remove(path, cwd=cwd, force=force)
        raise RuntimeError("simulated process crash after git worktree remove succeeded")

    monkeypatch.setattr(job_worktree_module.jwg, "remove_worktree", _remove_then_crash)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        release_job_worktree(handle)
    monkeypatch.undo()

    assert not handle.path.exists()  # git's own removal really did complete
    claim = conn.execute(
        "SELECT * FROM job_worktree_cleanup_claims WHERE worktree_id = ?",
        (handle.worktree_id,),
    ).fetchone()
    assert claim is not None
    assert claim["status"] == "REMOVING"

    retry = release_job_worktree(handle)
    assert retry.ok
    assert retry.reason == "removed"

    claim_after = conn.execute(
        "SELECT * FROM job_worktree_cleanup_claims WHERE worktree_id = ?",
        (handle.worktree_id,),
    ).fetchone()
    assert claim_after is None
    released_events = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_events WHERE event_type = 'JOB_WORKTREE_RELEASED'"
    ).fetchone()["n"]
    assert released_events == 1


def test_race_test_primary_repository_remains_untouched(checkpointed, monkeypatch):
    """Regression test #15, exercised specifically against the race/claim
    machinery above (the ordinary cleanup paths already have their own
    primary-isolation tests): none of cleanup's new claim/abort/retry
    bookkeeping ever touches the primary repository."""
    from code_slayer.store.task_repo import WorktreeCleanupInProgressError

    primary, handle, _conn = checkpointed
    before_head = git(primary, "rev-parse", "HEAD")
    before_index = git(primary, "ls-files", "--stage")
    before_snapshot = working_tree_snapshot(primary)

    def _race():
        other_conn = connect(handle.db_path)
        try:
            with pytest.raises(WorktreeCleanupInProgressError):
                TaskRepo(other_conn).create(
                    description="racing task", repo_root=str(handle.path),
                    repo_id=handle.repo_id, worktree_id=handle.worktree_id,
                )
        finally:
            other_conn.close()

    _hook_before_removal_checks(monkeypatch, handle, _race)
    result = release_job_worktree(handle)
    assert result.ok

    assert git(primary, "rev-parse", "HEAD") == before_head
    assert git(primary, "ls-files", "--stage") == before_index
    assert working_tree_snapshot(primary) == before_snapshot
