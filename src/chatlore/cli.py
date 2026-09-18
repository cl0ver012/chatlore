"""Command-line interface for ChatLore."""

from __future__ import annotations

import platform
import re
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
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
from chatlore.store import DATABASE_NAME, GraphStore, TextHit, open_store

__all__ = ["app", "default_home", "display_path"]

_MAX_ISSUES_SHOWN = 10

app = typer.Typer(
    name="chatlore",
    help="All your AI conversations, one graph, one chat.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def display_path(path: Path) -> str:
    """Render a path with the home directory shortened to ``~``."""
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


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
    table.add_row("data dir", f"{display_path(home)} ({state})")
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
        with Library(default_home()) as library, _store(dry_run) as store, _writes(store):
            for conversation in importer.parse(path, issues.append):
                messages += len(conversation.messages)
                if dry_run:
                    outcomes["parsed"] += 1
                    continue
                outcome = library.add(conversation)
                outcomes[outcome.value] += 1
                if outcome is not AddOutcome.UNCHANGED and store is not None:
                    store.upsert_conversation(conversation)
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

    _print_issues(issues)
    if not dry_run:
        console.print(f"Library: {display_path(default_home())}")


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
    with Library(default_home()) as library:
        library.add(conversation)
    with open_store(default_home()) as store:
        store.upsert_conversation(conversation)
    console.print(f"Saved note: {conversation.title}")


@app.command()
def stats() -> None:
    """Show what the local library contains."""
    totals = Library(default_home()).stats()
    if not totals:
        console.print("The library is empty. Run `chatlore import <path>` to add an export.")
        return

    table = Table(title="Library")
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
    console.print(f"Library: {display_path(default_home())}")


@app.command()
def index(
    rebuild: Annotated[
        bool,
        typer.Option("--rebuild", help="Drop the database and rebuild it from the library."),
    ] = False,
) -> None:
    """Bring the search database in line with the library."""
    home = default_home()
    if rebuild:
        for name in (DATABASE_NAME, f"{DATABASE_NAME}-wal", f"{DATABASE_NAME}-shm"):
            (home / name).unlink(missing_ok=True)
    count = 0
    library = Library(home)
    if rebuild:
        library.rebuild_index()
    with open_store(home) as store:
        with store.transaction():
            for conversation in library:
                store.upsert_conversation(conversation)
                count += 1
        total = store.count_nodes("Conversation")
    console.print(f"Indexed {count} conversations. Database holds {total}.")


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Words to look for. All of them must match.")],
    source: Annotated[
        list[str] | None,
        typer.Option("--source", "-s", help="Limit to a source. Repeatable."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many results.")] = 10,
) -> None:
    """Full-text search over every imported message."""
    with open_store(default_home()) as store:
        hits = store.search_text(query, limit=limit, sources=source)
        if not hits:
            console.print("No matches. Is the library imported and indexed?")
            return
        for hit in hits:
            _print_hit(hit)


def _print_issues(issues: list[ImportIssue]) -> None:
    """List skipped records, grouping identical reasons so 200 blanks take one line."""
    grouped: dict[str, list[str]] = {}
    for issue in issues:
        grouped.setdefault(_generic_reason(issue.reason), []).append(issue.record)
    for reason, records in list(grouped.items())[:_MAX_ISSUES_SHOWN]:
        if len(records) == 1:
            console.print(f"  [yellow]skipped[/yellow] {escape(records[0])}: {escape(reason)}")
        else:
            console.print(f"  [yellow]skipped[/yellow] {len(records)} records: {escape(reason)}")
    if len(grouped) > _MAX_ISSUES_SHOWN:
        console.print(f"  ... and {len(grouped) - _MAX_ISSUES_SHOWN} more kinds of problem")


def _generic_reason(reason: str) -> str:
    """Replace numbers so reasons that differ only by a count group together."""
    return re.sub(r"\d+", "N", reason)


def _print_hit(hit: TextHit) -> None:
    title = escape(hit.title or "(untitled)")
    console.print(f"[bold]{title}[/bold]  [dim]{hit.source} | {hit.conversation_id}[/dim]")
    console.print(f"  {hit.snippet}", markup=False, highlight=False)


@contextmanager
def _writes(store: GraphStore | None) -> Iterator[None]:
    """One transaction for a whole import; a no-op without a store."""
    if store is None:
        yield
        return
    with store.transaction():
        yield


@contextmanager
def _store(dry_run: bool) -> Iterator[GraphStore | None]:
    if dry_run:
        yield None
        return
    with open_store(default_home()) as store:
        yield store
