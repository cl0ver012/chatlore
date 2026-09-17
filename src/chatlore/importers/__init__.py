"""Importers turn vendor exports into canonical conversations."""

from __future__ import annotations

import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from chatlore.importers.base import (
    Importer,
    ImporterError,
    ImportIssue,
    IssueSink,
    load_json,
    read_candidates,
)
from chatlore.importers.chatgpt import ChatGPTImporter
from chatlore.importers.claude import ClaudeImporter
from chatlore.importers.gemini import GeminiImporter
from chatlore.importers.markdown import MarkdownImporter
from chatlore.importers.notes import make_note
from chatlore.models import SourceKind

IMPORTERS: dict[SourceKind, Importer] = {
    SourceKind.CHATGPT: ChatGPTImporter(),
    SourceKind.CLAUDE: ClaudeImporter(),
    SourceKind.GEMINI: GeminiImporter(),
    SourceKind.MARKDOWN: MarkdownImporter(),
}

__all__ = [
    "IMPORTERS",
    "ImportIssue",
    "Importer",
    "ImporterError",
    "IssueSink",
    "detect_source",
    "get_importer",
    "make_note",
]


def get_importer(source: str) -> Importer:
    """Return the importer for ``source``, or raise ``ImporterError`` for unknown names."""
    try:
        return IMPORTERS[SourceKind(source)]
    except (ValueError, KeyError):
        known = ", ".join(kind.value for kind in IMPORTERS)
        raise ImporterError(f"unknown source '{source}'. Choose one of: {known}") from None


def detect_source(path: Path) -> SourceKind:
    """Work out which importer fits ``path`` by looking at what it contains."""
    if not path.exists():
        raise ImporterError(f"{path} does not exist")

    names = _member_names(path)
    if "MyActivity.json" in names:
        return SourceKind.GEMINI
    if "conversations.json" in names:
        for display_name, content in read_candidates(path, "conversations.json"):
            return _detect_conversations(load_json(display_name, content), path)
    if path.is_file() and path.suffix.lower() == ".json":
        return _detect_conversations(load_json(str(path), path.read_bytes()), path)
    if path.is_dir() or path.suffix.lower() in {".md", ".markdown", ".txt"}:
        return SourceKind.MARKDOWN
    raise ImporterError(f"could not recognise the export at {path}; pass the source explicitly")


def _member_names(path: Path) -> set[str]:
    if path.is_dir():
        return {file.name for file in path.rglob("*.json")}
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            return {PurePosixPath(name).name for name in archive.namelist()}
    return {path.name}


def _detect_conversations(data: Any, path: Path) -> SourceKind:
    first = (
        next((item for item in data if isinstance(item, dict)), None)
        if isinstance(data, list)
        else None
    )
    if first is not None:
        if "mapping" in first:
            return SourceKind.CHATGPT
        if "chat_messages" in first:
            return SourceKind.CLAUDE
        if "header" in first and "time" in first:
            return SourceKind.GEMINI
    raise ImporterError(f"could not recognise the export at {path}; pass the source explicitly")
