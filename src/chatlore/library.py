"""The on-disk library of imported conversations.

Every imported conversation is kept as one JSON file under
``<home>/conversations/<source>/<id>.json``. The library is the source of
truth: indexes and databases built later can always be rebuilt from it, and the
files stay readable without ChatLore.

Importing is idempotent. A conversation whose content hash has not changed is
left untouched, so running the same import twice changes nothing on disk. The
hashes live in a small ``index.json`` next to the files, so that check never
has to open a conversation file. The index is rebuilt from the files whenever
it is missing.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from chatlore.models import Conversation

SCHEMA_VERSION = 1
INDEX_NAME = "index.json"
_HEAD_BYTES = 512
_HASH_LINE = re.compile(r'"content_hash":\s*"([0-9a-f]{64})"')


class AddOutcome(StrEnum):
    """What adding a conversation did to the library."""

    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class SourceStats:
    """Totals for one source."""

    conversations: int
    messages: int


class Library:
    """Reads and writes conversations under a ChatLore home directory.

    Use it as a context manager, or call ``flush`` after a batch of ``add``
    calls, so the index is written once instead of after every conversation.
    """

    def __init__(self, home: Path) -> None:
        self.home = home
        self.root = home / "conversations"
        self._index: dict[str, dict[str, Any]] | None = None
        self._dirty = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.flush()

    def path_for(self, conversation: Conversation) -> Path:
        """Return where ``conversation`` is, or would be, stored."""
        return self.root / conversation.source.value / f"{conversation.id}.json"

    def add(self, conversation: Conversation) -> AddOutcome:
        """Store ``conversation`` unless an identical copy is already there."""
        index = self._load_index()
        digest = conversation.content_hash()
        entry = index.get(conversation.id)

        if entry is not None and entry.get("content_hash") == digest:
            return AddOutcome.UNCHANGED
        outcome = AddOutcome.UPDATED if entry is not None else AddOutcome.NEW

        record = {
            "schema": SCHEMA_VERSION,
            "content_hash": digest,
            "imported_at": datetime.now(UTC).isoformat(),
            "conversation": conversation.model_dump(mode="json"),
        }
        path = self.path_for(conversation)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

        index[conversation.id] = {
            "source": conversation.source.value,
            "content_hash": digest,
            "messages": len(conversation.messages),
        }
        self._dirty = True
        return outcome

    def flush(self) -> None:
        """Write the index if anything changed since it was last written."""
        if self._dirty and self._index is not None:
            self._write_index(self._index)
            self._dirty = False

    def rebuild_index(self) -> int:
        """Recreate the index from the files on disk. Returns the number of entries."""
        index: dict[str, dict[str, Any]] = {}
        if self.root.exists():
            for path in sorted(self.root.glob("*/*.json")):
                record = _read(path)
                conversation = record.get("conversation", {})
                index[path.stem] = {
                    "source": path.parent.name,
                    "content_hash": record.get("content_hash") or _stored_hash(path),
                    "messages": len(conversation.get("messages", [])),
                }
        self._index = index
        self._write_index(index)
        self._dirty = False
        return len(index)

    def __iter__(self) -> Iterator[Conversation]:
        if not self.root.exists():
            return
        for path in sorted(self.root.glob("*/*.json")):
            yield Conversation.model_validate(_read(path)["conversation"])

    def stats(self) -> dict[str, SourceStats]:
        """Return conversation and message totals per source."""
        conversations: dict[str, int] = {}
        messages: dict[str, int] = {}
        for entry in self._load_index().values():
            source = str(entry.get("source"))
            conversations[source] = conversations.get(source, 0) + 1
            messages[source] = messages.get(source, 0) + int(entry.get("messages", 0))
        return {
            source: SourceStats(conversations=count, messages=messages[source])
            for source, count in sorted(conversations.items())
        }

    # -- internals -----------------------------------------------------------

    def _load_index(self) -> dict[str, dict[str, Any]]:
        if self._index is not None:
            return self._index
        path = self.root / INDEX_NAME
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else None
            if isinstance(entries, dict):
                self._index = entries
                return self._index
        self.rebuild_index()
        return self._index if self._index is not None else {}

    def _write_index(self, index: dict[str, dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / INDEX_NAME
        temporary = path.with_suffix(".json.tmp")
        payload = {"schema": SCHEMA_VERSION, "entries": index}
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)


def _stored_hash(path: Path) -> str | None:
    """Return the content hash recorded near the top of a library file."""
    with path.open("rb") as handle:
        head = handle.read(_HEAD_BYTES).decode("utf-8", errors="ignore")
    match = _HASH_LINE.search(head)
    return match.group(1) if match is not None else None


def _read(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a ChatLore library record")
    return data
