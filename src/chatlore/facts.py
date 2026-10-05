"""Reading facts from the graph: what was established, and where it was said.

``chatlore extract`` finds facts as it reads the chunks (see
``chatlore.pipeline.sync_entities``). Each fact is about one entity, may involve
a second, and keeps the messages that said it, so every fact leads back to the
conversation it came from. The command line, the API, the MCP server, and chat
read facts through this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from chatlore.store import EdgeType, GraphStore, Label, Node


@dataclass(frozen=True, slots=True)
class FactSource:
    """A message that said a fact."""

    message_id: str
    conversation_id: str
    title: str | None
    source: str | None
    created_at: str | None

    @property
    def date(self) -> str | None:
        return self.created_at[:10] if self.created_at else None


@dataclass(frozen=True, slots=True)
class Fact:
    """A statement from the conversations, with the messages that said it, newest first."""

    id: str
    statement: str
    subject: str
    object: str | None
    sources: tuple[FactSource, ...]
    said: int
    """How many messages said it; ``sources`` keeps the latest of them."""

    @property
    def date(self) -> str | None:
        """When it was last said."""
        return self.sources[0].date if self.sources else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "subject": self.subject,
            "object": self.object,
            "said": self.said,
            "sources": [
                {
                    "message_id": source.message_id,
                    "conversation_id": source.conversation_id,
                    "title": source.title,
                    "source": source.source,
                    "created_at": source.created_at,
                }
                for source in self.sources
            ],
        }


def fact(node: Node) -> Fact:
    """The fact a ``Fact`` node holds."""
    sources = [
        FactSource(
            str(item.get("message_id")),
            str(item.get("conversation_id")),
            item.get("title"),
            item.get("source"),
            item.get("created_at"),
        )
        for item in node.props.get("sources", [])
    ]
    return Fact(
        node.id,
        str(node.props.get("statement", "")),
        str(node.props.get("subject", "")),
        node.props.get("object"),
        tuple(reversed(sources)),
        int(node.props.get("said", len(sources))),
    )


def _newest_first(facts: Iterable[Fact]) -> list[Fact]:
    return sorted(
        facts,
        key=lambda item: str(item.sources[0].created_at or "") if item.sources else "",
        reverse=True,
    )


def facts_about(store: GraphStore, entity_ids: Sequence[str]) -> dict[str, list[Fact]]:
    """The facts about each entity, as subject or object, newest first, by entity id."""
    linked = store.neighbors_many(entity_ids, [EdgeType.SUBJECT, EdgeType.OBJECT], "in")
    return {
        entity_id: _newest_first(fact(node) for _, node in linked.get(entity_id, []))
        for entity_id in entity_ids
    }


def find_facts(store: GraphStore, words: str | None = None, limit: int = 20) -> list[Fact]:
    """Facts whose statement or subject matches ``words``, best first, or the newest facts."""
    if words:
        hits = store.search_text(words, limit=limit, labels=[Label.FACT])
        found = store.get_nodes(hit.node_id for hit in hits)
        return [fact(found[hit.node_id]) for hit in hits if hit.node_id in found]
    return _newest_first(fact(node) for node in store.find_nodes(Label.FACT))[:limit]
