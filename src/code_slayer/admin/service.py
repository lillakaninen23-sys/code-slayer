"""Install and control the systemd --user Code Slayer service."""

from __future__ import annotations

import os
import venv
from dataclasses import dataclass
from pathlib import Path

from code_slayer import __version__
from code_slayer.admin.process import ProcessError, ProcessResult, run_fixed
from code_slayer.admin.systemd import UNIT_NAME, render_user_unit, unit_path
from code_slayer.config.paths import managed_venv_dir, user_systemd_dir
from code_slayer.config.store import load_config, save_config

SYSTEMCTL = "systemctl"
CODESLAYER_UNIT = UNIT_NAME


@dataclass(frozen=True)
class ServiceStatus:
    state: str
    active: bool
    unit: str
    detail: str
    source: str


def default_checkout() -> Path:
    return Path(__file__).resolve().parents[3]


def default_webui(checkout: Path) -> Path:
    return checkout / "webui"


def install_service(
    *,
    checkout: Path | None = None,
    config_path: Path | None = None,
    state_root: Path | None = None,
    python: Path | None = None,
    runner=None,
    venv_create=None,
) -> dict:
    """Create managed venv, install this checkout, write unit, enable+start.

    Does not depend on exported CODESLAYER_CERT_* variables. Existing
    state.db / evidence / certificates are not moved or deleted.

    `systemctl --user daemon-reload` and `enable --now` are fail-closed:
    a non-zero status aborts installation. Linger is best-effort and is
    never labelled VERIFIED when it did not succeed.
    """
    run = runner or run_fixed
    create = venv_create or venv.create
    root = (checkout or default_checkout()).resolve()
    if not (root / "pyproject.toml").is_file():
        raise ProcessError("checkout_not_codeslayer")
    webui = default_webui(root)
    if not (webui / "index.html").is_file() or not (webui / "static").is_dir():
        raise ProcessError("webui_missing")
    venv_dir = managed_venv_dir(state_root=state_root)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    if not (venv_dir / "bin" / "python").exists():
        create(venv_dir, with_pip=True, clear=False)
    py = python or (venv_dir / "bin" / "python")
    pip_install = run(
        (str(py), "-m", "pip", "install", "-e", str(root)),
        timeout=300.0,
    )
    if pip_install.returncode != 0:
        raise ProcessError("pip_install_failed", pip_install.stderr[-500:])
    codeslayer_bin = venv_dir / "bin" / "codeslayer"
    if not codeslayer_bin.exists():
        raise ProcessError("codeslayer_entrypoint_missing")
    config = load_config(path=config_path)
    config = config.with_checkout(str(root), str(webui))
    saved = save_config(config, path=config_path)
    systemd_dir = user_systemd_dir()
    systemd_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_path(systemd_dir)
    unit.write_text(
        render_user_unit(
            python_or_codeslayer=codeslayer_bin, checkout=root, webui_dir=webui,
        ),
        encoding="utf-8",
    )
    reload_r = run((SYSTEMCTL, "--user", "daemon-reload"))
    if reload_r.returncode != 0:
        raise ProcessError(
            "systemctl_daemon_reload_failed",
            reload_r.stderr.strip() or reload_r.stdout.strip(),
        )
    enable_r = run((SYSTEMCTL, "--user", "enable", "--now", CODESLAYER_UNIT))
    if enable_r.returncode != 0:
        raise ProcessError(
            "systemctl_enable_failed",
            enable_r.stderr.strip() or enable_r.stdout.strip(),
        )
    linger = _enable_linger(run)
    return {
        "checkout": str(root),
        "webui_dir": str(webui),
        "venv": str(venv_dir),
        "config_path": str(saved),
        "unit_path": str(unit),
        "systemd_reload": _result_view(reload_r),
        "enable_start": _result_view(enable_r),
        "linger": linger,
        "version": __version__,
    }


def _enable_linger(run) -> dict:
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not user:
        return {
            "attempted": False,
            "ok": False,
            "source": "UNVERIFIED",
            "detail": "linger_user_unknown",
        }
    try:
        result = run(("loginctl", "enable-linger", user), timeout=10.0)
    except ProcessError as exc:
        return {
            "attempted": True,
            "ok": False,
            "source": "UNVERIFIED",
            "detail": exc.code,
        }
    if result.returncode != 0:
        return {
            "attempted": True,
            "ok": False,
            "source": "UNVERIFIED",
            "detail": "linger_failed",
        }
    return {
        "attempted": True,
        "ok": True,
        "source": "VERIFIED",
        "detail": "linger_enabled",
    }


def service_status(*, runner=None) -> ServiceStatus:
    run = runner or run_fixed
    try:
        result = run((SYSTEMCTL, "--user", "is-active", CODESLAYER_UNIT))
    except ProcessError as exc:
        return ServiceStatus(
            state="UNVERIFIED", active=False, unit=CODESLAYER_UNIT,
            detail=exc.code, source="systemctl",
        )
    text = (result.stdout or "").strip() or "unknown"
    active = result.returncode == 0 and text == "active"
    state = "running" if active else text
    return ServiceStatus(
        state=state, active=active, unit=CODESLAYER_UNIT,
        detail=text, source="systemctl",
    )


def start_service(*, runner=None) -> ProcessResult:
    run = runner or run_fixed
    return _require_ok(run((SYSTEMCTL, "--user", "start", CODESLAYER_UNIT)))


def stop_service(*, runner=None) -> ProcessResult:
    run = runner or run_fixed
    return _require_ok(run((SYSTEMCTL, "--user", "stop", CODESLAYER_UNIT)))


def restart_service(*, runner=None) -> ProcessResult:
    run = runner or run_fixed
    return _require_ok(run((SYSTEMCTL, "--user", "restart", CODESLAYER_UNIT)))


def _require_ok(result: ProcessResult) -> ProcessResult:
    if result.returncode != 0:
        raise ProcessError("systemctl_failed", result.stderr.strip() or result.stdout.strip())
    return result


def _result_view(result: ProcessResult) -> dict:
    return {
        "returncode": result.returncode,
        "ok": result.returncode == 0,
    }
