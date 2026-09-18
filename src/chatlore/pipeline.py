"""Processing steps that turn imported conversations into a searchable graph.

Every step is idempotent. Running it again on unchanged data does nothing, and
running it after a re-import touches only what changed, so expensive results
such as embeddings are never recomputed without a reason.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from chatlore.chunking import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    chunk_conversation,
)
from chatlore.embeddings import Embedder, EmbeddingCache, normalise, text_hash
from chatlore.models import Conversation
from chatlore.store import GraphStore, Label
from chatlore.store.mapping import chunks_to_graph


@dataclass(slots=True)
class ChunkReport:
    """What a chunking run did."""

    conversations: int = 0
    added: int = 0
    removed: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.added + self.unchanged


def sync_chunks(
    store: GraphStore,
    conversations: Iterable[Conversation],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> ChunkReport:
    """Make the chunks in ``store`` match the current text of ``conversations``.

    Chunk ids depend on their text, so comparing id sets is enough to find what
    is new and what is stale. Chunks whose text did not change are left alone,
    which keeps their embeddings.
    """
    report = ChunkReport()
    with store.transaction():
        for conversation in conversations:
            report.conversations += 1
            wanted = chunk_conversation(conversation, max_tokens, overlap_tokens)
            wanted_ids = {chunk.id for chunk in wanted}
            existing_ids = {
                node.id
                for node in store.find_nodes(Label.CHUNK, {"conversation_id": conversation.id})
            }

            stale = existing_ids - wanted_ids
            fresh = [chunk for chunk in wanted if chunk.id not in existing_ids]
            if stale:
                store.delete_nodes(stale)
            if fresh:
                nodes, edges = chunks_to_graph(conversation, fresh)
                store.upsert_nodes(nodes)
                store.upsert_edges(edges)

            report.removed += len(stale)
            report.added += len(fresh)
            report.unchanged += len(wanted) - len(fresh)
    return report


EMBEDDING_MODEL_KEY = "embedding_model"


class EmbeddingModelMismatchError(Exception):
    """The store holds vectors from another model; mixing them would corrupt search."""


@dataclass(slots=True)
class EmbedReport:
    """What an embedding run did."""

    embedded: int = 0
    from_cache: int = 0
    already_done: int = 0

    @property
    def total(self) -> int:
        return self.embedded + self.from_cache + self.already_done


def sync_embeddings(
    store: GraphStore,
    embedder: Embedder,
    cache: EmbeddingCache | None = None,
    batch_size: int = 32,
    on_progress: Callable[[int], None] | None = None,
) -> EmbedReport:
    """Give every chunk that lacks one an embedding. Safe to interrupt and resume.

    Each batch is committed on its own, so stopping halfway keeps the work done
    so far. Vectors are normalised to unit length before they are stored.
    """
    stored_model = store.get_meta(EMBEDDING_MODEL_KEY)
    if stored_model is not None and stored_model != embedder.name:
        raise EmbeddingModelMismatchError(
            f"the database was embedded with '{stored_model}' but '{embedder.name}' is configured"
        )

    report = EmbedReport(already_done=store.count_embeddings())
    while True:
        batch = store.nodes_without_embedding(Label.CHUNK, limit=batch_size)
        if not batch:
            return report

        hashes = {node.id: text_hash(str(node.props.get("text", ""))) for node in batch}
        known = cache.get_many(embedder.name, list(hashes.values())) if cache is not None else {}
        missing = [node for node in batch if hashes[node.id] not in known]
        if missing:
            vectors = embedder.embed_passages([str(node.props.get("text", "")) for node in missing])
            fresh = {
                hashes[node.id]: normalise(vector)
                for node, vector in zip(missing, vectors, strict=True)
            }
            if cache is not None:
                cache.put_many(embedder.name, fresh)
            known.update(fresh)

        with store.transaction():
            for node in batch:
                store.set_embedding(node.id, known[hashes[node.id]])
            store.set_meta(EMBEDDING_MODEL_KEY, embedder.name)

        report.embedded += len(missing)
        report.from_cache += len(batch) - len(missing)
        if on_progress is not None:
            on_progress(len(batch))
