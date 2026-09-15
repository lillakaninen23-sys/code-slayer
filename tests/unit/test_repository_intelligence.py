"""Deterministic repository intelligence (Phase 8.1).

Covers: exact-HEAD-bound snapshot identity and restart persistence; HEAD
and working-tree-content invalidation; content-safe identity that
cannot be fooled by a preserved size and/or mtime (Phase 8.1a);
deterministic, bounded, safety-respecting file inventory (exclusions,
binary/oversized handling, symlink-escape denial); evidence-only
project/language detection and command discovery (never executed);
Python AST symbol extraction that fails locally, never
repository-wide; a deterministic internal import graph; deterministic
relevance ranking and bounded context-pack assembly with recorded
truncation; and the read-only guarantee."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_slayer.intelligence import builder, commands, symbols
from code_slayer.intelligence.limits import MAX_TEXT_FILE_BYTES
from code_slayer.intelligence.service import RepositoryIntelligenceService
from tests.repo_helpers import git


@pytest.fixture
def state_root(tmp_path):
    root = tmp_path / "_codeslayer_state"
    root.mkdir()
    return root


@pytest.fixture
def service(git_repo_with_commit, state_root):
    svc = RepositoryIntelligenceService(git_repo_with_commit, state_root_override=state_root)
    yield svc
    svc.close()


# --- 1/2/3. snapshot identity, restart persistence, HEAD invalidation ------

def test_snapshot_bound_to_exact_head(service, git_repo_with_commit):
    snapshot = service.inspect()
    assert snapshot.head_sha == git(git_repo_with_commit, "rev-parse", "HEAD")
    assert snapshot.repo_id and snapshot.worktree_id
    assert snapshot.working_tree_dirty is False


def test_snapshot_persists_across_restart(git_repo_with_commit, state_root):
    first = RepositoryIntelligenceService(git_repo_with_commit, state_root_override=state_root)
    try:
        built = first.inspect()
    finally:
        first.close()

    restarted = RepositoryIntelligenceService(git_repo_with_commit, state_root_override=state_root)
    try:
        status = restarted.status()
        assert status.indexed and status.current
        assert status.snapshot_id == built.snapshot_id
        assert status.head_sha == built.head_sha
        candidates, stale = restarted.query("README")
        assert stale is False
    finally:
        restarted.close()


def test_head_change_invalidates_snapshot(service, git_repo_with_commit):
    service.inspect()
    assert service.status().current is True
    (git_repo_with_commit / "new.txt").write_text("x\n")
    git(git_repo_with_commit, "add", "new.txt")
    git(git_repo_with_commit, "commit", "-q", "-m", "second commit")
    status = service.status()
    assert status.current is False  # stale HEAD, not silently treated as current
    _candidates, stale = service.query("anything")
    assert stale is True
    refreshed = service.inspect()
    assert refreshed.head_sha == git(git_repo_with_commit, "rev-parse", "HEAD")
    assert service.status().current is True


def test_working_tree_edit_without_commit_invalidates_snapshot(service, git_repo_with_commit):
    """§10: HEAD unchanged, but indexed file content changed -- must
    still be detected, not silently treated as still-current."""
    service.inspect()
    (git_repo_with_commit / "README.md").write_text("changed content\n")
    assert service.status().current is False


# --- 8.1a. content-safe identity: never fooled by a preserved size/mtime ---
#
# The old `working_tree_fingerprint` was `path:size:mtime_ns` alone -- a
# file whose content changed but whose path, byte size, and mtime were
# all preserved would still report CURRENT. These tests prove the
# replacement identity (`intelligence.builder._working_tree_identity`)
# never makes that mistake, using real content hashes for every
# dirty/untracked/masked indexed path instead of trusting stat() metadata.

def _same_stat_rewrite(path: Path, new_bytes: bytes) -> None:
    """Rewrite `path` with `new_bytes` (same length as the current
    content) and restore the original mtime -- the strongest form of
    the regression this phase fixes: an editor that changes content but
    leaves size and mtime exactly as they were."""
    original = path.stat()
    assert len(new_bytes) == original.st_size, "test fixture must preserve exact byte size"
    path.write_bytes(new_bytes)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))


def test_clean_repo_with_unchanged_head_reports_current(service):
    service.inspect()
    assert service.status().current is True
    assert service.status().current is True  # repeated probe agrees, no drift


def test_tracked_edit_preserving_exact_byte_size_invalidates_snapshot(
    service, git_repo_with_commit,
):
    service.inspect()
    readme = git_repo_with_commit / "README.md"
    assert readme.read_bytes() == b"hello\n"
    readme.write_bytes(b"bye!!\n")  # same 6-byte size, different content
    assert service.status().current is False


def test_tracked_edit_preserving_size_and_restored_mtime_invalidates_snapshot(
    service, git_repo_with_commit,
):
    """The regression this phase exists to fix: the old stat()-only
    fingerprint (`path:size:mtime_ns`) cannot distinguish this from an
    untouched file."""
    service.inspect()
    assert service.status().current is True
    _same_stat_rewrite(git_repo_with_commit / "README.md", b"bye!!\n")
    assert service.status().current is False


def test_untracked_edit_preserving_size_and_mtime_invalidates_snapshot(
    service, git_repo_with_commit,
):
    extra = git_repo_with_commit / "notes.txt"
    extra.write_text("first!\n")
    service.inspect(force=True)
    assert service.status().current is True
    _same_stat_rewrite(extra, b"abcdef\n")
    assert service.status().current is False


def test_tracked_deletion_invalidates_snapshot(service, git_repo_with_commit):
    service.inspect()
    assert service.status().current is True
    (git_repo_with_commit / "README.md").unlink()
    assert service.status().current is False


def test_binary_content_change_preserving_size_and_mtime_invalidates_snapshot(
    service, git_repo_with_commit,
):
    image = git_repo_with_commit / "image.bin"
    image.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00AAAA")
    service.inspect(force=True)
    assert service.status().current is True
    _same_stat_rewrite(image, b"\x89PNG\r\n\x1a\n\x00\x00\x00BBBB")
    assert service.status().current is False


def test_oversized_content_change_preserving_size_and_mtime_invalidates_snapshot(
    service, git_repo_with_commit,
):
    size = MAX_TEXT_FILE_BYTES + 1024
    huge = git_repo_with_commit / "huge.txt"
    huge.write_bytes(b"a" * size)
    service.inspect(force=True)
    assert service.status().current is True
    _same_stat_rewrite(huge, b"b" * size)
    assert service.status().current is False


def test_restart_identifies_current_snapshot_with_dirty_worktree(git_repo_with_commit, state_root):
    """§9: a durable snapshot taken while the worktree was dirty must
    still be recognized as current after a fresh process restart --
    the stronger content identity is itself restart-stable."""
    (git_repo_with_commit / "scratch.txt").write_text("draft\n")
    first = RepositoryIntelligenceService(git_repo_with_commit, state_root_override=state_root)
    try:
        built = first.inspect()
        assert built.working_tree_dirty is True
    finally:
        first.close()

    restarted = RepositoryIntelligenceService(git_repo_with_commit, state_root_override=state_root)
    try:
        status = restarted.status()
        assert status.indexed and status.current
        assert status.snapshot_id == built.snapshot_id
    finally:
        restarted.close()


def test_probe_identity_deterministic_across_repeated_calls(git_repo_with_commit):
    first = builder.probe_identity(git_repo_with_commit)
    second = builder.probe_identity(git_repo_with_commit)
    assert first.working_tree_fingerprint == second.working_tree_fingerprint
    assert first.head_sha == second.head_sha

    (git_repo_with_commit / "dirty.txt").write_text("wip\n")
    third = builder.probe_identity(git_repo_with_commit)
    fourth = builder.probe_identity(git_repo_with_commit)
    assert third.working_tree_fingerprint == fourth.working_tree_fingerprint
    assert third.working_tree_fingerprint != first.working_tree_fingerprint


def test_probing_identity_never_mutates_the_repository(service, git_repo_with_commit):
    (git_repo_with_commit / "extra.txt").write_text("evidence\n")
    before_status = git(git_repo_with_commit, "status", "--porcelain")
    before_stat = (git_repo_with_commit / "extra.txt").stat()
    service.inspect()
    service.status()
    service.query("anything")
    after_status = git(git_repo_with_commit, "status", "--porcelain")
    after_stat = (git_repo_with_commit / "extra.txt").stat()
    assert before_status == after_status
    assert before_stat.st_mtime_ns == after_stat.st_mtime_ns
    assert before_stat.st_size == after_stat.st_size


# --- 4/5/6/7/8/9. deterministic, bounded, safe inventory --------------------

def test_file_inventory_is_deterministic(service):
    first = service.inspect(force=True)
    second = service.inspect(force=True)
    assert tuple(f.path for f in first.files) == tuple(f.path for f in second.files)
    assert first.working_tree_fingerprint == second.working_tree_fingerprint


def test_git_internals_excluded(service, git_repo_with_commit):
    snapshot = service.inspect()
    assert not any(f.path.startswith(".git/") or f.path == ".git" for f in snapshot.files)


def test_codeslayer_state_excluded(git_repo_with_commit, state_root):
    """Even if Code Slayer's own external state root is configured
    *inside* the inspected repository, its content must never be
    inventoried."""
    nested_state = git_repo_with_commit / ".codeslayer-state"
    nested_state.mkdir()
    svc = RepositoryIntelligenceService(git_repo_with_commit, state_root_override=nested_state)
    try:
        # Establishing the service already wrote a real state.db under
        # nested_state -- an untracked, undeniably-real file that would
        # otherwise be indexed.
        snapshot = svc.inspect()
        assert not any(f.path.startswith(".codeslayer-state") for f in snapshot.files)
    finally:
        svc.close()


def test_vendor_cache_dirs_excluded(service, git_repo_with_commit):
    for name in ("node_modules", "__pycache__", ".venv", "dist", "build"):
        directory = git_repo_with_commit / name
        directory.mkdir()
        (directory / "x.txt").write_text("vendor\n")
    snapshot = service.inspect(force=True)
    assert not any(
        any(part in {"node_modules", "__pycache__", ".venv", "dist", "build"}
            for part in Path(f.path).parts)
        for f in snapshot.files
    )


def test_binary_file_not_indexed_as_text(service, git_repo_with_commit):
    (git_repo_with_commit / "image.bin").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00binary")
    snapshot = service.inspect(force=True)
    record = next(f for f in snapshot.files if f.path == "image.bin")
    assert record.classification == "binary"
    assert record.content_hash is not None  # bytes hashed, never decoded as text


def test_oversized_file_handled_boundedly(service, git_repo_with_commit):
    (git_repo_with_commit / "huge.txt").write_text("x" * (MAX_TEXT_FILE_BYTES + 1))
    snapshot = service.inspect(force=True)
    record = next(f for f in snapshot.files if f.path == "huge.txt")
    assert record.classification == "oversized"
    assert record.content_hash is None  # never read/hashed


def test_symlink_escape_denied(service, git_repo_with_commit, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    link = git_repo_with_commit / "escape.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unsupported in this environment")
    snapshot = service.inspect(force=True)
    record = next((f for f in snapshot.files if f.path == "escape.txt"), None)
    # Either excluded entirely, or recorded without ever reading the
    # escaped target's content -- never a content_hash of `outside.txt`.
    if record is not None:
        import hashlib

        assert record.content_hash != hashlib.sha256(b"secret\n").hexdigest()


# --- 10/11/12. project/language detection -----------------------------------

def test_python_project_detected(service, git_repo_with_commit):
    (git_repo_with_commit / "pyproject.toml").write_text('[project]\nname = "demo"\n')
    snapshot = service.inspect(force=True)
    python_project = next(p for p in snapshot.projects if p.kind == "python")
    assert "pyproject.toml" in python_project.evidence_paths
    assert python_project.facts.get("name") == "demo"


def test_node_project_detected(service, git_repo_with_commit):
    (git_repo_with_commit / "package.json").write_text(
        '{"name": "demo", "scripts": {"test": "vitest", "lint": "eslint ."}}\n',
    )
    snapshot = service.inspect(force=True)
    node_project = next(p for p in snapshot.projects if p.kind == "node")
    assert "package.json" in node_project.evidence_paths
    assert node_project.facts.get("name") == "demo"


def test_project_evidence_references_correct_paths(service, git_repo_with_commit):
    (git_repo_with_commit / "Cargo.toml").write_text('[package]\nname = "demo"\n')
    snapshot = service.inspect(force=True)
    rust_project = next(p for p in snapshot.projects if p.kind == "rust")
    assert rust_project.evidence_paths == ("Cargo.toml",)
    for evidence_path in rust_project.evidence_paths:
        assert any(f.path == evidence_path for f in snapshot.files)


# --- 13/14/15. command discovery: discovery only, never execution ----------

def test_test_command_discovered_from_config(git_repo_with_commit):
    (git_repo_with_commit / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n[tool.pytest.ini_options]\n',
    )
    paths = {"pyproject.toml"}
    read = lambda name: (git_repo_with_commit / name).read_text()  # noqa: E731
    found = commands.discover_commands(paths, read)
    assert any(c.command == "pytest" and c.purpose == "test" for c in found)


def test_lint_command_discovered(git_repo_with_commit):
    (git_repo_with_commit / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n[tool.ruff]\nline-length = 100\n',
    )
    paths = {"pyproject.toml"}
    read = lambda name: (git_repo_with_commit / name).read_text()  # noqa: E731
    found = commands.discover_commands(paths, read)
    assert any(c.command == "ruff check ." and c.purpose == "lint" for c in found)


def test_command_discovery_never_executes(monkeypatch, git_repo_with_commit):
    (git_repo_with_commit / "Makefile").write_text("test:\n\techo should-never-run\n")
    calls = []

    def _forbidden_run(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("command discovery must never execute anything")

    monkeypatch.setattr("subprocess.run", _forbidden_run)
    paths = {"Makefile"}
    read = lambda name: (git_repo_with_commit / name).read_text()  # noqa: E731
    found = commands.discover_commands(paths, read)
    assert any(c.command == "make test" for c in found)
    assert not calls


# --- 16/17/18. symbol extraction and import graph ---------------------------

def test_python_ast_symbol_extraction(service, git_repo_with_commit):
    (git_repo_with_commit / "mod.py").write_text(
        "class Widget:\n    def render(self):\n        pass\n\n\ndef helper():\n    pass\n",
    )
    snapshot = service.inspect(force=True)
    names = {(s.kind, s.name) for s in snapshot.symbols if s.path == "mod.py"}
    assert ("module", "mod") in names
    assert ("class", "Widget") in names
    assert ("method", "render") in names
    assert ("function", "helper") in names


def test_import_edges_deterministic(service, git_repo_with_commit):
    (git_repo_with_commit / "a.py").write_text("import b\n")
    (git_repo_with_commit / "b.py").write_text("x = 1\n")
    first = service.inspect(force=True)
    second = service.inspect(force=True)
    edge = lambda s: {(e.source, e.target, e.relation) for e in s.edges}  # noqa: E731
    assert ("a.py", "b.py", "imports") in edge(first)
    assert edge(first) == edge(second)


def test_syntax_error_file_does_not_break_snapshot(service, git_repo_with_commit):
    (git_repo_with_commit / "broken.py").write_text("def broken(:\n")
    (git_repo_with_commit / "ok.py").write_text("def fine():\n    pass\n")
    snapshot = service.inspect(force=True)
    assert any(e.path == "broken.py" for e in snapshot.symbol_errors)
    assert any(s.name == "fine" for s in snapshot.symbols)


def test_symbol_extraction_error_is_local():
    with pytest.raises(symbols.SymbolExtractionError):
        symbols.extract("bad.py", "def f(:\n")
    # A different, well-formed file is entirely unaffected by the above.
    result = symbols.extract("ok.py", "def f():\n    pass\n")
    assert result is not None and result.symbols[-1].name == "f"


# --- 19/20/21/22. deterministic relevance ranking ---------------------------

def test_filename_mention_ranks_highly(service, git_repo_with_commit):
    (git_repo_with_commit / "billing.py").write_text("def charge():\n    pass\n")
    (git_repo_with_commit / "unrelated.py").write_text("def other():\n    pass\n")
    service.inspect(force=True)
    candidates, _stale = service.query("Refactor billing.py please")
    assert candidates and candidates[0].path == "billing.py"
    assert any(r.startswith("path_mention:") for r in candidates[0].reasons)


def test_symbol_mention_ranks_defining_file(service, git_repo_with_commit):
    (git_repo_with_commit / "core.py").write_text("def execute_guarded_turn():\n    pass\n")
    service.inspect(force=True)
    candidates, _stale = service.query("please call execute_guarded_turn now")
    assert candidates[0].path == "core.py"
    assert any("symbol_match:execute_guarded_turn" in r for r in candidates[0].reasons)


def test_import_neighbor_contributes_relevance(service, git_repo_with_commit):
    (git_repo_with_commit / "core.py").write_text("def target_symbol():\n    pass\n")
    (git_repo_with_commit / "helper.py").write_text("import core\n")
    service.inspect(force=True)
    candidates, _stale = service.query("target_symbol")
    by_path = {c.path: c for c in candidates}
    assert "helper.py" in by_path
    assert any(r.startswith("import_neighbor:") for r in by_path["helper.py"].reasons)


def test_ranking_deterministic_across_runs(service, git_repo_with_commit):
    (git_repo_with_commit / "core.py").write_text("def widget():\n    pass\n")
    service.inspect(force=True)
    first, _ = service.query("widget")
    second, _ = service.query("widget")
    assert first == second


# --- 23/24/25. bounded context packs ----------------------------------------

def test_context_pack_file_limit(service, git_repo_with_commit):
    for i in range(5):
        (git_repo_with_commit / f"m{i}.py").write_text("def widget():\n    pass\n")
    service.inspect(force=True)
    pack = service.build_context_pack("widget", max_files=2, max_bytes=1_000_000)
    assert 0 < len(pack.files) <= 2
    assert pack.budget_exhausted is True


def test_context_pack_byte_budget(service, git_repo_with_commit):
    (git_repo_with_commit / "big.py").write_text("def widget():\n    pass\n" + "# pad\n" * 5000)
    service.inspect(force=True)
    pack = service.build_context_pack("widget", max_files=1, max_bytes=200, per_file_bytes=200)
    assert len(pack.files[0].content.encode("utf-8")) <= 200
    assert pack.files[0].truncated is True


def test_context_pack_records_omission(service, git_repo_with_commit):
    for i in range(3):
        (git_repo_with_commit / f"m{i}.py").write_text("def widget():\n    pass\n")
    service.inspect(force=True)
    pack = service.build_context_pack("widget", max_files=1, max_bytes=1_000_000)
    assert pack.budget_exhausted is True
    assert len(pack.omitted) >= 1
    assert not any(f.path in pack.omitted for f in pack.files)


def test_context_pack_never_dumps_whole_repository(service, git_repo_with_commit):
    for i in range(30):
        (git_repo_with_commit / f"file{i}.py").write_text("def shared_symbol():\n    pass\n")
    service.inspect(force=True)
    pack = service.build_context_pack("shared_symbol")
    assert 0 < len(pack.files) < 30


# --- 27/28. read-only + staleness guarantees --------------------------------

def test_read_only_guarantees(service, git_repo_with_commit):
    before = git(git_repo_with_commit, "rev-parse", "HEAD")
    before_status = git(git_repo_with_commit, "status", "--porcelain")
    service.inspect(force=True)
    service.query("anything")
    service.build_context_pack("anything")
    service.status()
    assert git(git_repo_with_commit, "rev-parse", "HEAD") == before
    assert git(git_repo_with_commit, "status", "--porcelain") == before_status


def test_stale_snapshot_not_returned_as_current(service, git_repo_with_commit):
    service.inspect()
    (git_repo_with_commit / "README.md").write_text("edited\n")
    status = service.status()
    assert status.indexed is True
    assert status.current is False
