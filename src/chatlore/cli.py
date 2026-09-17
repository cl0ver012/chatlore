"""Command-line interface for ChatLore."""

from __future__ import annotations

import platform
import sys
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from chatlore import __version__
from chatlore.importers import (
    ImporterError,
    ImportIssue,
    detect_source,
    get_importer,
    make_note,
)
from chatlore.library import AddOutcome, Library
from chatlore.paths import default_home

__all__ = ["app", "default_home"]

_MAX_ISSUES_SHOWN = 10

app = typer.Typer(
    name="chatlore",
    help="All your AI conversations, one graph, one chat.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


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


@app.command("import")
def import_(
    path: Annotated[
        Path,
        typer.Argument(help="Export zip, extracted folder, JSON file, or Markdown folder."),
    ],
    source: Annotated[
        str,
        typer.Option(
            "--source",
            "-s",
            help="chatgpt, claude, gemini, markdown, or auto to detect it from the content.",
        ),
    ] = "auto",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Parse and report without writing anything."),
    ] = False,
) -> None:
    """Import an export into the local library. Safe to run repeatedly."""
    issues: list[ImportIssue] = []
    outcomes: Counter[str] = Counter()
    messages = 0

    try:
        kind = detect_source(path) if source == "auto" else get_importer(source).kind
        importer = get_importer(kind.value)
        library = Library(default_home())
        for conversation in importer.parse(path, issues.append):
            messages += len(conversation.messages)
            if dry_run:
                outcomes["parsed"] += 1
            else:
                outcomes[library.add(conversation).value] += 1
    except ImporterError as error:
        console.print(f"[red]Import failed:[/red] {error}")
        raise typer.Exit(code=1) from error

    title = f"{kind.value} import" + (" (dry run)" if dry_run else "")
    table = Table(title=title, show_header=False)
    table.add_column("key", style="bold")
    table.add_column("value", justify="right")
    if dry_run:
        table.add_row("conversations parsed", str(outcomes["parsed"]))
    else:
        for outcome in AddOutcome:
            table.add_row(outcome.value, str(outcomes[outcome.value]))
    table.add_row("messages", str(messages))
    table.add_row("skipped records", str(len(issues)))
    console.print(table)

    for issue in issues[:_MAX_ISSUES_SHOWN]:
        console.print(f"  [yellow]skipped[/yellow] {issue.record}: {issue.reason}")
    if len(issues) > _MAX_ISSUES_SHOWN:
        console.print(f"  ... and {len(issues) - _MAX_ISSUES_SHOWN} more")
    if not dry_run:
        console.print(f"Library: {default_home()}")


@app.command()
def note(
    text: Annotated[str, typer.Argument(help="The note to save.")],
    title: Annotated[str | None, typer.Option("--title", "-t", help="Optional title.")] = None,
) -> None:
    """Save a manual note to the library."""
    try:
        conversation = make_note(text, title=title)
    except ValueError as error:
        console.print(f"[red]{error}[/red]")
        raise typer.Exit(code=1) from error
    Library(default_home()).add(conversation)
    console.print(f"Saved note: {conversation.title}")


@app.command()
def stats() -> None:
    """Show what the local library contains."""
    totals = Library(default_home()).stats()
    if not totals:
        console.print("The library is empty. Run `chatlore import <path>` to add an export.")
        return

    table = Table(title=f"Library at {default_home()}")
    table.add_column("source", style="bold")
    table.add_column("conversations", justify="right")
    table.add_column("messages", justify="right")
    for source_name, total in totals.items():
        table.add_row(source_name, str(total.conversations), str(total.messages))
    table.add_section()
    table.add_row(
        "total",
        str(sum(t.conversations for t in totals.values())),
        str(sum(t.messages for t in totals.values())),
    )
    console.print(table)
