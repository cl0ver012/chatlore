"""Hybrid search: word matches and meaning matches ranked together.

Full-text search finds messages that contain the query's words; vector search
finds chunks whose meaning is close to it. The two lists are merged with
reciprocal rank fusion, which only looks at each hit's position in its own
list, so BM25 scores and vector distances never have to be put on one scale.

Chunks are folded into the message they came from. A message found both ways
is shown once and ranks above one found only one way, and messages that were
never chunked, such as tool output, stay findable through their words.

Once `chatlore extract` has built the entity graph, a third list joins in:
messages that mention an entity whose name or summary matches the query. It
finds a conversation about a tool or a person even where the message itself
names them differently.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from chatlore.store import EdgeType, GraphStore, Label

RRF_K = 60
"""Damping constant from the original reciprocal rank fusion paper."""

CANDIDATES_PER_RESULT = 4
"""How many candidates each list contributes for every result asked for."""


@dataclass(frozen=True, slots=True)
class HybridHit:
    """A message found by its words, its meaning, the entities it mentions, or several."""

    message_id: str
    conversation_id: str | None
    source: str | None
    title: str | None
    snippet: str
    score: float
    by_words: bool
    by_meaning: bool
    by_entity: bool = False


def hybrid_search(
    store: GraphStore,
    query: str,
    embedding: Sequence[float],
    limit: int = 20,
    sources: Sequence[str] | None = None,
) -> list[HybridHit]:
    """Rank messages by fusing full-text hits with the nearest chunks to ``embedding``."""
    candidates = limit * CANDIDATES_PER_RESULT
    hits: dict[str, HybridHit] = {}

    for rank, text_hit in enumerate(
        store.search_text(query, limit=candidates, sources=sources, labels=[Label.MESSAGE]),
        start=1,
    ):
        hits[text_hit.node_id] = HybridHit(
            message_id=text_hit.node_id,
            conversation_id=text_hit.conversation_id,
            source=text_hit.source,
            title=text_hit.title,
            snippet=text_hit.snippet,
            score=1.0 / (RRF_K + rank),
            by_words=True,
            by_meaning=False,
        )

    wanted = set(sources or [])
    seen: set[str] = set()
    # Source filtering happens after the nearest-neighbour query, so ask for extra.
    for vector_hit in store.search_vector(
        embedding, limit=candidates * (4 if wanted else 1), labels=[Label.CHUNK]
    ):
        node = store.get_node(vector_hit.node_id)
        if node is None or (wanted and node.props.get("source") not in wanted):
            continue
        message_id = str(node.props.get("message_id") or node.id)
        if message_id in seen:
            continue  # a better chunk of the same message already counted
        seen.add(message_id)
        score = 1.0 / (RRF_K + len(seen))
        found = hits.get(message_id)
        if found is not None:
            hits[message_id] = HybridHit(
                message_id=message_id,
                conversation_id=found.conversation_id,
                source=found.source,
                title=found.title,
                snippet=found.snippet,
                score=found.score + score,
                by_words=True,
                by_meaning=True,
            )
        else:
            title = node.props.get("title")
            hits[message_id] = HybridHit(
                message_id=message_id,
                conversation_id=node.props.get("conversation_id"),
                source=node.props.get("source"),
                title=str(title) if title else None,
                snippet=str(node.props.get("text", "")),
                score=score,
                by_words=False,
                by_meaning=True,
            )
        if len(seen) >= candidates:
            break

    mentioned: set[str] = set()
    for entity_hit in store.search_text(query, limit=limit, labels=[Label.ENTITY]):
        chunks = store.neighbors(entity_hit.node_id, [EdgeType.MENTIONS], "in", limit=candidates)
        for _, chunk in sorted(chunks, key=lambda pair: pair[1].id):
            if wanted and chunk.props.get("source") not in wanted:
                continue
            message_id = str(chunk.props.get("message_id") or chunk.id)
            if message_id in mentioned:
                continue
            mentioned.add(message_id)
            score = 1.0 / (RRF_K + len(mentioned))
            found = hits.get(message_id)
            if found is not None:
                hits[message_id] = replace(found, score=found.score + score, by_entity=True)
            else:
                title = chunk.props.get("title")
                hits[message_id] = HybridHit(
                    message_id=message_id,
                    conversation_id=chunk.props.get("conversation_id"),
                    source=chunk.props.get("source"),
                    title=str(title) if title else None,
                    snippet=str(chunk.props.get("text", "")),
                    score=score,
                    by_words=False,
                    by_meaning=False,
                    by_entity=True,
                )
        if len(mentioned) >= candidates:
            break

    ranked = sorted(hits.values(), key=lambda hit: hit.score, reverse=True)
    return ranked[:limit]
