"""Real temporary Git repositories, filenames, status classes and preservation."""

import os

import pytest

from code_slayer.repo import identity
from code_slayer.repo.git import GitError, read_bytes
from code_slayer.repo.inspection import InspectionError, inspect_repository
from tests.repo_helpers import commit, filesystem_snapshot, git


def test_clean_repository_identity_and_metadata(git_repo_with_commit):
    root = git_repo_with_commit
    nested = root / "nested" / "directory"
    nested.mkdir(parents=True)
    info = identity.resolve(root)
    before = filesystem_snapshot(root)
    result = inspect_repository(nested)
    assert result.repo_root == str(root.resolve())
    assert result.worktree_dir == result.repo_root
    assert result.repo_id == info.repo_id
    assert result.worktree_id == info.worktree_id
    assert result.git_dir == result.git_common_dir == str(root / ".git")
    assert result.head == git(root, "rev-parse", "HEAD")
    assert result.branch == git(root, "branch", "--show-current")
    assert not result.detached and not result.unborn and not result.shallow
    assert result.object_format == "sha1"
    assert result.is_clean
    assert result.protected_paths() == {}
    assert result.observation_hash == inspect_repository(root).observation_hash
    assert filesystem_snapshot(root) == before


def test_unborn_branch_is_not_detached(git_repo):
    result = inspect_repository(git_repo)
    assert result.head is None and result.unborn
    assert result.branch == git(git_repo, "symbolic-ref", "--short", "HEAD")
    assert not result.detached and result.is_clean


def test_first_inspection_only_establishes_approved_identity_metadata(git_repo_with_commit):
    root = git_repo_with_commit
    before = filesystem_snapshot(root)
    inspect_repository(root)
    after = filesystem_snapshot(root)
    changed = {path for path in before.keys() | after.keys() if before.get(path) != after.get(path)}
    assert changed == {".git/config", ".git/codeslayer-id"}


@pytest.mark.parametrize(("staged", "unstaged"), [(False, True), (True, False), (True, True)])
def test_staged_and_unstaged_are_independent(git_repo_with_commit, staged, unstaged):
    root = git_repo_with_commit
    file = root / "README.md"
    file.write_text("staged or dirty\n")
    if staged:
        git(root, "add", "README.md")
    if unstaged:
        file.write_text("further dirty modification\n")
    result = inspect_repository(root)
    assert not result.is_clean
    assert result.tracked_modifications == ("README.md",)
    assert ("README.md" in result.staged_modifications) == staged
    assert ("README.md" in result.unstaged_modifications) == unstaged
    assert result.protected_paths() == {"README.md": "pre_existing_dirty"}


@pytest.mark.parametrize("staged", [False, True])
def test_deleted_file(git_repo_with_commit, staged):
    root = git_repo_with_commit
    (root / "README.md").unlink()
    if staged:
        git(root, "add", "-u")
    result = inspect_repository(root)
    assert result.deleted_paths == ("README.md",)
    assert result.protected_paths()["README.md"] == "pre_existing_dirty"
    assert bool(result.staged_modifications) == staged


def test_rename_preserves_and_protects_both_paths(git_repo_with_commit):
    root = git_repo_with_commit
    destination = "new\tname\nwith space.md"
    git(root, "mv", "README.md", destination)
    result = inspect_repository(root)
    rename = result.changes[0]
    assert rename.path == destination
    assert rename.original_path == "README.md"
    assert rename.index_status == "R" and rename.similarity == "R100"
    assert result.protected_paths() == {
        "README.md": "pre_existing_dirty", destination: "pre_existing_dirty",
    }


@pytest.mark.parametrize("name", ["space name", "tab\tname", "new\nline", "cr\r\nlf", "räv.txt"])
def test_untracked_filename_is_lossless(git_repo_with_commit, name):
    (git_repo_with_commit / name).write_text("private bytes")
    result = inspect_repository(git_repo_with_commit)
    assert result.untracked_paths == (name,)
    assert result.protected_paths() == {name: "pre_existing_untracked"}
    assert "private bytes" not in str(result.metadata())


def test_non_utf8_filename_fails_closed(git_repo_with_commit):
    path = os.fsencode(git_repo_with_commit) + b"/bad-\xff"
    with open(path, "wb") as stream:
        stream.write(b"private")
    with pytest.raises(InspectionError, match="UTF-8"):
        inspect_repository(git_repo_with_commit)


def test_ignored_files_are_protected_without_reading_them(git_repo_with_commit):
    root = git_repo_with_commit
    (root / ".gitignore").write_text(".env\nignored/\n")
    commit(root)
    (root / ".env").write_text("PRIVATE_MARKER")
    (root / "ignored").mkdir()
    (root / "ignored" / "key").write_text("PRIVATE_MARKER")
    result = inspect_repository(root)
    assert result.is_clean
    assert result.ignored_paths == (".env", "ignored/key")
    assert set(result.protected_paths()) == {".env", "ignored/key"}
    assert "PRIVATE_MARKER" not in str(result.metadata())


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_index_hidden_paths_are_conservatively_protected(git_repo_with_commit, flag):
    root = git_repo_with_commit
    git(root, "update-index", flag, "README.md")
    (root / "README.md").write_text("hidden dirty bytes")
    result = inspect_repository(root)
    assert result.masked_paths == ("README.md",)
    assert not result.is_clean
    assert result.protected_paths() == {"README.md": "pre_existing_dirty"}


def test_detached_head(git_repo_with_commit):
    git(git_repo_with_commit, "checkout", "--detach", "-q")
    result = inspect_repository(git_repo_with_commit)
    assert result.detached and not result.unborn
    assert result.branch is None and result.head


def test_nested_linked_worktree_identity(git_repo_with_commit, tmp_path):
    main = inspect_repository(git_repo_with_commit)
    linked = tmp_path / "nested" / "linked"
    git(git_repo_with_commit, "worktree", "add", "-b", "linked", str(linked))
    inner = linked / "src" / "deep"
    inner.mkdir(parents=True)
    other = inspect_repository(inner)
    assert other.repo_id == main.repo_id
    assert other.worktree_id != main.worktree_id
    assert other.repo_root == str(linked)
    assert other.git_common_dir == main.git_common_dir
    assert other.git_dir != main.git_dir
    assert other.branch == "linked"


@pytest.mark.parametrize("change", ["unchanged", "added", "modified", "deleted"])
def test_gitlink_metadata_without_submodule_traversal(git_repo_with_commit, change):
    root = git_repo_with_commit
    old = git(root, "rev-parse", "HEAD")
    if change != "added":
        git(root, "update-index", "--add", "--cacheinfo", f"160000,{old},sub")
        git(root, "commit", "-qm", "gitlink fixture")
    if change in ("added", "modified"):
        new = git(root, "rev-parse", "HEAD")
        git(root, "update-index", "--add", "--cacheinfo", f"160000,{new},sub")
    if change == "deleted":
        git(root, "update-index", "--force-remove", "sub")
    result = inspect_repository(root)
    assert len(result.gitlinks) == 1
    assert result.gitlinks[0].path == "sub"
    assert ("sub" in result.protected_paths()) == (change != "unchanged")


def test_merge_conflict_is_protected(git_repo_with_commit):
    root = git_repo_with_commit
    branch = git(root, "branch", "--show-current")
    git(root, "checkout", "-qb", "other")
    (root / "README.md").write_text("other\n")
    commit(root)
    git(root, "checkout", branch)
    (root / "README.md").write_text("main\n")
    commit(root)
    import subprocess
    result = subprocess.run(["git", "merge", "other"], cwd=root, capture_output=True)
    assert result.returncode == 1
    inspection = inspect_repository(root)
    assert inspection.changes[0].kind == "unmerged"
    assert "MERGE_HEAD" in inspection.operation_markers
    assert inspection.protected_paths()["README.md"] == "pre_existing_dirty"
    assert {entry.stage for entry in inspection.index_entries} == {1, 2, 3}


def test_inspection_never_executes_local_filters_or_fsmonitor(git_repo_with_commit, tmp_path):
    root = git_repo_with_commit
    marker = tmp_path / "EXECUTED"
    helper = tmp_path / "helper"
    helper.write_text(f"#!/bin/sh\nprintf 'ran' > '{marker}'\ncat\n")
    helper.chmod(0o755)
    (root / ".gitattributes").write_text("*.md filter=unsafe\n")
    # Configure after fixture staging so only inspection can trigger the helper.
    git(root, "config", "filter.unsafe.clean", str(helper))
    git(root, "config", "filter.unsafe.process", str(helper))
    git(root, "config", "filter.unsafe.required", "true")
    git(root, "config", "core.fsmonitor", str(helper))
    (root / "README.md").write_text("changed content\n")
    identity.resolve(root)
    before = filesystem_snapshot(root)
    result = inspect_repository(root)
    assert not result.is_clean
    assert not marker.exists()
    assert filesystem_snapshot(root) == before


def test_git_environment_cannot_redirect_inspection(git_repo_with_commit, tmp_path, monkeypatch):
    root = git_repo_with_commit
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "alternate-index"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "must-not-execute")
    assert inspect_repository(root).repo_root == str(root)
    assert not (tmp_path / "alternate-index").exists()


def test_read_api_rejects_mutating_command(git_repo_with_commit):
    with pytest.raises(GitError, match="not an inspection read"):
        read_bytes(["reset", "--hard"], cwd=git_repo_with_commit)


@pytest.mark.parametrize("config_scope", ["included", "worktree"])
def test_indirect_filter_configuration_cannot_execute(git_repo_with_commit, tmp_path, config_scope):
    root = git_repo_with_commit
    marker = tmp_path / "EXECUTED"
    helper = tmp_path / "helper"
    helper.write_text(f"#!/bin/sh\nprintf 'ran' > '{marker}'\ncat\n")
    helper.chmod(0o755)
    (root / ".gitattributes").write_text("*.md filter=indirect\n")
    (root / "README.md").write_text("modified\n")
    if config_scope == "included":
        config = tmp_path / "included-config"
        config.write_text(f'[filter "indirect"]\nclean = {helper}\nrequired = true\n')
        git(root, "config", "include.path", str(config))
    else:
        git(root, "config", "extensions.worktreeConfig", "true")
        git(root, "config", "--worktree", "filter.indirect.clean", str(helper))
        git(root, "config", "--worktree", "filter.indirect.required", "true")
    inspect_repository(root)
    assert not marker.exists()


def test_all_git_subprocesses_disable_transport_and_shell(git_repo_with_commit, monkeypatch):
    from code_slayer.repo import git as git_module

    execute = git_module.subprocess.run
    calls = []

    def check(*args, **kwargs):
        assert kwargs["shell"] is False
        assert kwargs["env"]["GIT_ALLOW_PROTOCOL"] == ""
        assert kwargs["env"]["GIT_NO_LAZY_FETCH"] == "1"
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        calls.append(args[0])
        return execute(*args, **kwargs)

    monkeypatch.setattr(git_module.subprocess, "run", check)
    inspect_repository(git_repo_with_commit)
    assert calls and all(call[0] == "git" for call in calls)


def test_git_error_does_not_become_a_clean_observation(git_repo_with_commit, monkeypatch):
    from code_slayer.repo import git as git_module

    execute = git_module.read_bytes

    def fail(args, **kwargs):
        if args[0] == "status":
            raise GitError(tuple(args), 128, "unavailable Git data")
        return execute(args, **kwargs)

    monkeypatch.setattr(git_module, "read_bytes", fail)
    with pytest.raises(GitError):
        inspect_repository(git_repo_with_commit)
