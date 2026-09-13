"""Shared pytest fixtures.

Every fixture here resolves to a temporary directory. No test in this
suite ever touches a real user path (`~/.local/share/codeslayer`) or a
real, non-temporary Git repository.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# Make `import code_slayer` work even without an editable install, so the
# test suite doesn't silently depend on `pip install -e` having run.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from code_slayer.store import db as db_module  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state_root(tmp_path, monkeypatch):
    """Force CODESLAYER_STATE_ROOT to a fresh temp dir for every test, so a
    test can never resolve to a real user path even if it forgets to ask."""
    state_root = tmp_path / "_codeslayer_state_root"
    monkeypatch.setenv("CODESLAYER_STATE_ROOT", str(state_root))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return state_root


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def git_repo(tmp_path) -> Path:
    """A freshly `git init`'d repository with no commits yet."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], cwd=repo)
    _run_git(["config", "user.email", "test@example.invalid"], cwd=repo)
    _run_git(["config", "user.name", "Code Slayer Tests"], cwd=repo)
    return repo


@pytest.fixture
def git_repo_with_commit(git_repo) -> Path:
    """A repo with one commit, so HEAD is not unborn."""
    (git_repo / "README.md").write_text("hello\n")
    _run_git(["add", "README.md"], cwd=git_repo)
    _run_git(["commit", "-q", "-m", "initial commit"], cwd=git_repo)
    return git_repo


@pytest.fixture
def db_conn(tmp_path):
    """A migrated, temporary SQLite connection."""
    conn = db_module.connect(tmp_path / "state.db")
    db_module.migrate(conn)
    yield conn
    conn.close()
