"""Tests for the conversation to graph mapping."""

from __future__ import annotations

from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind
from chatlore.store import EdgeType, Label
from chatlore.store.mapping import conversation_to_graph, graph_to_conversation


def _conversation() -> Conversation:
    return Conversation(
        id="conv",
        source=SourceKind.CLAUDE,
        external_id="ext",
        title="Branches",
        messages=[
            Message(id="root", role=Role.USER, content=[ContentPart(text="q")]),
            Message(
                id="a1", parent_id="root", role=Role.ASSISTANT, content=[ContentPart(text="old")]
            ),
            Message(
                id="a2", parent_id="root", role=Role.ASSISTANT, content=[ContentPart(text="new")]
            ),
        ],
        current_leaf_id="a2",
        metadata={"summary": "s"},
    )


def test_graph_shape() -> None:
    nodes, edges = conversation_to_graph(_conversation())

    assert [n.label for n in nodes] == [
        Label.CONVERSATION,
        Label.MESSAGE,
        Label.MESSAGE,
        Label.MESSAGE,
    ]
    assert nodes[0].props["message_count"] == 3
    assert nodes[0].props["source"] == "claude"
    assert nodes[2].props["text"] == "old"
    assert nodes[2].props["conversation_id"] == "conv"
    assert [(e.src, e.type, e.dst) for e in edges] == [
        ("conv", EdgeType.HAS_MESSAGE, "root"),
        ("conv", EdgeType.HAS_MESSAGE, "a1"),
        ("a1", EdgeType.REPLIES_TO, "root"),
        ("conv", EdgeType.HAS_MESSAGE, "a2"),
        ("a2", EdgeType.REPLIES_TO, "root"),
    ]


def test_round_trip_restores_order_and_branches() -> None:
    conversation = _conversation()
    nodes, _ = conversation_to_graph(conversation)

    rebuilt = graph_to_conversation(nodes[0], [nodes[3], nodes[1], nodes[2]])

    assert rebuilt == conversation
    assert [m.id for m in rebuilt.linear_messages()] == ["root", "a2"]
