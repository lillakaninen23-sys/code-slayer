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
    quoted_checkout = _quote(checkout)
    quoted_webui = _quote(webui_dir)
    if exec_start.name == "python" or exec_start.name.startswith("python"):
        start = (
            f"{_quote(exec_start)} -m code_slayer.cli.main serve "
            f"--repo {quoted_checkout} --webui-dir {quoted_webui}"
        )
    else:
        start = (
            f"{_quote(exec_start)} serve --repo {quoted_checkout} "
            f"--webui-dir {quoted_webui}"
        )
    return (
        "[Unit]\n"
        "Description=Code Slayer local application service\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={_quote(checkout)}\n"
        f"ExecStart={start}\n"
        "Restart=on-failure\n"
        f"RestartSec={RESTART_SEC}\n"
        "# Persistent config is read from the XDG config path at runtime.\n"
        "# Do not inject CODESLAYER_CERT_* here.\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _quote(path: Path) -> str:
    text = str(path)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def unit_path(directory: Path) -> Path:
    return directory / UNIT_NAME
