"""Tests for hybrid search."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from chatlore.embeddings import normalise
from chatlore.extraction import ExtractionCache
from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind
from chatlore.pipeline import sync_chunks, sync_embeddings, sync_entities, sync_extractions
from chatlore.search import hybrid_search
from chatlore.store import GraphStore, SQLiteStore
from tests.fakes import FakeEmbedder, FakeLLM


def _conversation(conversation_id: str, source: SourceKind, *texts: str) -> Conversation:
    messages: list[Message] = []
    for position, text in enumerate(texts):
        role = Role.USER if position % 2 == 0 else Role.ASSISTANT
        messages.append(
            Message(
                id=f"{conversation_id}-{position}",
                parent_id=f"{conversation_id}-{position - 1}" if position else None,
                role=role,
                content=[ContentPart(text=text)],
            )
        )
    return Conversation(
        id=conversation_id,
        source=source,
        external_id=conversation_id,
        title=f"Title {conversation_id}",
        messages=messages,
    )


@pytest.fixture
def store() -> Iterator[GraphStore]:
    backend = SQLiteStore(":memory:")
    conversations = [
        _conversation(
            "pg",
            SourceKind.CHATGPT,
            "Why is my postgres query slow?",
            "Add a composite index on customer_id and created_at.",
        ),
        _conversation(
            "trip",
            SourceKind.CLAUDE,
            "Suggest a weekend trip near the mountains.",
            "Try a cabin by the lake, it is quiet and slow.",
        ),
    ]
    try:
        for conversation in conversations:
            backend.upsert_conversation(conversation)
        sync_chunks(backend, conversations)
        sync_embeddings(backend, FakeEmbedder())
        yield backend
    finally:
        backend.close()


def _embed(query: str) -> list[float]:
    return normalise(FakeEmbedder().embed_query(query))


def test_a_message_found_both_ways_ranks_first_and_appears_once(store: GraphStore) -> None:
    hits = hybrid_search(store, "postgres query slow", _embed("postgres query slow"))

    assert hits[0].message_id == "pg-0"
    assert hits[0].by_words and hits[0].by_meaning
    assert [hit.message_id for hit in hits].count("pg-0") == 1
    assert hits[0].title == "Title pg"
    assert "[postgres]" in hits[0].snippet


def test_meaning_finds_what_words_miss(store: GraphStore) -> None:
    # Every word must match in full-text search, so the extra word hides the message there.
    query = "postgres query slow yesterday"

    hits = hybrid_search(store, query, _embed(query))

    assert store.search_text(query) == []
    assert hits[0].message_id == "pg-0"
    assert not hits[0].by_words and hits[0].by_meaning
    assert hits[0].snippet == "Why is my postgres query slow?"
    assert hits[0].source == "chatgpt"


def test_word_matches_are_kept_without_a_close_chunk(store: GraphStore) -> None:
    hits = hybrid_search(store, "composite", _embed("unrelated words entirely"), limit=10)

    composite = next(hit for hit in hits if hit.message_id == "pg-1")
    assert composite.by_words


def test_sources_and_limit_are_honoured(store: GraphStore) -> None:
    only_claude = hybrid_search(store, "slow", _embed("slow"), sources=["claude"])
    one = hybrid_search(store, "slow", _embed("slow"), limit=1)

    assert only_claude
    assert {hit.source for hit in only_claude} == {"claude"}
    assert len(one) == 1


def test_without_embeddings_it_falls_back_to_words() -> None:
    backend = SQLiteStore(":memory:")
    try:
        conversation = _conversation("pg", SourceKind.CHATGPT, "Why is my postgres query slow?")
        backend.upsert_conversation(conversation)

        hits = hybrid_search(backend, "postgres", _embed("postgres"))
    finally:
        backend.close()

    assert [hit.message_id for hit in hits] == ["pg-0"]
    assert hits[0].by_words and not hits[0].by_meaning


def test_messages_mentioning_a_matching_entity_are_found() -> None:
    backend = SQLiteStore(":memory:")
    try:
        conversation = _conversation(
            "mac", SourceKind.CLAUDE, "Parsec keeps dropping frames.", "Try a wired connection."
        )
        backend.upsert_conversation(conversation)
        sync_chunks(backend, [conversation])
        with ExtractionCache() as cache:
            sync_extractions(backend, FakeLLM(), cache)
            sync_entities(backend, cache, "fake-llm")
        # The entity's summary says "Parsec appears in: ...", which no message says.
        query = "parsec appears"

        hits = hybrid_search(backend, query, _embed(query))
        other_source = hybrid_search(backend, query, _embed(query), sources=["chatgpt"])
    finally:
        backend.close()

    assert [hit.message_id for hit in hits] == ["mac-0"]
    assert hits[0].by_entity and not hits[0].by_words and not hits[0].by_meaning
    assert hits[0].title == "Title mac"
    assert other_source == []
