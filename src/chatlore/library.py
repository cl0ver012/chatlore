"""The on-disk library of imported conversations.

Every imported conversation is kept as one JSON file under
``<home>/conversations/<source>/<id>.json``. The library is the source of
truth: indexes and databases built later can always be rebuilt from it, and the
files stay readable without ChatLore.

Importing is idempotent. A conversation whose content hash has not changed is
left untouched, so running the same import twice changes nothing on disk.
"""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from chatlore.models import Conversation

SCHEMA_VERSION = 1


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
    """Reads and writes conversations under a ChatLore home directory."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.root = home / "conversations"

    def path_for(self, conversation: Conversation) -> Path:
        """Return where ``conversation`` is, or would be, stored."""
        return self.root / conversation.source.value / f"{conversation.id}.json"

    def add(self, conversation: Conversation) -> AddOutcome:
        """Store ``conversation`` unless an identical copy is already there."""
        path = self.path_for(conversation)
        digest = conversation.content_hash()

        outcome = AddOutcome.NEW
        if path.exists():
            if _read(path).get("content_hash") == digest:
                return AddOutcome.UNCHANGED
            outcome = AddOutcome.UPDATED

        record = {
            "schema": SCHEMA_VERSION,
            "content_hash": digest,
            "imported_at": datetime.now(UTC).isoformat(),
            "conversation": conversation.model_dump(mode="json"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return outcome

    def __iter__(self) -> Iterator[Conversation]:
        if not self.root.exists():
            return
        for path in sorted(self.root.glob("*/*.json")):
            yield Conversation.model_validate(_read(path)["conversation"])

    def stats(self) -> dict[str, SourceStats]:
        """Return conversation and message totals per source."""
        conversations: Counter[str] = Counter()
        messages: Counter[str] = Counter()
        for conversation in self:
            conversations[conversation.source.value] += 1
            messages[conversation.source.value] += len(conversation.messages)
        return {
            source: SourceStats(conversations=count, messages=messages[source])
            for source, count in sorted(conversations.items())
        }


def _read(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a ChatLore library record")
    return data
