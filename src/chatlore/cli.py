"""Command-line interface for ChatLore."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from chatlore import __version__

app = typer.Typer(
    name="chatlore",
    help="All your AI conversations, one graph, one chat.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def default_home() -> Path:
    """Return the ChatLore data directory.

    Uses ``$CHATLORE_HOME`` when set, otherwise ``~/.chatlore``.
    """
    override = os.environ.get("CHATLORE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".chatlore"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"chatlore {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """ChatLore: a local-first graph knowledge base built from your AI conversations."""


@app.command()
def doctor() -> None:
    """Report the local environment ChatLore will run in."""
    home = default_home()
    state = "exists" if home.exists() else "not created yet"

    table = Table(title=f"chatlore {__version__}", show_header=False)
    table.add_column("key", style="bold")
    table.add_column("value", overflow="fold")
    table.add_row("python", f"{platform.python_version()} ({sys.executable})")
    table.add_row("platform", platform.platform())
    table.add_row("data dir", f"{home} ({state})")
    console.print(table)
