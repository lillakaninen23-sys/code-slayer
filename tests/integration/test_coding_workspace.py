"""`coding.workspace.prepare_coder_workspace()`: `repo.job_worktree.
create_job_worktree()` plus an independent, freshly-run re-verification
of the exact base commit and cleanliness before any Coder execution is
authorized against it.
"""

from __future__ import annotations

from code_slayer.coding.workspace import prepare_coder_workspace
from code_slayer.repo import identity
from tests.repo_helpers import commit, git


def test_prepare_coder_workspace_pins_to_current_head_by_default(git_repo_with_commit):
    primary_head = identity.resolve(git_repo_with_commit)
    expected = git(git_repo_with_commit, "rev-parse", "HEAD")
    handle = prepare_coder_workspace(git_repo_with_commit)
    assert handle.base_revision == expected
    assert handle.repo_id == primary_head.repo_id
    assert git(handle.path, "rev-parse", "HEAD") == expected


def test_prepare_coder_workspace_pins_to_explicit_base_revision(git_repo_with_commit):
    first_head = git(git_repo_with_commit, "rev-parse", "HEAD")
    (git_repo_with_commit / "second.txt").write_text("second\n")
    commit(git_repo_with_commit, "second commit")
    second_head = git(git_repo_with_commit, "rev-parse", "HEAD")
    assert first_head != second_head

    handle = prepare_coder_workspace(git_repo_with_commit, base_revision=first_head)
    assert handle.base_revision == first_head
    assert git(handle.path, "rev-parse", "HEAD") == first_head
    assert not (handle.path / "second.txt").exists()


def test_prepare_coder_workspace_worktree_is_clean_immediately_after_creation(git_repo_with_commit):
    handle = prepare_coder_workspace(git_repo_with_commit)
    assert git(handle.path, "status", "--porcelain") == ""


def test_prepare_coder_workspace_does_not_touch_primary_working_tree(git_repo_with_commit):
    before = git(git_repo_with_commit, "status", "--porcelain")
    prepare_coder_workspace(git_repo_with_commit)
    assert git(git_repo_with_commit, "status", "--porcelain") == before
