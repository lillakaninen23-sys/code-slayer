"""`coding.mutation_guard`: independent, real-git-verified inspection of
what a Coder/Repairer turn actually changed on disk -- never a model's
own claim -- plus the independently-computed diff Reviewer/Security are
shown.
"""

from __future__ import annotations

from code_slayer.coding.mutation_guard import compute_diff_text, verify_authorized_mutations
from code_slayer.core import TaskState, TaskStateMachine
from code_slayer.repo.baseline import InspectionService
from code_slayer.repo.job_worktree import create_job_worktree
from code_slayer.store.db import connect, migrate
from code_slayer.store.task_repo import TaskRepo
from code_slayer.tools.executor import ToolExecutor
from code_slayer.tools.models import ToolRequest
from tests.repo_helpers import acquire_lease


def _build_task(handle):
    conn = connect(handle.db_path)
    migrate(conn)
    task = TaskRepo(conn).create(
        description="mutation guard test", repo_root=str(handle.path), repo_id=handle.repo_id,
        worktree_id=handle.worktree_id,
        config={"tool_policy": {"scope": ["."]}, "execution_kind": "coding_job"},
    )
    InspectionService(conn, blobs_dir=handle.blobs_dir).start(task.task_id)
    InspectionService(conn, blobs_dir=handle.blobs_dir).capture(task.task_id)
    machine = TaskStateMachine(conn)
    for to_state, expected in (
        (TaskState.PLANNING, TaskState.BASELINED),
        (TaskState.PLANNED, TaskState.PLANNING),
        (TaskState.IMPLEMENTING, TaskState.PLANNED),
    ):
        machine.transition(task.task_id, expected_state=expected, to_state=to_state, reason="setup")
    lease = acquire_lease(conn, task)
    return conn, task, lease


def test_verify_authorized_mutations_reports_clean_when_nothing_changed(git_repo_with_commit):
    handle = create_job_worktree(git_repo_with_commit)
    conn, task, _lease = _build_task(handle)
    result = verify_authorized_mutations(
        conn, handle.path, task_id=task.task_id, base_revision=handle.base_revision,
        tmp_dir=handle.tmp_dir,
    )
    assert result.ok
    assert result.changed_paths == ()
    assert result.unauthorized_paths == ()


def test_verify_authorized_mutations_accepts_a_real_owned_mutation(git_repo_with_commit):
    handle = create_job_worktree(git_repo_with_commit)
    conn, task, lease = _build_task(handle)
    executor = ToolExecutor(conn, blobs_dir=handle.blobs_dir, lease=lease)
    result = executor.execute(
        task.task_id, ToolRequest(tool="create_file", path="new.txt", content=b"hi\n"),
    )
    assert result.status == "SUCCEEDED", result.reason

    audit = verify_authorized_mutations(
        conn, handle.path, task_id=task.task_id, base_revision=handle.base_revision,
        tmp_dir=handle.tmp_dir,
    )
    assert audit.ok
    assert audit.changed_paths == ("new.txt",)
    assert audit.owned_paths == ("new.txt",)
    assert audit.unauthorized_paths == ()


def test_verify_authorized_mutations_flags_an_untracked_file_never_owned_by_toolexecutor(
    git_repo_with_commit,
):
    """A file that appears in the working tree WITHOUT ever going through
    a real, journaled `ToolExecutor` operation must be reported as
    unauthorized -- proves this check is independent of, not merely a
    restatement of, `task_owned_paths` bookkeeping."""
    handle = create_job_worktree(git_repo_with_commit)
    conn, task, _lease = _build_task(handle)
    (handle.path / "sneaky.txt").write_text("not through ToolExecutor\n")

    audit = verify_authorized_mutations(
        conn, handle.path, task_id=task.task_id, base_revision=handle.base_revision,
        tmp_dir=handle.tmp_dir,
    )
    assert not audit.ok
    assert audit.unauthorized_paths == ("sneaky.txt",)


def test_compute_diff_text_reflects_a_real_mutation(git_repo_with_commit):
    handle = create_job_worktree(git_repo_with_commit)
    conn, task, lease = _build_task(handle)
    executor = ToolExecutor(conn, blobs_dir=handle.blobs_dir, lease=lease)
    executor.execute(
        task.task_id, ToolRequest(tool="create_file", path="new.txt", content=b"hello diff\n"),
    )
    diff_text = compute_diff_text(handle.path, base_revision=handle.base_revision)
    assert "new.txt" in diff_text
    assert "hello diff" in diff_text


def test_compute_diff_text_is_empty_when_nothing_changed(git_repo_with_commit):
    handle = create_job_worktree(git_repo_with_commit)
    diff_text = compute_diff_text(handle.path, base_revision=handle.base_revision)
    assert diff_text == ""
