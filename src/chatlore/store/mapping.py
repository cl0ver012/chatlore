"""Conversions between conversations and graph nodes and edges.

These are pure functions shared by every backend, so a conversation looks the
same in SQLite and in FalkorDB.
"""

from __future__ import annotations

from typing import Any

from chatlore.chunking import Chunk
from chatlore.models import Conversation, Message
from chatlore.store.base import Edge, EdgeType, Label, Node


def conversation_to_graph(conversation: Conversation) -> tuple[list[Node], list[Edge]]:
    """Return the nodes and edges that represent ``conversation``."""
    source = conversation.source.value
    nodes = [
        Node(
            id=conversation.id,
            label=Label.CONVERSATION,
            props={
                "conversation": conversation.model_dump(mode="json", exclude={"messages"}),
                "source": source,
                "title": conversation.title,
                "created_at": _iso(conversation.created_at),
                "updated_at": _iso(conversation.updated_at),
                "message_count": len(conversation.messages),
                "content_hash": conversation.content_hash(),
            },
        )
    ]
    edges: list[Edge] = []
    for position, message in enumerate(conversation.messages):
        nodes.append(
            Node(
                id=message.id,
                label=Label.MESSAGE,
                props={
                    "message": message.model_dump(mode="json"),
                    "position": position,
                    "conversation_id": conversation.id,
                    "source": source,
                    "title": conversation.title,
                    "role": message.role.value,
                    "created_at": _iso(message.created_at),
                    "text": message.text,
                },
            )
        )
        edges.append(
            Edge(
                src=conversation.id,
                type=EdgeType.HAS_MESSAGE,
                dst=message.id,
                props={"position": position},
            )
        )
        if message.parent_id is not None:
            edges.append(Edge(src=message.id, type=EdgeType.REPLIES_TO, dst=message.parent_id))
    return nodes, edges


def graph_to_conversation(conversation_node: Node, message_nodes: list[Node]) -> Conversation:
    """Rebuild a conversation from its node and its message nodes in any order."""
    ordered = sorted(message_nodes, key=lambda node: int(node.props["position"]))
    messages = [Message.model_validate(node.props["message"]) for node in ordered]
    data: dict[str, Any] = dict(conversation_node.props["conversation"])
    data["messages"] = [message.model_dump(mode="json") for message in messages]
    return Conversation.model_validate(data)


def chunks_to_graph(
    conversation: Conversation, chunks: list[Chunk]
) -> tuple[list[Node], list[Edge]]:
    """Return the nodes and edges that attach ``chunks`` to their messages."""
    by_id = {message.id: message for message in conversation.messages}
    nodes: list[Node] = []
    edges: list[Edge] = []
    for chunk in chunks:
        message = by_id[chunk.message_id]
        nodes.append(
            Node(
                id=chunk.id,
                label=Label.CHUNK,
                props={
                    "text": chunk.text,
                    "conversation_id": conversation.id,
                    "message_id": chunk.message_id,
                    "source": conversation.source.value,
                    "title": conversation.title,
                    "role": message.role.value,
                    "created_at": _iso(message.created_at),
                    "order": chunk.order,
                    "token_estimate": chunk.token_estimate,
                },
            )
        )
        edges.append(
            Edge(
                src=chunk.message_id,
                type=EdgeType.HAS_CHUNK,
                dst=chunk.id,
                props={"order": chunk.order},
            )
        )
    return nodes, edges


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None
