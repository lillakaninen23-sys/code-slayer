"""Discovery boundaries, roles, deterministic scope, and raw byte identity."""

import hashlib
import os

import pytest

from code_slayer.repo.inspection import InspectionError, inspect_repository
from code_slayer.repo.rules import DocumentRole, discover_rules
from tests.repo_helpers import commit


def write(root, path, content=b"instructions\n"):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(content)


def test_root_and_nested_roles_scope_and_precedence(git_repo_with_commit):
    root = git_repo_with_commit
    for name in ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "src/AGENTS.md",
                 "src/deep/AGENTS.md", "src/README.md", "sibling/AGENTS.md"):
        write(root, name)
    found = discover_rules(inspect_repository(root))
    by_name = {d.source_path: d for d in found.documents}
    assert len(by_name) == 8
    assert by_name["AGENTS.md"].role == DocumentRole.AUTHORITATIVE
    assert by_name["AGENTS.md"].scope == "."
    assert by_name["AGENTS.md"].precedence_rank == 0
    assert by_name["src/AGENTS.md"].scope == "src"
    assert by_name["src/AGENTS.md"].precedence_rank == 1
    assert by_name["src/deep/AGENTS.md"].precedence_rank == 2
    assert by_name["CLAUDE.md"].role == DocumentRole.DISCOVERED
    assert by_name["CLAUDE.md"].precedence_rank == -1
    for name in ("README.md", "CONTRIBUTING.md", "src/README.md"):
        assert by_name[name].role == DocumentRole.INFORMATIONAL
        assert by_name[name].source_kind == "documentation"
        assert by_name[name].precedence_rank == -1
    assert [d.source_path for d in found.instructions_for("src/deep/file.py")] == [
        "AGENTS.md", "src/AGENTS.md", "src/deep/AGENTS.md",
    ]
    assert [d.source_path for d in found.instructions_for("src-other/file")] == ["AGENTS.md"]
    again = discover_rules(inspect_repository(root))
    assert found.identity() == again.identity()
    assert [d.metadata() | {"discovered_at": None} for d in found.documents] == [
        d.metadata() | {"discovered_at": None} for d in again.documents
    ]


def test_raw_bytes_are_hashed_without_interpretation(git_repo_with_commit):
    root = git_repo_with_commit
    data = b"\xef\xbb\xbfRule\r\n@outside-file\n\xff\x00"
    write(root, "AGENTS.md", data)
    rule = next(d for d in discover_rules(inspect_repository(root)).documents
                if d.source_path == "AGENTS.md")
    assert rule.content == data
    assert rule.content_hash == hashlib.sha256(data).hexdigest()
    assert "content" not in rule.metadata()
    assert "outside-file" not in repr(rule)


def test_ignored_documents_and_git_internal_files_are_not_discovered(git_repo_with_commit):
    root = git_repo_with_commit
    write(root, ".gitignore", b"ignored/\n")
    commit(root)
    write(root, "ignored/AGENTS.md")
    write(root, ".git/AGENTS.md")
    write(root, "agents.md")
    found = discover_rules(inspect_repository(root))
    assert [d.source_path for d in found.documents] == ["README.md"]


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_symlinks_never_read_outside_the_repo(git_repo_with_commit, tmp_path, kind):
    root = git_repo_with_commit
    secret = b"DO_NOT_READ_THIS_EXTERNAL_CONTENT"
    outside = tmp_path / "outside"
    outside.mkdir()
    write(outside, "AGENTS.md", secret)
    if kind == "file":
        (root / "AGENTS.md").symlink_to(outside / "AGENTS.md")
    else:
        write(root, "nested/AGENTS.md")
        commit(root)
        (root / "nested/AGENTS.md").unlink()
        (root / "nested").rmdir()
        (root / "nested").symlink_to(outside, target_is_directory=True)
    found = discover_rules(inspect_repository(root))
    assert all(secret != d.content for d in found.documents)
    assert len(found.skipped) == 1
    assert found.skipped[0].reason == "missing_or_symlink_path"


def test_fifo_is_not_opened_as_blocking_instruction_stream(git_repo_with_commit):
    os.mkfifo(git_repo_with_commit / "AGENTS.md")
    found = discover_rules(inspect_repository(git_repo_with_commit))
    # Git can omit special untracked files entirely; neither outcome reads it.
    assert all(d.source_path != "AGENTS.md" for d in found.documents)


def test_deleted_instruction_is_explicitly_unavailable(git_repo_with_commit):
    root = git_repo_with_commit
    write(root, "AGENTS.md")
    commit(root)
    (root / "AGENTS.md").unlink()
    found = discover_rules(inspect_repository(root))
    assert found.skipped[0].source_path == "AGENTS.md"
    assert found.skipped[0].reason == "missing_or_symlink_path"


def test_oversized_instruction_fails_without_truncating(git_repo_with_commit):
    write(git_repo_with_commit, "AGENTS.md", b"x" * 100)
    with pytest.raises(InspectionError, match="byte limit"):
        discover_rules(inspect_repository(git_repo_with_commit), max_document_bytes=20)


def test_embedded_repository_is_not_traversed(git_repo_with_commit):
    from tests.repo_helpers import git

    root = git_repo_with_commit
    nested = root / "nested"
    nested.mkdir()
    git(nested, "init", "-q")
    write(nested, "AGENTS.md", b"external instructions")
    found = discover_rules(inspect_repository(root))
    assert all(not d.source_path.startswith("nested/") for d in found.documents)


def test_tracked_directory_turned_into_repository_is_a_boundary(git_repo_with_commit):
    from tests.repo_helpers import git

    root = git_repo_with_commit
    write(root, "nested/AGENTS.md")
    commit(root)
    git(root / "nested", "init", "-q")
    found = discover_rules(inspect_repository(root))
    assert all(d.source_path != "nested/AGENTS.md" for d in found.documents)
    assert found.skipped[0].reason == "nested_repository_boundary"


@pytest.mark.parametrize("path", ["../outside", "/absolute", "src/../outside"])
def test_scope_lookup_rejects_outside_paths(git_repo_with_commit, path):
    found = discover_rules(inspect_repository(git_repo_with_commit))
    with pytest.raises(InspectionError):
        found.instructions_for(path)
