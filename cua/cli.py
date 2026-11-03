"""Typer CLI entry point `cua`. Commands for discover/replay/console are added in later steps."""

import typer

from cua import __version__

app = typer.Typer(help="Computer-use automation: discover, save and replay capabilities.",
                  no_args_is_help=True)


@app.callback()
def main() -> None:
    """Computer-use automation CLI."""


@app.command()
def version() -> None:
    """Print the cua package version."""
    typer.echo(__version__)


if __name__ == "__main__":
    app()
