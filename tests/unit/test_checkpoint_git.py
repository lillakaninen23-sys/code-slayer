"""Narrow checkpoint Git plumbing: never touches the real index/HEAD/branch."""

from __future__ import annotations

import pytest

from code_slayer.repo import checkpoint_git as cg
from tests.repo_helpers import git


def test_empty_tree_sha_is_the_well_known_constant():
    assert cg.EMPTY_TREE_SHA == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def test_resolve_tree_sha_of_none_is_empty_tree(git_repo):
    assert cg.resolve_tree_sha(None, cwd=git_repo) == cg.EMPTY_TREE_SHA


def test_resolve_tree_sha_of_head(git_repo_with_commit):
    root = git_repo_with_commit
    head = git(root, "rev-parse", "HEAD")
    expected = git(root, "rev-parse", f"{head}^{{tree}}")
    assert cg.resolve_tree_sha(head, cwd=root) == expected


def test_hash_blob_matches_git_hash_object(git_repo):
    data = b"hello checkpoint world\n"
    sha = cg.hash_blob(data, cwd=git_repo)
    # The object is actually written and readable back via plain git.
    # (`git()` strips trailing whitespace from captured stdout.)
    assert git(git_repo, "cat-file", "-p", sha) == data.decode().rstrip()


def test_object_exists(git_repo):
    sha = cg.hash_blob(b"present", cwd=git_repo)
    assert cg.object_exists(sha, cwd=git_repo)
    assert not cg.object_exists("0" * 40, cwd=git_repo)


def test_build_tree_overlays_onto_base_without_touching_real_index(git_repo_with_commit, tmp_path):
    root = git_repo_with_commit
    real_index_before = (root / ".git" / "index").read_bytes()
    base_tree = cg.resolve_tree_sha(git(root, "rev-parse", "HEAD"), cwd=root)
    blob = cg.hash_blob(b"new content", cwd=root)
    tree = cg.build_tree(
        base_tree, (cg.TreeEdit("added.txt", blob),),
        cwd=root, index_path=tmp_path / "idx",
    )
    listing = git(root, "ls-tree", "-r", "--name-only", tree)
    assert "README.md" in listing.splitlines()  # base content preserved
    assert "added.txt" in listing.splitlines()  # overlay applied
    # The real repository index and working tree are completely untouched.
    assert (root / ".git" / "index").read_bytes() == real_index_before
    assert not (root / "added.txt").exists()


def test_build_tree_rejects_path_with_embedded_control_character(git_repo, tmp_path):
    """Defense in depth against `--index-info` line injection: an embedded
    newline in a path must never be allowed to smuggle in an unrelated
    extra tree entry, independent of whatever validated it upstream."""
    blob = cg.hash_blob(b"payload", cwd=git_repo)
    evil_path = "innocent.txt\n100644 " + blob + "\tinjected.txt"
    with pytest.raises(cg.CheckpointGitError):
        cg.build_tree(
            cg.EMPTY_TREE_SHA, (cg.TreeEdit(evil_path, blob),),
            cwd=git_repo, index_path=tmp_path / "idx",
        )


def test_build_tree_can_remove_a_path_from_the_base(git_repo, tmp_path):
    root = git_repo
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "seed")
    base_tree = cg.resolve_tree_sha(git(root, "rev-parse", "HEAD"), cwd=root)
    tree = cg.build_tree(
        base_tree, (cg.TreeEdit("a.txt", None),), cwd=root, index_path=tmp_path / "idx",
    )
    listing = git(root, "ls-tree", "-r", "--name-only", tree).splitlines()
    assert "a.txt" not in listing
    assert "b.txt" in listing


def test_build_tree_from_empty_base(git_repo, tmp_path):
    blob = cg.hash_blob(b"root content", cwd=git_repo)
    tree = cg.build_tree(
        cg.EMPTY_TREE_SHA, (cg.TreeEdit("only.txt", blob),),
        cwd=git_repo, index_path=tmp_path / "idx",
    )
    assert git(git_repo, "ls-tree", "-r", "--name-only", tree).strip() == "only.txt"


def test_commit_tree_root_commit_has_no_parents(git_repo, tmp_path):
    blob = cg.hash_blob(b"x", cwd=git_repo)
    tree = cg.build_tree(
        cg.EMPTY_TREE_SHA, (cg.TreeEdit("f.txt", blob),), cwd=git_repo, index_path=tmp_path / "idx",
    )
    commit = cg.commit_tree(tree, (), "root checkpoint\n", cwd=git_repo)
    tree_back, parents = cg.read_commit(commit, cwd=git_repo)
    assert tree_back == tree
    assert parents == ()


def test_commit_tree_with_parent(git_repo_with_commit, tmp_path):
    root = git_repo_with_commit
    head = git(root, "rev-parse", "HEAD")
    tree = cg.resolve_tree_sha(head, cwd=root)
    commit = cg.commit_tree(tree, (head,), "checkpoint on top\n", cwd=root)
    tree_back, parents = cg.read_commit(commit, cwd=root)
    assert tree_back == tree
    assert parents == (head,)


def test_create_ref_is_create_only_and_never_touches_head(git_repo_with_commit):
    root = git_repo_with_commit
    head_before = (root / ".git" / "HEAD").read_bytes()
    tree = cg.resolve_tree_sha(git(root, "rev-parse", "HEAD"), cwd=root)
    commit = cg.commit_tree(tree, (), "cp\n", cwd=root)
    ref = "refs/codeslayer/checkpoints/task-1/0"
    cg.create_ref(ref, commit, cwd=root)
    assert cg.resolve_ref(ref, cwd=root) == commit
    assert (root / ".git" / "HEAD").read_bytes() == head_before
    assert git(root, "rev-parse", "HEAD") != commit
    # A second create at the same ref name must fail (compare-and-swap:
    # never force-moves an existing checkpoint ref).
    other_commit = cg.commit_tree(tree, (), "cp2\n", cwd=root)
    with pytest.raises(cg.CheckpointGitError):
        cg.create_ref(ref, other_commit, cwd=root)
    assert cg.resolve_ref(ref, cwd=root) == commit


def test_resolve_ref_absent_returns_none(git_repo):
    assert cg.resolve_ref("refs/codeslayer/checkpoints/nope/0", cwd=git_repo) is None


def test_read_commit_unknown_sha_raises(git_repo):
    with pytest.raises(cg.CheckpointGitError):
        cg.read_commit("0" * 40, cwd=git_repo)
