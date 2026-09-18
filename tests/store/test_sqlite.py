"""SQLite-specific behaviour: persistence and query building."""

from __future__ import annotations

import contextlib
from pathlib import Path

from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind
from chatlore.store import Edge, EdgeType, Label, Node, SQLiteStore, open_store
from chatlore.store.sqlite import fts_query


def test_data_persists_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "chatlore.db"
    with SQLiteStore(path) as store:
        store.upsert_nodes([Node("n", Label.ENTITY, {"text": "persisted"})])
        store.set_embedding("n", [0.5, 0.5])

    with SQLiteStore(path) as store:
        assert store.get_node("n") is not None
        assert [h.node_id for h in store.search_text("persisted")] == ["n"]
        assert [h.node_id for h in store.search_vector([0.5, 0.5])] == ["n"]


def test_open_store_creates_the_home_directory(tmp_path: Path) -> None:
    home = tmp_path / "fresh" / "home"

    with open_store(home) as store:
        store.upsert_conversation(
            Conversation(
                id="c",
                source=SourceKind.NOTE,
                external_id="x",
                messages=[Message(id="m", role=Role.USER, content=[ContentPart(text="hi")])],
            )
        )

    assert (home / "chatlore.db").exists()


def test_failed_writes_roll_back(tmp_path: Path) -> None:
    with SQLiteStore(tmp_path / "db.sqlite") as store:
        store.upsert_nodes([Node("a", Label.ENTITY)])

        with contextlib.suppress(Exception):
            store.upsert_edges(
                [Edge("a", EdgeType.RELATED_TO, "a"), Edge("a", EdgeType.ABOUT, "nope")]
            )

        assert store.neighbors("a") == []
        assert store.count_nodes() == 1


def test_fts_query_quotes_every_word() -> None:
    assert fts_query("postgres index") == '"postgres" "index"'
    assert fts_query('  "quoted" AND (weird) ') == '"quoted" "AND" "weird"'
    assert fts_query("café") == '"café"'
    assert fts_query("...") == ""
