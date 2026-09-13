"""External durable-state location resolution."""

from __future__ import annotations

from pathlib import Path

from code_slayer.store import location


def test_env_override_wins_over_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("CODESLAYER_STATE_ROOT", str(tmp_path / "from-env"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "from-xdg"))
    assert location.state_root() == tmp_path / "from-env"


def test_explicit_override_wins_over_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("CODESLAYER_STATE_ROOT", str(tmp_path / "from-env"))
    explicit = tmp_path / "explicit"
    assert location.state_root(override=explicit) == explicit


def test_defaults_to_xdg_data_home_codeslayer(monkeypatch, tmp_path):
    monkeypatch.delenv("CODESLAYER_STATE_ROOT", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert location.state_root() == tmp_path / "xdg" / "codeslayer"


def test_falls_back_to_home_local_share_when_xdg_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("CODESLAYER_STATE_ROOT", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert location.state_root() == tmp_path / ".local" / "share" / "codeslayer"


def test_worktree_state_dir_is_never_inside_a_repo_path(tmp_path):
    fake_repo_root = tmp_path / "some-users-repo"
    fake_repo_root.mkdir()
    d = location.worktree_state_dir("repo-1", "wt-1", override=tmp_path / "state")
    assert fake_repo_root not in d.parents
    assert str(fake_repo_root) not in str(d)


def test_ensure_dirs_creates_blobs_and_tmp(tmp_path):
    root = tmp_path / "state"
    d = location.ensure_dirs("repo-1", "wt-1", override=root)
    assert (d / "blobs").is_dir()
    assert (d / "tmp").is_dir()
    assert d == location.worktree_state_dir("repo-1", "wt-1", override=root)


def test_db_path_and_blobs_dir_share_worktree_dir(tmp_path):
    root = tmp_path / "state"
    db_p = location.db_path("repo-1", "wt-1", override=root)
    blobs = location.blobs_dir("repo-1", "wt-1", override=root)
    assert db_p.parent == blobs.parent
