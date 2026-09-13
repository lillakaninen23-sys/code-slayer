"""Repository/worktree identity foundation."""

from __future__ import annotations

import subprocess

import pytest

from code_slayer.repo import identity


def _run_git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


def test_not_a_git_repository_raises(tmp_path):
    not_a_repo = tmp_path / "plain_dir"
    not_a_repo.mkdir()
    with pytest.raises(identity.NotAGitRepositoryError):
        identity.resolve(not_a_repo)


def test_same_worktree_same_identity(git_repo):
    first = identity.resolve(git_repo)
    second = identity.resolve(git_repo)
    assert first.repo_id == second.repo_id
    assert first.worktree_id == second.worktree_id


def test_same_repo_reopened_same_repo_id(git_repo):
    first = identity.resolve(git_repo)
    # Simulate "reopening" by resolving again from a fresh call with no
    # in-process state carried over.
    second = identity.resolve(git_repo, create=False)
    assert first.repo_id == second.repo_id
    assert first.worktree_id == second.worktree_id


def test_resolve_create_false_before_init_raises(git_repo):
    with pytest.raises(identity.UnknownIdentityError):
        identity.resolve(git_repo, create=False)


def test_separate_worktree_same_repo_id_different_worktree_id(git_repo_with_commit, tmp_path):
    main_identity = identity.resolve(git_repo_with_commit)

    linked = tmp_path / "linked-worktree"
    _run_git(["worktree", "add", str(linked), "-b", "feature"], cwd=git_repo_with_commit)

    linked_identity = identity.resolve(linked)

    assert linked_identity.repo_id == main_identity.repo_id
    assert linked_identity.worktree_id != main_identity.worktree_id


def test_git_clean_fdx_does_not_destroy_identity(git_repo_with_commit):
    before = identity.resolve(git_repo_with_commit)

    (git_repo_with_commit / "untracked.txt").write_text("junk\n")
    _run_git(["clean", "-fdx"], cwd=git_repo_with_commit)
    _run_git(["reset", "--hard"], cwd=git_repo_with_commit)

    after = identity.resolve(git_repo_with_commit, create=False)
    assert after.repo_id == before.repo_id
    assert after.worktree_id == before.worktree_id


def test_clone_does_not_inherit_source_identity(git_repo_with_commit, tmp_path):
    source = identity.resolve(git_repo_with_commit)

    clone_path = tmp_path / "clone"
    _run_git(["clone", "-q", str(git_repo_with_commit), str(clone_path)], cwd=tmp_path)

    # The clone must not silently inherit the source's identity: it has no
    # repo_id of its own yet (git clone does not copy custom config keys).
    with pytest.raises(identity.UnknownIdentityError):
        identity.resolve(clone_path, create=False)

    # And once it establishes its own, it must be a *different* identity,
    # not the source machine's.
    clone_identity = identity.resolve(clone_path, create=True)
    assert clone_identity.repo_id != source.repo_id
    assert clone_identity.worktree_id != source.worktree_id


def test_git_dir_and_common_dir_agree_for_main_worktree(git_repo):
    info = identity.resolve(git_repo)
    assert info.git_dir == info.git_common_dir
    assert info.git_dir.name == ".git"
