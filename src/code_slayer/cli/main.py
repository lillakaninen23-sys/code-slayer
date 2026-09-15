"""Code Slayer CLI: repository identity inspection and local application API.

Identity is established through the existing backend when needed. The HTTP
server is explicitly launched; no background service is installed.
"""

from __future__ import annotations

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


@cli.command()
@click.option("--repo", default=".", type=click.Path(exists=True, file_okay=False))
@click.option("--webui-dir", type=click.Path(exists=True, file_okay=False))
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8765, type=click.IntRange(1, 65535), show_default=True)
@click.option(
    "--trusted-host", multiple=True, help="Explicit additional HTTP Host allowlist entry."
)
@click.option(
    "--runtime-factory", help="Trusted installed module:function returning RuntimeBindings."
)
def serve(repo, webui_dir, host, port, trusted_host, runtime_factory):
    """Serve the local application API and optionally the existing WebUI sidecar."""
    from importlib import import_module

    from waitress import serve as wsgi_serve

    from code_slayer.api import create_app
    from code_slayer.api.service import RuntimeBindings

    bindings = None
    if runtime_factory:
        module, separator, name = runtime_factory.partition(":")
        if not separator:
            raise click.ClickException("runtime-factory must be module:function")
        bindings = getattr(import_module(module), name)()
        if not isinstance(bindings, RuntimeBindings):
            raise click.ClickException("runtime-factory must return RuntimeBindings")
    app = create_app(
        repo,
        webui_dir=webui_dir,
        bindings=bindings,
        trusted_hosts=("127.0.0.1", "localhost", "[::1]", *trusted_host),
    )
    click.echo(f"Code Slayer API: http://{host}:{port}")
    wsgi_serve(app, host=host, port=port, threads=4, max_request_body_size=65536)


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
