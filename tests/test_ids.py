"""Tests for deterministic identifiers and content hashes."""

from __future__ import annotations

from chatlore.ids import canonical_json, content_hash, conversation_id, message_id


def test_canonical_json_is_independent_of_key_order() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": [1, 2], "b": 1})


def test_canonical_json_keeps_non_ascii_text() -> None:
    assert canonical_json({"text": "café"}) == '{"text":"café"}'


def test_content_hash_changes_with_content() -> None:
    assert content_hash({"a": 1}) == content_hash({"a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})


def test_conversation_id_is_stable_and_prefixed() -> None:
    first = conversation_id("chatgpt", "abc-123")

    assert first == conversation_id("chatgpt", "abc-123")
    assert first.startswith("conv_")
    assert len(first) == len("conv_") + 24


def test_conversation_id_depends_on_source() -> None:
    assert conversation_id("chatgpt", "abc") != conversation_id("claude", "abc")


def test_ids_do_not_collide_when_parts_are_shifted() -> None:
    assert conversation_id("ab", "c") != conversation_id("a", "bc")


def test_message_id_depends_on_conversation() -> None:
    assert message_id("conv_1", "m1") != message_id("conv_2", "m1")
    assert message_id("conv_1", "m1").startswith("msg_")
