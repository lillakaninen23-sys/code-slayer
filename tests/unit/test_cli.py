"""Minimal CLI wiring smoke test."""

from __future__ import annotations

from click.testing import CliRunner

from code_slayer.cli.main import cli


def test_inspect_prints_identity(git_repo):
    runner = CliRunner()
    result = runner.invoke(cli, ["inspect", str(git_repo)])
    assert result.exit_code == 0, result.output
    assert "repo_id:" in result.output
    assert "worktree_id:" in result.output


def test_inspect_on_non_repo_fails_cleanly(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli, ["inspect", str(not_a_repo)])
    assert result.exit_code != 0


def test_version_flag():
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "codeslayer" in result.output
