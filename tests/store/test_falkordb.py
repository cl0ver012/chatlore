"""Tests for choosing a store and for the FalkorDB backend's text helpers.

The FalkorDB store itself runs the contract tests when a server is available;
these need none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chatlore.store import (
    DATABASE_NAME,
    SQLiteStore,
    describe_store,
    drop_store,
    open_store,
)

falkordb = pytest.importorskip("chatlore.store.falkordb")


def test_words_are_folded_as_they_are_indexed() -> None:
    assert falkordb.fold_words("The Café's RÉSUMÉ, customer_id 42!") == [
        "the",
        "cafe",
        "s",
        "resume",
        "customer",
        "id",
        "42",
    ]
    assert falkordb.fold_words("(draft). --") == ["draft"]
    assert falkordb.fold_words("") == []


def test_snippets_mark_matching_words_around_the_most_matches() -> None:
    text = " ".join(f"w{number}" for number in range(40)) + " Postgres index here."

    short = falkordb.snippet("Add an index on customer_id.", {"index"})
    long = falkordb.snippet(text, {"postgres", "index"})

    assert short == "Add an [index] on customer_id."
    assert long.startswith("...w26 ")
    assert long.endswith("[Postgres] [index]...")
    assert falkordb.snippet("Café au lait", {"cafe"}) == "[Café] au lait"
    assert falkordb.snippet("", {"x"}) == ""


def test_sqlite_is_the_default_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHATLORE_STORE", raising=False)

    with open_store(tmp_path) as store:
        assert isinstance(store, SQLiteStore)
    assert (tmp_path / DATABASE_NAME).exists()
    assert describe_store(tmp_path) == f"SQLite at {tmp_path / DATABASE_NAME}"

    drop_store(tmp_path)

    assert not (tmp_path / DATABASE_NAME).exists()


def test_falkordb_is_chosen_by_setting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[tuple[Any, ...]] = []

    class Fake:
        def __init__(self, *settings: Any) -> None:
            opened.append(settings)

    monkeypatch.setattr(falkordb, "FalkorDBStore", Fake)
    monkeypatch.setenv("CHATLORE_STORE", "FalkorDB")
    monkeypatch.delenv("CHATLORE_FALKORDB_URL", raising=False)
    monkeypatch.setenv("CHATLORE_FALKORDB_GRAPH", "mine")

    store = open_store(tmp_path)

    assert isinstance(store, Fake)
    assert opened == [("redis://localhost:6379", "mine")]
    assert describe_store(tmp_path) == "FalkorDB graph 'mine' at redis://localhost:6379"
    assert not (tmp_path / DATABASE_NAME).exists()


def test_an_unknown_store_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHATLORE_STORE", "neo4j")

    with pytest.raises(ValueError, match="sqlite or falkordb"):
        open_store(tmp_path)
