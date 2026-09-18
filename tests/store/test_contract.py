"""Behaviour every GraphStore backend must satisfy."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from chatlore.models import ContentPart, Conversation, Message, PartType, Role, SourceKind
from chatlore.store import Edge, EdgeType, GraphStore, Label, Node


def _conversation(
    id_: str = "conv_1",
    source: SourceKind = SourceKind.CHATGPT,
    texts: tuple[str, ...] = ("Why is Postgres slow?", "Add an index on customer_id."),
    title: str = "Postgres indexing",
    when: datetime | None = datetime(2026, 1, 5, tzinfo=UTC),
) -> Conversation:
    messages: list[Message] = []
    for position, text in enumerate(texts):
        messages.append(
            Message(
                id=f"{id_}_m{position}",
                parent_id=messages[-1].id if messages else None,
                role=Role.USER if position % 2 == 0 else Role.ASSISTANT,
                created_at=when,
                content=[ContentPart(text=text)],
            )
        )
    return Conversation(
        id=id_,
        source=source,
        external_id=f"ext-{id_}",
        title=title,
        created_at=when,
        updated_at=when,
        messages=messages,
        current_leaf_id=messages[-1].id if messages else None,
    )


# -- nodes and edges -----------------------------------------------------------


def test_nodes_round_trip_and_upsert_replaces(store: GraphStore) -> None:
    store.upsert_nodes([Node("a", Label.ENTITY, {"name": "Postgres", "tags": ["db"]})])
    store.upsert_nodes([Node("a", Label.ENTITY, {"name": "PostgreSQL"})])

    assert store.get_node("a") == Node("a", Label.ENTITY, {"name": "PostgreSQL"})
    assert store.get_node("missing") is None
    assert store.count_nodes() == 1
    assert store.count_nodes(Label.ENTITY) == 1
    assert store.count_nodes(Label.TOPIC) == 0


def test_edges_link_nodes_in_both_directions(store: GraphStore) -> None:
    store.upsert_nodes([Node("a", Label.ENTITY), Node("b", Label.ENTITY), Node("c", Label.TOPIC)])
    store.upsert_edges(
        [
            Edge("a", EdgeType.RELATED_TO, "b", {"weight": 0.5}),
            Edge("a", EdgeType.ABOUT, "c"),
            Edge("a", EdgeType.RELATED_TO, "b", {"weight": 0.9}),
        ]
    )

    out = store.neighbors("a")
    assert [(e.type, n.id) for e, n in out] == [("ABOUT", "c"), ("RELATED_TO", "b")]
    assert out[1][0].props == {"weight": 0.9}
    assert [n.id for _, n in store.neighbors("b", direction="in")] == ["a"]
    assert [n.id for _, n in store.neighbors("a", [EdgeType.ABOUT])] == ["c"]
    assert len(store.neighbors("a", direction="both")) == 2
    assert store.neighbors("a", limit=1) == out[:1]


def test_edges_require_both_endpoints(store: GraphStore) -> None:
    store.upsert_nodes([Node("a", Label.ENTITY)])

    with pytest.raises(Exception, match=r"FOREIGN KEY|constraint"):
        store.upsert_edges([Edge("a", EdgeType.RELATED_TO, "ghost")])


def test_deleting_a_node_removes_its_edges_and_index_entries(store: GraphStore) -> None:
    store.upsert_nodes([Node("a", Label.ENTITY, {"text": "orphan text"}), Node("b", Label.ENTITY)])
    store.upsert_edges([Edge("a", EdgeType.RELATED_TO, "b")])
    store.set_embedding("a", [1.0, 0.0])

    store.delete_nodes(["a"])

    assert store.get_node("a") is None
    assert store.neighbors("b", direction="in") == []
    assert store.search_text("orphan") == []
    assert store.search_vector([1.0, 0.0]) == []


# -- conversations -------------------------------------------------------------


def test_conversations_round_trip(store: GraphStore) -> None:
    conversation = _conversation()
    conversation.messages[1].content.append(
        ContentPart(type=PartType.CODE, language="sql", text="CREATE INDEX ...")
    )

    store.upsert_conversation(conversation)

    assert store.get_conversation("conv_1") == conversation
    assert store.get_conversation("missing") is None
    assert store.count_nodes(Label.CONVERSATION) == 1
    assert store.count_nodes(Label.MESSAGE) == 2
    replies = store.neighbors(conversation.messages[1].id, [EdgeType.REPLIES_TO])
    assert [n.id for _, n in replies] == [conversation.messages[0].id]


def test_upserting_a_conversation_replaces_removed_messages(store: GraphStore) -> None:
    store.upsert_conversation(_conversation(texts=("one", "two", "three")))
    store.upsert_conversation(_conversation(texts=("one", "two changed")))

    rebuilt = store.get_conversation("conv_1")
    assert rebuilt is not None
    assert [m.text for m in rebuilt.messages] == ["one", "two changed"]
    assert store.count_nodes(Label.MESSAGE) == 2
    assert store.search_text("three") == []


def test_list_and_delete_conversations(store: GraphStore) -> None:
    old = _conversation("conv_old", when=datetime(2025, 1, 1, tzinfo=UTC))
    new = _conversation("conv_new", SourceKind.CLAUDE, when=datetime(2026, 1, 1, tzinfo=UTC))
    undated = _conversation("conv_undated", when=None)
    for conversation in (old, new, undated):
        store.upsert_conversation(conversation)

    assert [c.id for c in store.list_conversations()] == ["conv_new", "conv_old", "conv_undated"]
    assert [c.id for c in store.list_conversations(source="claude")] == ["conv_new"]
    assert [c.id for c in store.list_conversations(limit=1, offset=1)] == ["conv_old"]
    summary = store.list_conversations(source="claude")[0]
    assert (summary.title, summary.message_count, summary.source) == (
        "Postgres indexing",
        2,
        "claude",
    )

    store.delete_conversation("conv_new")

    assert store.get_conversation("conv_new") is None
    assert store.count_nodes(Label.MESSAGE) == 4
    assert store.search_text("Postgres", sources=["claude"]) == []


# -- search --------------------------------------------------------------------


def test_text_search_finds_messages_and_filters(store: GraphStore) -> None:
    store.upsert_conversation(_conversation("conv_a"))
    store.upsert_conversation(
        _conversation("conv_b", SourceKind.CLAUDE, ("Name my cat", "Biscuit"), "Cat names")
    )

    hits = store.search_text("postgres index")
    assert len(hits) == 1
    assert hits[0].conversation_id == "conv_a"
    assert hits[0].label == Label.MESSAGE
    assert hits[0].title == "Postgres indexing"
    assert "[index]" in hits[0].snippet
    assert hits[0].score > 0

    assert store.search_text("cat") != []
    assert store.search_text("cat", sources=["chatgpt"]) == []
    assert store.search_text("cat", labels=[Label.CONVERSATION]) == []
    assert store.search_text("") == []
    assert store.search_text("nothing matches this") == []


def test_text_search_is_forgiving_about_accents_and_punctuation(store: GraphStore) -> None:
    store.upsert_nodes([Node("n", Label.ENTITY, {"text": "The café's résumé (draft)."})])

    assert [h.node_id for h in store.search_text("cafe resume")] == ["n"]
    assert [h.node_id for h in store.search_text('"(draft)." NOT')] == []
    assert [h.node_id for h in store.search_text("(draft).")] == ["n"]


def test_vector_search_returns_nearest_first(store: GraphStore) -> None:
    store.upsert_nodes([Node("x", Label.CHUNK), Node("y", Label.CHUNK), Node("z", Label.ENTITY)])
    store.set_embedding("x", [1.0, 0.0, 0.0])
    store.set_embedding("y", [0.0, 1.0, 0.0])
    store.set_embedding("z", [0.9, 0.1, 0.0])
    store.set_embedding("y", [0.0, 0.0, 1.0])  # replaced

    hits = store.search_vector([1.0, 0.0, 0.0], limit=2)
    assert [h.node_id for h in hits] == ["x", "z"]
    assert hits[0].distance == pytest.approx(0.0)
    assert [h.node_id for h in store.search_vector([1.0, 0.0, 0.0], labels=[Label.CHUNK])] == [
        "x",
        "y",
    ]


def test_vector_dimension_is_fixed_after_first_write(store: GraphStore) -> None:
    assert store.search_vector([1.0, 2.0]) == []
    store.upsert_nodes([Node("x", Label.CHUNK)])
    store.set_embedding("x", [1.0, 2.0])

    with pytest.raises(ValueError, match="dimensions"):
        store.set_embedding("x", [1.0, 2.0, 3.0])
    with pytest.raises(ValueError, match="dimensions"):
        store.search_vector([1.0])
    with pytest.raises(ValueError, match="empty"):
        store.set_embedding("x", [])
    with pytest.raises(KeyError):
        store.set_embedding("ghost", [1.0, 2.0])
