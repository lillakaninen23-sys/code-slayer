"""Minimal `codeslayer` CLI entry point (Phase 1).

Only `inspect` exists so far — a read-only command that resolves and
prints repo/worktree identity. It never mutates `.git/` unless identity
hasn't been established yet, in which case it establishes it (the same
behavior `identity.resolve(create=True)` documents).
"""

from __future__ import annotations

import click

from code_slayer import __version__
from code_slayer.repo import identity


@click.group()
@click.version_option(__version__, prog_name="codeslayer")
def cli() -> None:
    """Code Slayer: a local, persistent, repo-level software-engineering
    agent system. Phase 1 build — foundation only."""


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


if __name__ == "__main__":
    cli()
