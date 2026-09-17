"""Tests for the canonical conversation model."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from chatlore.models import (
    ContentPart,
    Conversation,
    Message,
    PartType,
    Role,
    SourceKind,
)


def _message(
    id_: str, parent: str | None = None, text: str = "hi", role: Role = Role.USER
) -> Message:
    return Message(id=id_, parent_id=parent, role=role, content=[ContentPart(text=text)])


def _conversation(messages: list[Message], leaf: str | None = None) -> Conversation:
    return Conversation(
        id="conv_1",
        source=SourceKind.CHATGPT,
        external_id="ext-1",
        title="Test",
        messages=messages,
        current_leaf_id=leaf,
    )


def test_message_text_joins_parts_and_fences_code() -> None:
    message = Message(
        id="m1",
        role=Role.ASSISTANT,
        content=[
            ContentPart(text="Use this:"),
            ContentPart(type=PartType.CODE, language="python", text="print(1)"),
            ContentPart(text=""),
        ],
    )

    assert message.text == "Use this:\n\n```python\nprint(1)\n```"


def test_naive_timestamps_are_taken_as_utc() -> None:
    message = Message(id="m1", role=Role.USER, created_at=datetime(2026, 1, 2, 3, 4, 5))

    assert message.created_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_aware_timestamps_are_converted_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    conversation = Conversation(
        id="conv_1",
        source=SourceKind.CLAUDE,
        external_id="x",
        created_at=datetime(2026, 1, 2, 12, 0, tzinfo=plus_two),
    )

    assert conversation.created_at == datetime(2026, 1, 2, 10, 0, tzinfo=UTC)


def test_unknown_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Message.model_validate({"id": "m1", "role": "user", "surprise": True})


def test_duplicate_message_ids_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate message id"):
        _conversation([_message("m1"), _message("m1")])


def test_unknown_parent_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown parent_id"):
        _conversation([_message("m1", parent="missing")])


def test_unknown_leaf_is_rejected() -> None:
    with pytest.raises(ValidationError, match="current_leaf_id"):
        _conversation([_message("m1")], leaf="missing")


def test_parent_cycles_are_rejected() -> None:
    with pytest.raises(ValidationError, match="cycle"):
        _conversation([_message("m1", parent="m2"), _message("m2", parent="m1")])


def test_linear_messages_without_leaf_keeps_stored_order() -> None:
    conversation = _conversation([_message("m1"), _message("m2", parent="m1")])

    assert [m.id for m in conversation.linear_messages()] == ["m1", "m2"]


def test_linear_messages_follows_the_active_branch() -> None:
    messages = [
        _message("root"),
        _message("old-answer", parent="root", role=Role.ASSISTANT),
        _message("new-answer", parent="root", role=Role.ASSISTANT),
        _message("follow-up", parent="new-answer"),
    ]
    conversation = _conversation(messages, leaf="follow-up")

    assert [m.id for m in conversation.linear_messages()] == ["root", "new-answer", "follow-up"]


def test_long_chains_validate_without_recursion_limits() -> None:
    messages = [_message("m0")]
    messages += [_message(f"m{i}", parent=f"m{i - 1}") for i in range(1, 5000)]

    conversation = _conversation(messages, leaf="m4999")

    assert len(conversation.linear_messages()) == 5000


def test_content_hash_ignores_metadata_but_not_content() -> None:
    base = _conversation([_message("m1", text="hello")])
    same_content = base.model_copy(update={"metadata": {"importer_version": "2"}})
    changed = _conversation([_message("m1", text="hello!")])

    assert base.content_hash() == same_content.content_hash()
    assert base.content_hash() != changed.content_hash()


def test_round_trips_through_json() -> None:
    conversation = _conversation([_message("m1"), _message("m2", parent="m1")], leaf="m2")

    restored = Conversation.model_validate_json(conversation.model_dump_json())

    assert restored == conversation
