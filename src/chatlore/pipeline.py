"""Processing steps that turn imported conversations into a searchable graph.

Every step is idempotent. Running it again on unchanged data does nothing, and
running it after a re-import touches only what changed, so expensive results
such as embeddings are never recomputed without a reason.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from chatlore.chunking import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    chunk_conversation,
)
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
