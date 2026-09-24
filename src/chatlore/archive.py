"""Taking a library elsewhere: one archive file, or Markdown to read anywhere.

An archive is a zip holding everything a library knows: its conversations, the
knowledge graph built from them, and the cached embeddings and model answers.
Importing it into another library, on another machine or next to other
conversations, restores the graph at once, with no model calls, and
``chatlore process`` then takes the embeddings from the cache instead of
computing them. The database itself is not copied: it is rebuilt from these
parts, which keeps the archive independent of the store backend.

Markdown export writes one readable file per conversation, for reading,
printing, or keeping in a notes app. It holds the branch each conversation
ended on, as ``Conversation.linear_messages`` returns it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from chatlore import __version__
from chatlore.embeddings import EmbeddingCache
from chatlore.extraction import ExtractionCache
from chatlore.library import AddOutcome, Library
from chatlore.models import Conversation, Role
from chatlore.store import Edge, GraphStore, Label, Node, open_store

FORMAT = "chatlore-archive"
FORMAT_VERSION = 1
MANIFEST = "manifest.json"
CONVERSATIONS = "conversations.jsonl"
GRAPH = "graph.jsonl"

# The caches an archive carries, with the tables merged on import. Only these
# tables are read, so an archive cannot bring anything else into a cache.
CACHES: dict[str, tuple[type[EmbeddingCache | ExtractionCache], tuple[str, ...]]] = {
    "embeddings.db": (EmbeddingCache, ("embeddings",)),
    "extractions.db": (ExtractionCache, ("extractions", "summaries", "verdicts", "reports")),
}

# What the graph adds on top of the conversations. Conversations and messages
# are not exported as nodes, since importing the conversations rebuilds them.
_DERIVED = (Label.CHUNK, Label.ENTITY, Label.TOPIC, Label.FACT)
_ALL = 1_000_000_000

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_NAME_LENGTH = 80
_ROLE_NAMES = {Role.USER: "You", Role.ASSISTANT: "Assistant", Role.SYSTEM: "System"}


class ArchiveError(Exception):
    """The file is not a ChatLore archive, or one this version cannot read."""


@dataclass(frozen=True, slots=True)
class ExportReport:
    """What went into an archive."""

    conversations: int
    nodes: int
    edges: int
    caches: tuple[str, ...]


@dataclass(slots=True)
class ImportReport:
    """What importing an archive did. A dry run only counts."""

    outcomes: Counter[str] = field(default_factory=Counter)
    messages: int = 0
    nodes: int = 0
    edges: int = 0


# -- archives ----------------------------------------------------------------


def export_archive(home: Path, target: Path) -> ExportReport:
    """Write the library at ``home`` into one archive at ``target``.

    The file is written next to ``target`` and moved into place at the end, so
    a failed export never leaves half an archive behind.
    """
    partial = target.with_name(target.name + ".partial")
    conversations = 0
    caches: list[str] = []
    try:
        with (
            open_store(home) as store,
            zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as archive,
        ):
            with archive.open(CONVERSATIONS, "w") as handle:
                for conversation in Library(home):
                    handle.write(_line(conversation.model_dump(mode="json")))
                    conversations += 1
            nodes, edges = _derived_graph(store)
            with archive.open(GRAPH, "w") as handle:
                for node in nodes:
                    handle.write(_line({"node": [node.id, node.label, node.props]}))
                for edge in edges:
                    handle.write(_line({"edge": [edge.src, edge.type, edge.dst, edge.props]}))
            for name in CACHES:
                path = home / "cache" / name
                if path.exists():
                    with tempfile.TemporaryDirectory() as folder:
                        copy = Path(folder) / name
                        _backup(path, copy)
                        archive.write(copy, f"cache/{name}")
                    caches.append(name)
            manifest = {
                "format": FORMAT,
                "version": FORMAT_VERSION,
                "chatlore": __version__,
                "created_at": datetime.now(UTC).isoformat(),
                "conversations": conversations,
                "nodes": len(nodes),
                "edges": len(edges),
                "caches": caches,
            }
            archive.writestr(MANIFEST, json.dumps(manifest, indent=2))
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return ExportReport(conversations, len(nodes), len(edges), tuple(caches))


def is_archive(path: Path) -> bool:
    """Whether ``path`` is a ChatLore archive rather than an export from a chat app."""
    try:
        read_manifest(path)
    except ArchiveError:
        return False
    return True


def read_manifest(path: Path) -> dict[str, Any]:
    """Return the archive's manifest, or raise ``ArchiveError``."""
    if not path.is_file() or not zipfile.is_zipfile(path):
        raise ArchiveError(f"{path} is not a ChatLore archive")
    with zipfile.ZipFile(path) as archive:
        try:
            manifest = json.loads(archive.read(MANIFEST))
        except (KeyError, ValueError):
            raise ArchiveError(f"{path} is not a ChatLore archive") from None
        complete = CONVERSATIONS in archive.namelist()
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise ArchiveError(f"{path} is not a ChatLore archive")
    version = manifest.get("version")
    if not isinstance(version, int) or version > FORMAT_VERSION:
        raise ArchiveError(
            f"{path} was written by a newer ChatLore ({manifest.get('chatlore')}); "
            "upgrade to import it"
        )
    if not complete:
        raise ArchiveError(f"{path} has no conversations; the archive is damaged")
    return manifest


def read_conversations(path: Path) -> Iterator[Conversation]:
    """The conversations in an archive, one at a time."""
    with zipfile.ZipFile(path) as archive, archive.open(CONVERSATIONS) as handle:
        for line in handle:
            if line.strip():
                yield Conversation.model_validate_json(line)


def read_graph(path: Path) -> tuple[list[Node], list[Edge]]:
    """The chunks, entities, topics, and edges in an archive."""
    nodes: list[Node] = []
    edges: list[Edge] = []
    with zipfile.ZipFile(path) as archive:
        if GRAPH not in archive.namelist():
            return nodes, edges
        with archive.open(GRAPH) as handle:
            for line in handle:
                if not line.strip():
                    continue
                record = json.loads(line)
                if "node" in record:
                    nodes.append(Node(*record["node"]))
                else:
                    edges.append(Edge(*record["edge"]))
    return nodes, edges


def import_archive(path: Path, home: Path, dry_run: bool = False) -> ImportReport:
    """Add an archive's conversations, graph, and caches to the library at ``home``.

    Conversations are added like any import, so importing twice changes nothing
    and a conversation already in the library is replaced by the archive's copy
    only when they differ. Nodes and edges are merged into the graph, and cache
    entries the library lacks are added to its caches.
    """
    read_manifest(path)
    report = ImportReport()
    if dry_run:
        for conversation in read_conversations(path):
            report.outcomes["parsed"] += 1
            report.messages += len(conversation.messages)
        nodes, edges = read_graph(path)
        report.nodes, report.edges = len(nodes), len(edges)
        return report

    with Library(home) as library, open_store(home) as store, store.transaction():
        for conversation in read_conversations(path):
            outcome = library.add(conversation)
            report.outcomes[outcome.value] += 1
            report.messages += len(conversation.messages)
            if outcome is not AddOutcome.UNCHANGED:
                store.upsert_conversation(conversation)
        report.nodes, report.edges = restore_graph(store, *read_graph(path))
    restore_caches(path, home)
    return report


def restore_graph(store: GraphStore, nodes: list[Node], edges: list[Edge]) -> tuple[int, int]:
    """Merge nodes and edges into ``store``. Returns how many of each were written.

    An edge whose other end is neither in the archive nor in the store, such as
    a chunk of a message the library no longer has, is left out.
    """
    known = {node.id for node in nodes}
    kept = [
        edge
        for edge in edges
        if all(end in known or store.get_node(end) is not None for end in (edge.src, edge.dst))
    ]
    with store.transaction():
        store.upsert_nodes(nodes)
        store.upsert_edges(kept)
    return len(nodes), len(kept)


def restore_caches(path: Path, home: Path) -> None:
    """Add the archive's cached embeddings and model answers to the library's caches."""
    with zipfile.ZipFile(path) as archive, tempfile.TemporaryDirectory() as folder:
        names = set(archive.namelist())
        for name, (cache, tables) in CACHES.items():
            member = f"cache/{name}"
            if member not in names:
                continue
            copy = Path(folder) / name
            with archive.open(member) as source, copy.open("wb") as target:
                shutil.copyfileobj(source, target)
            destination = home / "cache" / name
            cache(destination).close()  # creates the file and its tables if missing
            _merge(copy, destination, tables)


def _derived_graph(store: GraphStore) -> tuple[list[Node], list[Edge]]:
    nodes = [node for label in _DERIVED for node in store.find_nodes(label, limit=_ALL)]
    edges: dict[tuple[str, str, str], Edge] = {}
    for node in nodes:
        for edge, _ in store.neighbors(node.id, direction="both", limit=_ALL):
            edges[(edge.src, edge.type, edge.dst)] = edge
    return nodes, [edges[key] for key in sorted(edges)]


def _backup(source: Path, target: Path) -> None:
    """Copy a SQLite file consistently, even if something else has it open."""
    with (
        closing(sqlite3.connect(source)) as reading,
        closing(sqlite3.connect(target)) as writing,
    ):
        reading.backup(writing)


def _merge(source: Path, target: Path, tables: Iterable[str]) -> None:
    """Copy the rows of ``tables`` that ``target`` lacks from ``source``."""
    with closing(sqlite3.connect(target)) as db:
        db.execute("ATTACH DATABASE ? AS archived", (str(source),))
        present = {
            row[0]
            for row in db.execute("SELECT name FROM archived.sqlite_master WHERE type = 'table'")
        }
        for table in tables:
            if table not in present:
                continue
            columns = ", ".join(row[1] for row in db.execute(f"PRAGMA main.table_info({table})"))
            db.execute(
                f"INSERT OR IGNORE INTO main.{table} ({columns}) "
                f"SELECT {columns} FROM archived.{table}"
            )
        db.commit()
        db.execute("DETACH DATABASE archived")


def _line(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


# -- Markdown ----------------------------------------------------------------


def export_markdown(conversations: Iterable[Conversation], folder: Path) -> int:
    """Write each conversation as a Markdown file under ``folder/<source>/``.

    Files are named by date and title, so exporting again overwrites the same
    files. Returns how many were written.
    """
    taken: set[Path] = set()
    written = 0
    for conversation in conversations:
        path = folder / conversation.source.value / f"{_file_stem(conversation)}.md"
        if path in taken:
            path = path.with_name(f"{path.stem} {_UNSAFE.sub('-', conversation.id[-8:])}.md")
        taken.add(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(conversation_markdown(conversation), encoding="utf-8", newline="\n")
        written += 1
    return written


def conversation_markdown(conversation: Conversation) -> str:
    """One conversation as Markdown: front matter, a title, then each message."""
    front = {
        "title": conversation.title or "(untitled)",
        "source": conversation.source.value,
        "id": conversation.id,
        "created": _stamp(conversation.created_at),
        "updated": _stamp(conversation.updated_at),
        "model": conversation.model,
    }
    header = yaml.safe_dump(
        {key: value for key, value in front.items() if value is not None},
        sort_keys=False,
        allow_unicode=True,
        width=1_000,
    )
    parts = [f"---\n{header}---\n", f"# {conversation.title or '(untitled)'}\n"]
    for message in conversation.linear_messages():
        text = message.text.strip()
        if not text:
            continue
        who = _ROLE_NAMES.get(message.role, message.role.value.title())
        when = f" · {_stamp(message.created_at)}" if message.created_at else ""
        parts.append(f"## {who}{when}\n\n{text}\n")
    return "\n".join(parts)


def _file_stem(conversation: Conversation) -> str:
    title = _UNSAFE.sub("-", conversation.title or "untitled")
    title = " ".join(title.split())[:_NAME_LENGTH].rstrip(" .") or "untitled"
    when = conversation.created_at or conversation.updated_at
    return f"{when:%Y-%m-%d} {title}" if when else title


def _stamp(value: datetime | None) -> str | None:
    return f"{value:%Y-%m-%d %H:%M} UTC" if value else None
