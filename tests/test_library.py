"""Tests for the on-disk library."""

from __future__ import annotations

import json
from pathlib import Path

from chatlore.library import AddOutcome, Library, SourceStats
from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind


def _conversation(text: str = "hello", source: SourceKind = SourceKind.CHATGPT) -> Conversation:
    return Conversation(
        id=f"conv_{source.value}",
        source=source,
        external_id="ext",
        title="Test",
        messages=[Message(id="m1", role=Role.USER, content=[ContentPart(text=text)])],
    )


def test_add_is_idempotent_and_detects_changes(tmp_path: Path) -> None:
    library = Library(tmp_path)

    assert library.add(_conversation()) is AddOutcome.NEW
    assert library.add(_conversation()) is AddOutcome.UNCHANGED
    assert library.add(_conversation("hello again")) is AddOutcome.UPDATED
    assert library.add(_conversation("hello again")) is AddOutcome.UNCHANGED


def test_unchanged_conversations_are_not_rewritten(tmp_path: Path) -> None:
    library = Library(tmp_path)
    library.add(_conversation())
    path = library.path_for(_conversation())
    before = path.read_text(encoding="utf-8")

    library.add(_conversation())

    assert path.read_text(encoding="utf-8") == before


def test_files_are_readable_json_grouped_by_source(tmp_path: Path) -> None:
    library = Library(tmp_path)
    conversation = _conversation("café")
    library.add(conversation)
    path = tmp_path / "conversations" / "chatgpt" / "conv_chatgpt.json"

    record = json.loads(path.read_text(encoding="utf-8"))

    assert record["schema"] == 1
    assert record["content_hash"] == conversation.content_hash()
    assert "café" in path.read_text(encoding="utf-8")
    assert list(path.parent.glob("*.tmp")) == []


def test_iteration_round_trips_conversations(tmp_path: Path) -> None:
    library = Library(tmp_path)
    stored = [_conversation(source=SourceKind.CLAUDE), _conversation(source=SourceKind.CHATGPT)]
    for conversation in stored:
        library.add(conversation)

    assert sorted(library, key=lambda c: c.id) == sorted(stored, key=lambda c: c.id)


def test_stats_count_per_source(tmp_path: Path) -> None:
    library = Library(tmp_path)

    assert library.stats() == {}

    library.add(_conversation(source=SourceKind.CLAUDE))
    library.add(_conversation(source=SourceKind.GEMINI))

    assert library.stats() == {
        "claude": SourceStats(conversations=1, messages=1),
        "gemini": SourceStats(conversations=1, messages=1),
    }


def test_index_makes_unchanged_checks_cheap_and_survives_loss(tmp_path: Path) -> None:
    with Library(tmp_path) as library:
        library.add(_conversation("x" * 200_000))
    index = tmp_path / "conversations" / "index.json"
    assert index.exists()

    # A fresh instance answers from the index without opening the conversation file.
    fresh = Library(tmp_path)
    assert fresh.add(_conversation("x" * 200_000)) is AddOutcome.UNCHANGED
    assert fresh.stats()["chatgpt"] == SourceStats(conversations=1, messages=1)

    # Without the index the files are scanned and the index is written again.
    index.unlink()
    rebuilt = Library(tmp_path)
    assert rebuilt.add(_conversation("x" * 200_000)) is AddOutcome.UNCHANGED
    assert index.exists()
    assert rebuilt.rebuild_index() == 1
