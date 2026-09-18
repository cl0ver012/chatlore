"""Tests for embeddings, the cache, and the embedding pipeline step."""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path

import pytest

from chatlore.embeddings import (
    DEFAULT_MODEL,
    EmbeddingCache,
    FastEmbedEmbedder,
    make_embedder,
    normalise,
    text_hash,
)
from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind
from chatlore.pipeline import EmbeddingModelMismatchError, sync_chunks, sync_embeddings
from chatlore.store import GraphStore, Label, SQLiteStore
from tests.fakes import FakeEmbedder


@pytest.fixture
def store() -> Iterator[GraphStore]:
    backend = SQLiteStore(":memory:")
    try:
        yield backend
    finally:
        backend.close()


def _conversation(*texts: str, id_: str = "conv") -> Conversation:
    return Conversation(
        id=id_,
        source=SourceKind.CLAUDE,
        external_id=id_,
        title="Test",
        messages=[
            Message(id=f"{id_}_m{i}", role=Role.USER, content=[ContentPart(text=text)])
            for i, text in enumerate(texts)
        ],
    )


def _load(store: GraphStore, conversation: Conversation) -> None:
    store.upsert_conversation(conversation)
    sync_chunks(store, [conversation])


def test_normalise_gives_unit_length_and_survives_zero() -> None:
    assert math.isclose(math.hypot(*normalise([3.0, 4.0])), 1.0)
    assert normalise([3.0, 4.0]) == [0.6, 0.8]
    assert normalise([0.0, 0.0]) == [0.0, 0.0]


def test_cache_round_trips_per_model(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "embeddings.db"
    cache = EmbeddingCache(path)
    cache.put_many("model-a", {text_hash("hello"): [0.25, 0.5]})
    cache.close()

    reopened = EmbeddingCache(path)
    assert reopened.get_many("model-a", [text_hash("hello"), text_hash("other")]) == {
        text_hash("hello"): [0.25, 0.5]
    }
    assert reopened.get_many("model-b", [text_hash("hello")]) == {}
    reopened.close()


def test_every_chunk_gets_a_normalised_embedding(store: GraphStore) -> None:
    _load(store, _conversation("postgres index tuning", "weekend trip to sintra"))
    embedder = FakeEmbedder()
    seen: list[int] = []

    report = sync_embeddings(store, embedder, batch_size=1, on_progress=seen.append)

    assert (report.embedded, report.from_cache, report.already_done) == (2, 0, 0)
    assert seen == [1, 1]
    assert store.count_embeddings() == 2
    assert store.nodes_without_embedding(Label.CHUNK) == []
    assert store.get_meta("embedding_model") == "fake-model"
    hits = store.search_vector(normalise(embedder.embed_query("postgres index")), limit=1)
    chunk = store.get_node(hits[0].node_id)
    assert chunk is not None and chunk.props["text"] == "postgres index tuning"
    assert hits[0].distance < 1.0


def test_running_again_embeds_nothing(store: GraphStore) -> None:
    _load(store, _conversation("one", "two"))
    embedder = FakeEmbedder()
    sync_embeddings(store, embedder)

    report = sync_embeddings(store, embedder)

    assert (report.embedded, report.from_cache, report.already_done) == (0, 0, 2)
    assert sorted(embedder.embedded) == ["one", "two"]


def test_only_new_chunks_are_embedded_after_an_edit(store: GraphStore) -> None:
    embedder = FakeEmbedder()
    _load(store, _conversation("keep this", "change this"))
    sync_embeddings(store, embedder)

    _load(store, _conversation("keep this", "changed now"))
    report = sync_embeddings(store, embedder)

    assert (report.embedded, report.already_done) == (1, 1)
    assert sorted(embedder.embedded) == ["change this", "changed now", "keep this"]
    assert store.count_embeddings() == 2


def test_cache_makes_a_rebuilt_database_free(store: GraphStore) -> None:
    cache = EmbeddingCache()
    first = FakeEmbedder()
    _load(store, _conversation("alpha", "beta"))
    sync_embeddings(store, first, cache)

    rebuilt = SQLiteStore(":memory:")
    _load(rebuilt, _conversation("alpha", "beta"))
    second = FakeEmbedder()
    report = sync_embeddings(rebuilt, second, cache)

    assert (report.embedded, report.from_cache) == (0, 2)
    assert second.embedded == []
    assert rebuilt.count_embeddings() == 2
    rebuilt.close()


def test_switching_models_is_refused_until_embeddings_are_cleared(store: GraphStore) -> None:
    _load(store, _conversation("alpha"))
    sync_embeddings(store, FakeEmbedder("model-a"))

    with pytest.raises(EmbeddingModelMismatchError, match="model-a"):
        sync_embeddings(store, FakeEmbedder("model-b"))

    store.clear_embeddings()
    assert store.count_embeddings() == 0
    assert store.get_meta("embedding_model") is None
    report = sync_embeddings(store, FakeEmbedder("model-b"))
    assert report.embedded == 1
    assert store.get_meta("embedding_model") == "model-b"


def test_embedder_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CHATLORE_EMBEDDING_MODEL", raising=False)
    assert make_embedder(tmp_path).name == DEFAULT_MODEL

    monkeypatch.setenv("CHATLORE_EMBEDDING_MODEL", "some/other-model")
    embedder = make_embedder(tmp_path)
    assert embedder.name == "some/other-model"
    assert isinstance(embedder, FastEmbedEmbedder)
    assert embedder.embed_passages([]) == []  # no model is loaded for empty input
