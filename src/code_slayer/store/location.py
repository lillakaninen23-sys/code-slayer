"""Resolves Code Slayer's external durable-state location.

Foundation Plan §03/§17, INV-9: durable state never lives inside a target
repository's working tree, so a destructive repo operation (`git clean
-fdx`, `rm -rf`, ...) cannot destroy Code Slayer's own record of what it
did. This module resolves the on-disk directory for a given
`(repo_id, worktree_id)` pair, honoring `$XDG_DATA_HOME`, with an explicit
override so tests never touch a real user path.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_STATE_ROOT_OVERRIDE = "CODESLAYER_STATE_ROOT"


def xdg_data_home() -> Path:
    """`$XDG_DATA_HOME`, or its default, `~/.local/share`."""
    override = os.environ.get("XDG_DATA_HOME")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share"


def state_root(*, override: str | Path | None = None) -> Path:
    """Code Slayer's top-level state root.

    Resolution order: an explicit `override` argument, then
    `$CODESLAYER_STATE_ROOT` (this is what tests set, so no test ever
    resolves to a real user path), then `$XDG_DATA_HOME/codeslayer`.
    """
    if override is not None:
        return Path(override)
    env_override = os.environ.get(ENV_STATE_ROOT_OVERRIDE)
    if env_override:
        return Path(env_override)
    return xdg_data_home() / "codeslayer"


def worktree_state_dir(
    repo_id: str, worktree_id: str, *, override: str | Path | None = None
) -> Path:
    """The durable state directory for one (repo, worktree) pair."""
    return state_root(override=override) / "repos" / repo_id / "worktrees" / worktree_id


def db_path(repo_id: str, worktree_id: str, *, override: str | Path | None = None) -> Path:
    return worktree_state_dir(repo_id, worktree_id, override=override) / "state.db"


def blobs_dir(repo_id: str, worktree_id: str, *, override: str | Path | None = None) -> Path:
    return worktree_state_dir(repo_id, worktree_id, override=override) / "blobs"


def tmp_dir(repo_id: str, worktree_id: str, *, override: str | Path | None = None) -> Path:
    return worktree_state_dir(repo_id, worktree_id, override=override) / "tmp"


def validation_certification_state_dir(
    repo_id: str, worktree_id: str, *, override: str | Path | None = None,
) -> Path:
    """Isolated, non-production state for Certification Center v1.

    Never the production worktree directory (`repos/<repo>/worktrees/<id>`).
    Validation certificates and evidence live only here.
    """
    return state_root(override=override) / "validation-certification" / repo_id / worktree_id


def validation_certification_db_path(
    repo_id: str, worktree_id: str, *, override: str | Path | None = None,
) -> Path:
    return validation_certification_state_dir(
        repo_id, worktree_id, override=override,
    ) / "state.db"


def validation_certification_blobs_dir(
    repo_id: str, worktree_id: str, *, override: str | Path | None = None,
) -> Path:
    return validation_certification_state_dir(
        repo_id, worktree_id, override=override,
    ) / "blobs"


def ensure_validation_certification_dirs(
    repo_id: str, worktree_id: str, *, override: str | Path | None = None,
) -> Path:
    directory = validation_certification_state_dir(
        repo_id, worktree_id, override=override,
    )
    (directory / "blobs").mkdir(parents=True, exist_ok=True)
    (directory / "tmp").mkdir(parents=True, exist_ok=True)
    return directory


def ensure_dirs(
    repo_id: str, worktree_id: str, *, override: str | Path | None = None
) -> Path:
    """Create the worktree state dir and its `blobs`/`tmp` subdirectories
    if they don't exist yet. Returns the worktree state dir."""
    directory = worktree_state_dir(repo_id, worktree_id, override=override)
    (directory / "blobs").mkdir(parents=True, exist_ok=True)
    (directory / "tmp").mkdir(parents=True, exist_ok=True)
    return directory
