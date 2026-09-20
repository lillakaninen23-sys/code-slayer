"""Code Slayer CLI: inspect, serve, and local service administration.

Ordinary administration happens in the WebUI after `install-service`.
These commands exist for first-time install and emergency recovery.
"""

from __future__ import annotations

from pathlib import Path

import click

from code_slayer import __version__
from code_slayer.repo import identity


@click.group()
@click.version_option(__version__, prog_name="codeslayer")
def cli() -> None:
    """Code Slayer: a local, persistent, repo-level software-engineering
    agent system."""


@cli.command()
@click.argument(
    "path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=str),
    default=".",
)
def inspect(path: str) -> None:
    """Resolve and print repo/worktree identity for PATH (default: .)."""
    try:
        info = identity.resolve(path)
    except identity.NotAGitRepositoryError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"repo_root:       {info.repo_root}")
    click.echo(f"repo_id:         {info.repo_id}")
    click.echo(f"worktree_id:     {info.worktree_id}")
    click.echo(f"git_common_dir:  {info.git_common_dir}")
    click.echo(f"git_dir:         {info.git_dir}")


def _echo_process_error(exc) -> None:
    raise click.ClickException(f"{exc.code}: {exc}") from exc


@cli.command("install-service")
@click.option(
    "--checkout",
    default=None,
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=str),
    help="CSLR checkout to install (default: this source tree).",
)
def install_service_cmd(checkout: str | None) -> None:
    """Create the managed venv, write the user systemd unit, enable and start.

    Does not require exported CODESLAYER_CERT_* variables. Existing
    state.db, evidence, and certificates are left in place.
    """
    from code_slayer.admin.process import ProcessError
    from code_slayer.admin.service import default_checkout, install_service

    root = Path(checkout).resolve() if checkout else default_checkout()
    try:
        result = install_service(checkout=root)
    except ProcessError as exc:
        _echo_process_error(exc)
    click.echo("installed")
    for key in ("checkout", "venv", "config_path", "unit_path", "version"):
        click.echo(f"{key}: {result[key]}")
    linger = result.get("linger") or {}
    click.echo(
        f"linger: {linger.get('source', 'UNVERIFIED')} ({linger.get('detail', '')})"
    )
    click.echo("local_url: http://127.0.0.1:8765")
    click.echo("Ordinary administration is in the WebUI. Terminal is for recovery.")


@cli.command("status")
def status_cmd() -> None:
    """Print systemd --user codeslayer.service status."""
    from code_slayer.admin.service import service_status

    info = service_status()
    click.echo(f"unit:    {info.unit}")
    click.echo(f"state:   {info.state}")
    click.echo(f"running: {info.active}")
    click.echo(f"source:  {info.source}")


@cli.command("start")
def start_cmd() -> None:
    """Start codeslayer.service (systemd --user)."""
    from code_slayer.admin.process import ProcessError
    from code_slayer.admin.service import start_service

    try:
        start_service()
    except ProcessError as exc:
        _echo_process_error(exc)
    click.echo("started")


@cli.command("stop")
def stop_cmd() -> None:
    """Stop codeslayer.service (systemd --user)."""
    from code_slayer.admin.process import ProcessError
    from code_slayer.admin.service import stop_service

    try:
        stop_service()
    except ProcessError as exc:
        _echo_process_error(exc)
    click.echo("stopped")


@cli.command("restart")
def restart_cmd() -> None:
    """Restart codeslayer.service (systemd --user)."""
    from code_slayer.admin.process import ProcessError
    from code_slayer.admin.service import restart_service

    try:
        restart_service()
    except ProcessError as exc:
        _echo_process_error(exc)
    click.echo("restarted")


@cli.command()
@click.option("--repo", default=".", type=click.Path(exists=True, file_okay=False))
@click.option("--webui-dir", type=click.Path(exists=True, file_okay=False))
@click.option("--host", default=None)
@click.option("--port", default=None, type=click.IntRange(1, 65535))
@click.option(
    "--trusted-host", multiple=True, help="Explicit additional HTTP Host allowlist entry."
)
@click.option(
    "--runtime-factory", help="Trusted installed module:function returning RuntimeBindings."
)
def serve(repo, webui_dir, host, port, trusted_host, runtime_factory):
    """Serve the local application API and the WebUI sidecar.

    Persistent config (XDG `~/.config/codeslayer/config.toml`) is the
    normal source of workers, Ollama origins, and bind address. A
    runtime-factory is optional emergency wiring, not required for
    Certification Center or runtime identity.
    """
    from importlib import import_module

    from waitress import serve as wsgi_serve

    from code_slayer.admin.hosts import LOOPBACK_TRUSTED_HOSTS, exact_static_hosts
    from code_slayer.api import create_app
    from code_slayer.api.service import RuntimeBindings
    from code_slayer.config.schema import LOOPBACK_HOSTS
    from code_slayer.config.store import load_config

    cfg = load_config()
    checkout = Path(repo).resolve()
    if repo == "." and cfg.server.checkout:
        checkout = Path(cfg.server.checkout)
    bind_host = host or cfg.server.host
    bind_port = port if port is not None else cfg.server.port
    if bind_host not in LOOPBACK_HOSTS:
        raise click.ClickException("server host must be a loopback address")
    resolved_webui = webui_dir or cfg.server.webui_dir
    if resolved_webui is None:
        candidate = checkout / "webui"
        if (candidate / "index.html").is_file() and (candidate / "static").is_dir():
            resolved_webui = str(candidate)
    bindings = None
    if runtime_factory:
        module, separator, name = runtime_factory.partition(":")
        if not separator:
            raise click.ClickException("runtime-factory must be module:function")
        bindings = getattr(import_module(module), name)()
        if not isinstance(bindings, RuntimeBindings):
            raise click.ClickException("runtime-factory must return RuntimeBindings")
    try:
        trusted = exact_static_hosts((*LOOPBACK_TRUSTED_HOSTS, *trusted_host))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    app = create_app(
        checkout,
        webui_dir=resolved_webui,
        bindings=bindings,
        trusted_hosts=trusted,
        load_persistent_config=True,
    )
    click.echo(f"Code Slayer API: http://{bind_host}:{bind_port}")
    wsgi_serve(app, host=bind_host, port=bind_port, threads=4, max_request_body_size=65536)


@cli.command("permission-request-demo")
@click.option("--repo", default=".", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--permission-key", default="network.discovery.local", show_default=True,
    help="Must name a registered code-owned permission definition.",
)
@click.option("--semantic-version", default="1", show_default=True)
@click.option(
    "--purpose", default="Manual acceptance testing of the consent UI.", show_default=True,
)
def permission_request_demo(repo, permission_key, semantic_version, purpose):
    """DIAGNOSTIC / DEVELOPER-ONLY. Durably creates one real PENDING
    permission request so the WebUI consent flow can be exercised by
    hand.

    This is never called by production feature code and grants no
    authority by itself -- it only creates a request a human must still
    explicitly ALLOW or DENY through the WebUI/API, exactly like any
    other permission request. There is no equivalent HTTP endpoint: the
    browser can never mint a permission request (see
    `docs/PERMISSIONS_MODEL.md`); this command exists solely because a
    trusted, explicitly-invoked, developer-only CLI action is the
    sanctioned way to demonstrate the consent flow without fabricating
    production data or adding a public request-minting endpoint.
    """
    from code_slayer.permissions.service import PermissionEngineError, PermissionService

    service = PermissionService(repo)
    try:
        record = service.request(
            permission_key=permission_key, semantic_version=semantic_version, resource=None,
            purpose=purpose, requesting_subsystem="cli-diagnostic",
        )
    except PermissionEngineError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        service.close()
    click.echo(f"request_id:  {record.request_id}")
    click.echo(f"permission:  {record.permission_key}:{record.semantic_version}")
    click.echo(f"state:       {record.state}")
    click.echo("Open the WebUI's Privacy & Security view to Allow or Deny it.")


if __name__ == "__main__":
    cli()
