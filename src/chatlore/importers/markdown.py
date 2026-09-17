"""Importer for Markdown folders such as Obsidian vaults, and single text files.

Each file becomes one conversation holding a single message, so notes flow
through the same storage, search, and extraction path as chats. The file's
path relative to the imported folder is its identity, which keeps re-imports
idempotent when the vault is moved or copied.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from chatlore.ids import conversation_id, message_id
from chatlore.importers.base import ImporterError, IssueSink, report
from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind

_EXTENSIONS = frozenset({".md", ".markdown", ".txt"})
_SKIPPED_DIRS = frozenset({".obsidian", ".git", ".trash", "node_modules", ".venv"})
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n(?:---|\.\.\.)\s*(?:\n|\Z)", re.DOTALL)
_HEADING = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)
_WIKILINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_DATE_KEYS = ("created", "date", "created_at")
_UPDATED_KEYS = ("updated", "modified", "updated_at")


class MarkdownImporter:
    """Parses a folder of Markdown or text files, or a single file."""

    kind = SourceKind.MARKDOWN

    def parse(self, path: Path, on_issue: IssueSink | None = None) -> Iterator[Conversation]:
        if not path.exists():
            raise ImporterError(f"{path} does not exist")

        root = path if path.is_dir() else path.parent
        files = sorted(_files(path)) if path.is_dir() else [path]
        if not files:
            raise ImporterError(f"no Markdown or text files found in {path}")

        for file in files:
            relative = file.relative_to(root).as_posix()
            try:
                conversation = _conversation(file, relative)
            except (OSError, UnicodeDecodeError, ValidationError) as error:
                report(on_issue, relative, str(error).splitlines()[0])
                continue
            if conversation is None:
                report(on_issue, relative, "file is empty")
            else:
                yield conversation


def _files(root: Path) -> Iterator[Path]:
    for file in root.rglob("*"):
        if not file.is_file() or file.suffix.lower() not in _EXTENSIONS:
            continue
        if any(part in _SKIPPED_DIRS for part in file.relative_to(root).parts[:-1]):
            continue
        yield file


def _conversation(file: Path, relative: str) -> Conversation | None:
    text = file.read_text(encoding="utf-8-sig")
    frontmatter, body = _split_frontmatter(text)
    body = body.strip()
    if not body:
        return None

    conv_id = conversation_id(SourceKind.MARKDOWN, relative)
    created = _first_date(frontmatter, _DATE_KEYS)
    heading = _HEADING.search(body)
    title = frontmatter.get("title") if isinstance(frontmatter.get("title"), str) else None
    title = title or (heading.group(1) if heading else None) or file.stem

    links = sorted({match.strip() for match in _WIKILINK.findall(body) if match.strip()})
    metadata: dict[str, Any] = {"kind": "document", "path": relative}
    if frontmatter:
        metadata["frontmatter"] = json.loads(json.dumps(frontmatter, default=str))
    if links:
        metadata["wikilinks"] = links

    return Conversation(
        id=conv_id,
        source=SourceKind.MARKDOWN,
        external_id=relative,
        title=title,
        created_at=created,
        updated_at=_first_date(frontmatter, _UPDATED_KEYS),
        messages=[
            Message(
                id=message_id(conv_id, "body"),
                role=Role.USER,
                created_at=created,
                content=[ContentPart(text=body)],
            )
        ],
        metadata=metadata,
    )


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER.match(text)
    if match is None:
        return {}, text
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return {str(key): value for key, value in data.items()}, text[match.end() :]


def _first_date(frontmatter: dict[str, Any], keys: tuple[str, ...]) -> datetime | None:
    for key in keys:
        value = frontmatter.get(key)
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day)
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.strip())
            except ValueError:
                continue
    return None
