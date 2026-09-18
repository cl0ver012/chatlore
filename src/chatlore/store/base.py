"""The graph store interface.

A store keeps nodes and edges, a full-text index over nodes that carry text,
and a vector index over nodes that carry an embedding. Backends implement this
one interface, and everything above it (importing, search, extraction, the API)
talks only to the interface, so SQLite and FalkorDB are interchangeable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Self

from chatlore.models import Conversation


class Label(StrEnum):
    """Node labels used across the graph."""

    CONVERSATION = "Conversation"
    MESSAGE = "Message"
    CHUNK = "Chunk"
    ENTITY = "Entity"
    TOPIC = "Topic"
    FACT = "Fact"


class EdgeType(StrEnum):
    """Edge types used across the graph."""

    HAS_MESSAGE = "HAS_MESSAGE"
    REPLIES_TO = "REPLIES_TO"
    HAS_CHUNK = "HAS_CHUNK"
    MENTIONS = "MENTIONS"
    ABOUT = "ABOUT"
    ASSERTED_IN = "ASSERTED_IN"
    SUBJECT = "SUBJECT"
    OBJECT = "OBJECT"
    SUPERSEDES = "SUPERSEDES"
    RELATED_TO = "RELATED_TO"
    SAME_AS = "SAME_AS"


Direction = Literal["out", "in", "both"]


@dataclass(frozen=True, slots=True)
class Node:
    """A node. ``props`` must be JSON-serialisable.

    Two props are special: a string ``text`` is added to the full-text index,
    and ``conversation_id``, ``source``, and ``title`` are stored alongside it
    so search results can be filtered and displayed without extra lookups.
    """

    id: str
    label: str
    props: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed, typed edge. At most one edge of a type exists between two nodes."""

    src: str
    type: str
    dst: str
    props: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TextHit:
    """A full-text search result."""

    node_id: str
    label: str
    conversation_id: str | None
    source: str | None
    title: str | None
    snippet: str
    score: float


@dataclass(frozen=True, slots=True)
class VectorHit:
    """A vector search result. Lower distance is closer."""

    node_id: str
    label: str
    distance: float


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """Enough about a conversation to list it."""

    id: str
    source: str
    title: str | None
    created_at: str | None
    updated_at: str | None
    message_count: int


class GraphStore(ABC):
    """What every backend provides."""

    # -- lifecycle -----------------------------------------------------------

    @abstractmethod
    def close(self) -> None:
        """Release the underlying connection."""

    def __enter__(self) -> Self:
        return self

    @abstractmethod
    def transaction(self) -> AbstractContextManager[Any]:
        """Group many writes into one transaction. Nested calls join the outer one."""

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- nodes and edges -----------------------------------------------------

    @abstractmethod
    def upsert_nodes(self, nodes: Iterable[Node]) -> None:
        """Insert or replace nodes, keeping their indexes in step."""

    @abstractmethod
    def upsert_edges(self, edges: Iterable[Edge]) -> None:
        """Insert or replace edges. Both endpoints must exist."""

    @abstractmethod
    def get_node(self, node_id: str) -> Node | None:
        """Return a node, or ``None`` when it does not exist."""

    @abstractmethod
    def delete_nodes(self, node_ids: Iterable[str]) -> None:
        """Delete nodes together with their edges and index entries."""

    @abstractmethod
    def neighbors(
        self,
        node_id: str,
        edge_types: Sequence[str] | None = None,
        direction: Direction = "out",
        limit: int = 100,
    ) -> list[tuple[Edge, Node]]:
        """Return edges touching ``node_id`` with the node at the other end."""

    @abstractmethod
    def find_nodes(
        self, label: str, where: Mapping[str, Any] | None = None, limit: int = 1_000_000
    ) -> list[Node]:
        """Return nodes of ``label`` whose props equal every entry of ``where``."""

    @abstractmethod
    def count_nodes(self, label: str | None = None) -> int:
        """Return how many nodes exist, optionally of one label."""

    # -- conversations -------------------------------------------------------

    @abstractmethod
    def upsert_conversation(self, conversation: Conversation) -> None:
        """Store a conversation and its messages, replacing any earlier version.

        Messages that are still present keep their chunks and embeddings. Messages
        that disappeared are deleted together with their chunks.
        """

    @abstractmethod
    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """Rebuild a conversation from the graph."""

    @abstractmethod
    def list_conversations(
        self, source: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[ConversationSummary]:
        """List conversations, newest first."""

    @abstractmethod
    def delete_conversation(self, conversation_id: str) -> None:
        """Delete a conversation and everything hanging off it."""

    # -- search --------------------------------------------------------------

    @abstractmethod
    def search_text(
        self,
        query: str,
        limit: int = 20,
        sources: Sequence[str] | None = None,
        labels: Sequence[str] | None = None,
    ) -> list[TextHit]:
        """Full-text search over indexed nodes, best matches first."""

    @abstractmethod
    def set_embedding(self, node_id: str, embedding: Sequence[float]) -> None:
        """Attach an embedding to a node. All embeddings share one dimension."""

    @abstractmethod
    def nodes_without_embedding(self, label: str, limit: int = 100) -> list[Node]:
        """Return up to ``limit`` nodes of ``label`` that have no embedding yet."""

    @abstractmethod
    def count_embeddings(self) -> int:
        """Return how many nodes have an embedding."""

    @abstractmethod
    def clear_embeddings(self) -> None:
        """Drop every embedding, for example before switching to another model."""

    @abstractmethod
    def get_meta(self, key: str) -> str | None:
        """Read a small piece of store-wide state, such as the embedding model name."""

    @abstractmethod
    def set_meta(self, key: str, value: str) -> None:
        """Write a small piece of store-wide state."""

    @abstractmethod
    def search_vector(
        self, embedding: Sequence[float], limit: int = 20, labels: Sequence[str] | None = None
    ) -> list[VectorHit]:
        """Nearest nodes to ``embedding``, closest first."""
