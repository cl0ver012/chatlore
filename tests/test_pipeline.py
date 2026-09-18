"""Tests for the processing pipeline."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from chatlore.models import ContentPart, Conversation, Message, PartType, Role, SourceKind
from chatlore.pipeline import sync_chunks
from chatlore.store import EdgeType, GraphStore, Label, SQLiteStore


@pytest.fixture
def store() -> Iterator[GraphStore]:
    backend = SQLiteStore(":memory:")
    try:
        yield backend
    finally:
        backend.close()


def _conversation(
    answer: str = "Add an index on customer_id.", extra: bool = False
) -> Conversation:
    messages = [
        Message(id="q", role=Role.USER, content=[ContentPart(text="Why is Postgres slow?")]),
        Message(
            id="a",
            parent_id="q",
            role=Role.ASSISTANT,
            content=[
                ContentPart(text=answer),
                ContentPart(type=PartType.OTHER, text="[tool result]\nEXPLAIN output " * 50),
            ],
        ),
    ]
    if extra:
        messages.append(
            Message(id="q2", parent_id="a", role=Role.USER, content=[ContentPart(text="Thanks!")])
        )
    return Conversation(
        id="conv",
        source=SourceKind.CHATGPT,
        external_id="x",
        title="Postgres indexing",
        messages=messages,
    )


def test_chunks_are_written_with_edges_and_search_metadata(store: GraphStore) -> None:
    conversation = _conversation()
    store.upsert_conversation(conversation)

    report = sync_chunks(store, [conversation])

    assert (report.conversations, report.added, report.removed, report.unchanged) == (1, 2, 0, 0)
    chunks = store.find_nodes(Label.CHUNK, {"conversation_id": "conv"})
    assert sorted(c.props["text"] for c in chunks) == [
        "Add an index on customer_id.",
        "Why is Postgres slow?",
    ]
    answer = next(c for c in chunks if c.props["message_id"] == "a")
    assert answer.props["role"] == "assistant"
    assert answer.props["title"] == "Postgres indexing"
    assert answer.props["source"] == "chatgpt"
    linked = store.neighbors("a", [EdgeType.HAS_CHUNK])
    assert [node.id for _, node in linked] == [answer.id]


def test_tool_output_is_searchable_as_text_but_not_chunked(store: GraphStore) -> None:
    conversation = _conversation()
    store.upsert_conversation(conversation)
    sync_chunks(store, [conversation])

    assert store.search_text("EXPLAIN", labels=[Label.CHUNK]) == []
    assert [h.label for h in store.search_text("EXPLAIN")] == [Label.MESSAGE]


def test_running_again_changes_nothing(store: GraphStore) -> None:
    conversation = _conversation()
    store.upsert_conversation(conversation)
    sync_chunks(store, [conversation])

    report = sync_chunks(store, [conversation])

    assert (report.added, report.removed, report.unchanged) == (0, 0, 2)
    assert store.count_nodes(Label.CHUNK) == 2


def test_only_changed_text_is_rechunked_and_embeddings_survive(store: GraphStore) -> None:
    original = _conversation()
    store.upsert_conversation(original)
    sync_chunks(store, [original])
    question = next(
        c
        for c in store.find_nodes(Label.CHUNK, {"conversation_id": "conv"})
        if c.props["message_id"] == "q"
    )
    store.set_embedding(question.id, [1.0, 0.0])

    edited = _conversation(answer="Actually, add a composite index.", extra=True)
    store.upsert_conversation(edited)
    report = sync_chunks(store, [edited])

    assert (report.added, report.removed, report.unchanged) == (2, 1, 1)
    texts = sorted(c.props["text"] for c in store.find_nodes(Label.CHUNK))
    assert texts == ["Actually, add a composite index.", "Thanks!", "Why is Postgres slow?"]
    assert [h.node_id for h in store.search_vector([1.0, 0.0])] == [question.id]


def test_chunks_go_away_with_their_message_or_conversation(store: GraphStore) -> None:
    long = _conversation(extra=True)
    store.upsert_conversation(long)
    sync_chunks(store, [long])
    assert store.count_nodes(Label.CHUNK) == 3

    store.upsert_conversation(_conversation())  # the third message is gone
    assert store.count_nodes(Label.CHUNK) == 2

    store.delete_conversation("conv")
    assert store.count_nodes(Label.CHUNK) == 0
    assert store.search_text("Postgres") == []
