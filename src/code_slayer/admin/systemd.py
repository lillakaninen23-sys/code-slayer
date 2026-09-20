"""Generate the Code Slayer systemd user unit. No subprocess here."""

from __future__ import annotations

from pathlib import Path

UNIT_NAME = "codeslayer.service"
RESTART_SEC = 5


def render_user_unit(
    *,
    python_or_codeslayer: Path,
    checkout: Path,
    webui_dir: Path,
) -> str:
    exec_start = python_or_codeslayer
    exec_checkout = _exec_arg(checkout)
    exec_webui = _exec_arg(webui_dir)
    if exec_start.name == "python" or exec_start.name.startswith("python"):
        start = (
            f"{_exec_arg(exec_start)} -m code_slayer.cli.main serve "
            f"--repo {exec_checkout} --webui-dir {exec_webui}"
        )
    else:
        start = (
            f"{_exec_arg(exec_start)} serve --repo {exec_checkout} "
            f"--webui-dir {exec_webui}"
        )
    return (
        "[Unit]\n"
        "Description=Code Slayer local application service\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={_working_directory(checkout)}\n"
        f"ExecStart={start}\n"
        "Restart=on-failure\n"
        f"RestartSec={RESTART_SEC}\n"
        "# Persistent config is read from the XDG config path at runtime.\n"
        "# Do not inject CODESLAYER_CERT_* here.\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _working_directory(path: Path) -> str:
    """Serialize WorkingDirectory=. This is a path setting, not ExecStart.

    systemd includes surrounding quotes in the path and then rejects it as
    not absolute (``WorkingDirectory= path is not absolute: "/..."``).
    Specifiers are expanded, so ``%`` is escaped as ``%%``. Whitespace is
    literal because the remainder of the line is the value.
    """
    return _unit_path_text(path).replace("%", "%%")


def _exec_arg(path: Path) -> str:
    """C-style-quote one ExecStart word. Specifiers are expanded first."""
    text = _unit_path_text(path).replace("%", "%%")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unit_path_text(path: Path) -> str:
    text = str(path)
    if not path.is_absolute() or not text.startswith("/"):
        raise ValueError("systemd unit path must be absolute")
    if any(ch in text for ch in "\n\r\x00"):
        raise ValueError("systemd unit path contains control characters")
    return text


def unit_path(directory: Path) -> Path:
    return directory / UNIT_NAME
