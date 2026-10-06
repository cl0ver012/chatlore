"""Tests for facts: how they are read from the model, built into the graph, and shown."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from typer.testing import CliRunner

from chatlore.api import create_app
from chatlore.chat import Note, retrieve
from chatlore.cli import app as cli
from chatlore.embeddings import text_hash
from chatlore.extraction import (
    ExtractedEntity,
    ExtractedFact,
    Extraction,
    ExtractionCache,
    extraction_messages,
    fact_key,
    parse_extractions,
)
from chatlore.facts import facts_about, find_facts
from chatlore.ids import entity_id
from chatlore.mcp_server import create_server
from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind
from chatlore.pipeline import pending_extractions, sync_chunks, sync_entities, sync_extractions
from chatlore.store import EdgeType, GraphStore, Label, SQLiteStore
from tests.fakes import FakeEmbedder, FakeLLM

runner = CliRunner()

BACKUPS = "after a scare, we decided Litestream backs up Postgres every hour."


def _conversation(conversation_id: str, *texts: str, day: int = 1) -> Conversation:
    return Conversation(
        id=conversation_id,
        source=SourceKind.CHATGPT,
        external_id=conversation_id,
        title=f"Title {conversation_id}",
        messages=[
            Message(
                id=f"{conversation_id}-{position}",
                parent_id=f"{conversation_id}-{position - 1}" if position else None,
                role=Role.USER if position % 2 == 0 else Role.ASSISTANT,
                content=[ContentPart(text=text)],
                created_at=f"2026-03-{day:02d}T10:00:00Z",
            )
            for position, text in enumerate(texts)
        ],
    )


@pytest.fixture
def store() -> Iterator[GraphStore]:
    backend = SQLiteStore(":memory:")
    try:
        yield backend
    finally:
        backend.close()


@pytest.fixture
def cache() -> Iterator[ExtractionCache]:
    with ExtractionCache() as backend:
        yield backend


def _build(store: GraphStore, cache: ExtractionCache, *conversations: Conversation) -> None:
    for conversation in conversations:
        store.upsert_conversation(conversation)
    sync_chunks(store, conversations)
    sync_extractions(store, FakeLLM(), cache)
    sync_entities(store, cache, "fake-llm")


# -- reading the model's answer ------------------------------------------------


def test_the_request_asks_for_facts() -> None:
    system = extraction_messages(["text"])[0].content

    assert '"facts"' in system and "self-contained" in system


def test_facts_are_read_with_their_subject_and_object() -> None:
    answer = json.dumps(
        {
            "passages": [
                {
                    "passage": 1,
                    "entities": [
                        {"name": "Postgres", "type": "tool", "description": "A database."},
                        {"name": "Litestream", "type": "tool", "description": "Backups."},
                    ],
                    "facts": [
                        {
                            "subject": "litestream",
                            "statement": "Litestream backs up Postgres hourly.",
                            "object": "Postgres",
                        },
                        {"subject": "Postgres", "statement": "Postgres runs on port 5433."},
                        {"subject": "Postgres", "statement": "postgres runs on port 5433"},
                        {"subject": "MySQL", "statement": "MySQL was dropped."},
                        {"subject": "Postgres", "statement": "", "object": "Litestream"},
                        {"subject": "Postgres", "statement": "Version 17.", "object": "Redis"},
                        "not a fact",
                    ],
                }
            ]
        }
    )

    (found,) = parse_extractions(answer, 1)

    assert found is not None
    assert found.facts == (
        ExtractedFact("Litestream", "Litestream backs up Postgres hourly.", "Postgres"),
        ExtractedFact("Postgres", "Postgres runs on port 5433."),
        ExtractedFact("Postgres", "Version 17."),
    )


def test_statements_differing_only_in_form_are_one_fact() -> None:
    assert fact_key("Postgres runs on port 5433.") == fact_key("  postgres runs ON port 5433 ")
    assert fact_key("Postgres runs on 5433") != fact_key("Postgres runs on 5432")


def test_facts_survive_the_cache_and_older_answers_still_load(cache: ExtractionCache) -> None:
    extraction = Extraction(
        (ExtractedEntity("Postgres", "tool", "A database."),),
        facts=(ExtractedFact("Postgres", "Postgres runs on port 5433."),),
    )
    cache.put_many("model", {"new": extraction})

    assert cache.get_many("model", ["new"]) == {"new": extraction}
    assert Extraction.from_json('{"entities": [], "relationships": []}') == Extraction()


def _put_earlier(cache: ExtractionCache, digest: str, extraction: Extraction) -> None:
    connection: sqlite3.Connection = cache._connection
    connection.execute(
        "INSERT INTO extractions VALUES (?, ?, ?, ?)",
        ("fake-llm", "1", digest, extraction.to_json()),
    )
    connection.commit()


def test_chunks_read_with_an_earlier_prompt_keep_their_entities_until_read_again(
    store: GraphStore, cache: ExtractionCache
) -> None:
    conversation = _conversation("one", BACKUPS)
    store.upsert_conversation(conversation)
    sync_chunks(store, [conversation])
    (chunk,) = store.find_nodes(Label.CHUNK)
    earlier = Extraction((ExtractedEntity("Postgres", "tool", "A database."),))
    _put_earlier(cache, text_hash(str(chunk.props["text"])), earlier)

    pending = pending_extractions(store, cache, "fake-llm")
    before = sync_entities(store, cache, "fake-llm")
    sync_extractions(store, FakeLLM(), cache)
    after = sync_entities(store, cache, "fake-llm")

    assert [node.id for node in pending] == [chunk.id]  # read again, to find its facts
    assert (before.entities, before.facts) == (1, 0)
    assert (after.entities, after.facts) == (2, 1)


# -- the graph -----------------------------------------------------------------


def test_a_fact_links_to_its_entities_and_the_message_that_said_it(
    store: GraphStore, cache: ExtractionCache
) -> None:
    _build(store, cache, _conversation("one", "Hello there.", BACKUPS))

    (node,) = store.find_nodes(Label.FACT)
    links = {(edge.type, other.id) for edge, other in store.neighbors(node.id)}

    assert node.props["statement"] == BACKUPS
    assert node.props["subject"] == "Litestream" and node.props["object"] == "Postgres"
    assert links == {
        (EdgeType.SUBJECT, entity_id("litestream")),
        (EdgeType.OBJECT, entity_id("postgre")),
        (EdgeType.ASSERTED_IN, "one-1"),
    }
    assert node.props["sources"][0]["conversation_id"] == "one"
    assert node.props["sources"][0]["created_at"].startswith("2026-03-01")


def test_one_statement_said_twice_is_one_fact_with_both_sources(
    store: GraphStore, cache: ExtractionCache
) -> None:
    _build(
        store,
        cache,
        _conversation("one", BACKUPS, day=1),
        _conversation("two", BACKUPS, day=9),
    )

    (fact,) = find_facts(store)

    assert fact.said == 2
    assert [source.conversation_id for source in fact.sources] == ["two", "one"]  # newest first
    assert fact.date == "2026-03-09"


def test_facts_follow_the_text_after_a_re_import(store: GraphStore, cache: ExtractionCache) -> None:
    _build(store, cache, _conversation("one", BACKUPS))
    _build(store, cache, _conversation("one", "Nothing was settled about Postgres."))

    assert store.find_nodes(Label.FACT) == []


def test_facts_about_an_entity_include_those_it_is_the_object_of(
    store: GraphStore, cache: ExtractionCache
) -> None:
    _build(
        store,
        cache,
        _conversation("one", BACKUPS, day=1),
        _conversation("two", "we decided Postgres gets version seventeen.", day=5),
    )

    about = facts_about(store, [entity_id("postgre")])[entity_id("postgre")]

    assert [fact.subject for fact in about] == ["Postgres", "Litestream"]  # newest first


def test_facts_are_found_by_their_words(store: GraphStore, cache: ExtractionCache) -> None:
    _build(store, cache, _conversation("one", BACKUPS, "we decided Caddy serves HTTPS."))

    assert [fact.subject for fact in find_facts(store, "hour")] == ["Litestream"]
    assert len(find_facts(store)) == 2


def test_chat_gets_the_facts_about_the_entities_a_question_names(
    store: GraphStore, cache: ExtractionCache
) -> None:
    _build(store, cache, _conversation("one", BACKUPS))

    context = retrieve(store, "how is Litestream set up?")

    assert Note("Litestream", "fact", f"{BACKUPS} (said 2026-03-01)") in context.notes


# -- the command line, the API, and MCP ----------------------------------------


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "home"
    monkeypatch.setenv("CHATLORE_HOME", str(target))
    monkeypatch.setenv("COLUMNS", "200")
    for module in ("cli", "api", "mcp_server"):
        monkeypatch.setattr(f"chatlore.{module}.make_embedder", lambda home: FakeEmbedder())
    monkeypatch.setattr("chatlore.cli.make_llm", FakeLLM)
    runner.invoke(cli, ["note", BACKUPS, "--title", "Backups"])
    runner.invoke(cli, ["process"])
    result = runner.invoke(cli, ["extract"])
    assert "facts" in result.output
    return target


def test_the_facts_command_lists_facts_with_where_they_were_said(home: Path) -> None:
    listed = runner.invoke(cli, ["facts"])
    searched = runner.invoke(cli, ["facts", "nothing like this"])

    assert "Litestream: after a scare, we decided Litestream backs up Postgres" in listed.output
    assert "Backups" in listed.output
    assert "No facts mention those words." in searched.output


def test_the_entity_command_shows_its_facts(home: Path) -> None:
    result = runner.invoke(cli, ["entity", "Postgres"])

    assert "Facts" in result.output
    assert "we decided Litestream backs up Postgres" in result.output


def test_the_api_serves_facts(home: Path) -> None:
    with TestClient(create_app(home)) as client:
        listed = client.get("/facts").json()
        found = client.get("/facts", params={"q": "hour"}).json()
        entity = client.get(f"/entities/{entity_id('litestream')}").json()
        stats = client.get("/stats").json()

    assert [fact["statement"] for fact in listed] == [BACKUPS]
    assert found == listed
    assert entity["facts"][0]["sources"][0]["title"] == "Backups"
    assert stats["facts"] == 1


def _call(home: Path, tool: str, **arguments: Any) -> str:
    async def run() -> str:
        async with Client(create_server(home)) as client:
            result = await client.call_tool(tool, arguments)
        return "\n".join(part.text for part in result.content if part.type == "text")

    return anyio.run(run)


def test_mcp_tools_show_facts_with_the_message_that_said_them(home: Path) -> None:
    listed = _call(home, "facts")
    entity = _call(home, "entity", name="Litestream")

    assert listed.startswith(f"- Litestream: {BACKUPS} (Backups, ")
    assert "message " in listed
    assert "Facts:" in entity and BACKUPS in entity
