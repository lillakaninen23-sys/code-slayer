"""`ground_verification_commands()`: pure, read-only, tree-directed
discovery -- never trusts planner text, never executes anything. Tested
here directly against a plain `tmp_path` standing in for the isolated
verification tree `finalization.service.Finalizer` materializes in
production."""

from __future__ import annotations

from code_slayer.finalization.verification import (
    _ARGV_BY_COMMAND,
    ground_verification_commands,
)


def test_empty_repository_grounds_nothing(tmp_path):
    assert ground_verification_commands(tmp_path) == ()


def test_high_confidence_pyproject_tools_are_grounded(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest]\n[tool.ruff]\n[tool.mypy]\n",
    )
    commands = {c.command for c in ground_verification_commands(tmp_path)}
    assert commands == {"pytest", "ruff check .", "mypy ."}
    for candidate in ground_verification_commands(tmp_path):
        assert candidate.command in _ARGV_BY_COMMAND
        assert candidate.confidence == "high"


def test_formatter_commands_are_mapped_to_their_read_only_check_form():
    """Verification must never mutate the code it verifies: the
    discovered formatter labels ("ruff format .", "black .") are real,
    repository-declared invocations that would rewrite files -- the
    fixed argv map always substitutes each tool's own non-mutating
    `--check` invocation instead."""
    assert _ARGV_BY_COMMAND["ruff format ."] == ("ruff", "format", "--check", ".")
    assert _ARGV_BY_COMMAND["black ."] == ("black", "--check", ".")


def test_no_mapped_argv_contains_a_mutating_formatter_invocation():
    mutating = {("ruff", "format", "."), ("black", ".")}
    assert not any(argv in mutating for argv in _ARGV_BY_COMMAND.values())


def test_medium_confidence_tox_is_discoverable_but_not_executable(tmp_path):
    (tmp_path / "tox.ini").write_text("[tox]\n")
    # discover_commands() itself would find "tox", but it is medium
    # confidence and (deliberately, for this first vertical slice) has no
    # entry in the fixed executable argv map -- never executed.
    assert ground_verification_commands(tmp_path) == ()


def test_makefile_target_without_a_fixed_argv_mapping_is_not_executed(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\techo hi\n")
    # "make test" is high-confidence but has no entry in _ARGV_BY_COMMAND
    # in this first slice -- discoverable only, never executed.
    assert ground_verification_commands(tmp_path) == ()


def test_malformed_pyproject_toml_grounds_nothing_not_a_crash(tmp_path):
    (tmp_path / "pyproject.toml").write_text("not [ valid toml")
    assert ground_verification_commands(tmp_path) == ()


def test_oversized_root_file_is_ignored(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest]\n" + "x" * 2_000_000)
    assert ground_verification_commands(tmp_path) == ()


def test_planner_text_is_never_consulted():
    """`ground_verification_commands()` takes only a tree root -- there is
    no parameter through which a planner's own asserted command text could
    ever reach it."""
    import inspect

    sig = inspect.signature(ground_verification_commands)
    assert list(sig.parameters) == ["tree_root"]
