"""XDG locations for persistent CSLR configuration.

Config is machine-local and MUST NOT live inside a target Git repository
or the CSLR checkout. Tests isolate this via `$CODESLAYER_CONFIG` and
`$XDG_CONFIG_HOME`, matching `store.location`'s state-root override.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_CONFIG_PATH = "CODESLAYER_CONFIG"


def default_config_dir(*, override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "codeslayer"
    return Path.home() / ".config" / "codeslayer"


def config_path(*, override: str | Path | None = None) -> Path:
    if override is not None:
        return Path(override)
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        return Path(env)
    return default_config_dir() / "config.toml"


def managed_service_dir(*, state_root: Path | None = None) -> Path:
    if state_root is not None:
        return Path(state_root) / "service"
    from code_slayer.store.location import state_root as resolve_state_root

    return resolve_state_root() / "service"


def managed_venv_dir(*, state_root: Path | None = None) -> Path:
    return managed_service_dir(state_root=state_root) / "venv"


def user_systemd_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"
