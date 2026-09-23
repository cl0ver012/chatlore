"""Tests for answering questions from the library."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chatlore.chat import (
    Context,
    Note,
    Source,
    answer,
    answer_messages,
    cited,
    mentioned_entities,
    retrieve,
)
from chatlore.cli import app
from chatlore.embeddings import normalise
from chatlore.extraction import ExtractionCache, entity_key
from chatlore.ids import entity_id
from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind
from chatlore.pipeline import (
    sync_chunks,
    sync_duplicates,
    sync_embeddings,
    sync_entities,
    sync_extractions,
    sync_summaries,
    sync_topics,
)
from chatlore.store import GraphStore, Label, Node, SQLiteStore
from tests.fakes import FakeEmbedder, FakeLLM

runner = CliRunner()

TEXTS = {
    "tools": [
        "Parsec keeps dropping frames on the Mac.",
        "Try Moonlight with Sunshine instead of Parsec.",
        "Claude Code writes the Tidewater tests.",
    ],
    "trip": [
        "Lisbon and Sintra make a good weekend.",
        "Take the train from Rossio to Sintra.",
    ],
}


def _conversation(conversation_id: str, texts: list[str]) -> Conversation:
    messages = [
        Message(
            id=f"{conversation_id}-{position}",
            parent_id=f"{conversation_id}-{position - 1}" if position else None,
            role=Role.USER if position % 2 == 0 else Role.ASSISTANT,
            content=[ContentPart(text=text)],
        )
        for position, text in enumerate(texts)
    ]
    return Conversation(
        id=conversation_id,
        source=SourceKind.CLAUDE,
        external_id=conversation_id,
        title=f"About {conversation_id}",
        messages=messages,
    )


def _build(store: GraphStore, embed: bool) -> None:
    conversations = [_conversation(name, texts) for name, texts in TEXTS.items()]
    for conversation in conversations:
        store.upsert_conversation(conversation)
    sync_chunks(store, conversations)
    if embed:
        sync_embeddings(store, FakeEmbedder())
    llm = FakeLLM()
    with ExtractionCache() as cache:
        sync_extractions(store, llm, cache)
        sync_entities(store, cache, llm.name)
        sync_summaries(store, llm, cache)
        sync_duplicates(store, llm, cache)
        sync_topics(store, llm, cache)


@pytest.fixture
def store() -> Iterator[GraphStore]:
    backend = SQLiteStore(":memory:")
    try:
        _build(backend, embed=False)
        yield backend
    finally:
        backend.close()


def _names(nodes: Sequence[Node]) -> list[str]:
    return [str(node.props["name"]) for node in nodes]


def _add_entity(store: GraphStore, name: str) -> None:
    """An entity with a name of several words, which the fake model never extracts."""
    props = {"name": name, "type": "tool", "summary": f"{name} is a tool.", "mentions": 1}
    store.upsert_nodes([Node(entity_id(entity_key(name)), Label.ENTITY, props)])


# -- finding what the question is about ------------------------------------------


def test_entities_named_in_the_question_are_found(store: GraphStore) -> None:
    _add_entity(store, "Claude Code")

    assert _names(mentioned_entities(store, "Why does parsec drop frames?")) == ["Parsec"]
    assert _names(mentioned_entities(store, "claude-code and TIDEWATER")) == [
        "Claude Code",
        "Tidewater",
    ]


def test_longer_names_come_first_and_common_words_never_match(store: GraphStore) -> None:
    _add_entity(store, "Claude Code")

    found = _names(mentioned_entities(store, "What did Claude Code do with the Mac?"))

    assert found[0] == "Claude Code"
    assert "Mac" in found
    assert not set(found) & {"What", "did", "do", "the"}
    assert mentioned_entities(store, "what is it about?") == []


# -- retrieval -------------------------------------------------------------------


def test_without_embeddings_passages_come_from_named_entities(store: GraphStore) -> None:
    context = retrieve(store, "How do I fix Parsec?")

    texts = [source.text for source in context.sources]
    assert texts
    assert all("Parsec" in text for text in texts)
    assert [source.number for source in context.sources] == list(range(1, len(texts) + 1))
    assert context.sources[0].title == "About tools"
    assert any(note.name == "Parsec" for note in context.notes)
    assert any(note.kind == "topic" for note in context.notes)


def test_with_embeddings_passages_found_by_meaning_join_in() -> None:
    backend = SQLiteStore(":memory:")
    try:
        _build(backend, embed=True)
        question = "a good weekend trip by train"
        context = retrieve(backend, question, normalise(FakeEmbedder().embed_query(question)))
    finally:
        backend.close()

    assert any("weekend" in source.text for source in context.sources)


def test_a_question_about_nothing_known_finds_nothing(store: GraphStore) -> None:
    assert retrieve(store, "what about quantum knitting?").empty


def test_the_number_of_passages_is_limited(store: GraphStore) -> None:
    context = retrieve(store, "Parsec Moonlight Sunshine Lisbon Sintra Rossio", limit=2)

    assert len(context.sources) == 2


# -- asking the model --------------------------------------------------------------


def _context() -> Context:
    source = Source(1, "c1", "m1", "conv", "Mac setup", "claude", "user", "2026-05-02T10:00", "Hi.")
    bare = Source(2, "c2", "m2", "conv", None, None, None, None, "Hello.")
    return Context(
        "What did I set up?",
        (source, bare),
        (Note("Parsec", "tool", "A remote desktop app."), Note("Remote access", "topic", "Macs.")),
    )


def test_the_request_numbers_sources_and_adds_notes_and_the_question() -> None:
    system, user = answer_messages(_context())

    assert "Cite every claim" in system.content
    assert user.content == (
        "### Sources\n\n[1] Mac setup (2026-05-02, user)\nHi.\n\n[2] (untitled)\nHello.\n\n"
        "### Notes from the knowledge graph\n- Parsec (tool): A remote desktop app.\n"
        "- Topic Remote access: Macs.\n\n### Question\nWhat did I set up?"
    )


def test_the_answer_streams_and_its_citations_are_read() -> None:
    context = _context()

    text = "".join(answer(FakeLLM(), context))

    assert text == "From your conversations: Mac setup [1]. (untitled) [2]. "
    assert [source.number for source in cited(text, context)] == [1, 2]
    assert cited("See [2] and [9].", context) == [context.sources[1]]


# -- the ask command -----------------------------------------------------------------


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "home"
    monkeypatch.setenv("CHATLORE_HOME", str(target))
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr("chatlore.cli.make_embedder", lambda home: FakeEmbedder())
    return target


def _library(home: Path, monkeypatch: pytest.MonkeyPatch, embed: bool) -> FakeLLM:
    for name, texts in TEXTS.items():
        runner.invoke(app, ["note", " ".join(texts), "--title", f"About {name}"])
    runner.invoke(app, ["process"] if embed else ["process", "--no-embed"])
    llm = FakeLLM()
    monkeypatch.setattr("chatlore.cli.make_llm", lambda: llm)
    runner.invoke(app, ["extract"])
    return llm


def test_ask_needs_a_library(home: Path) -> None:
    result = runner.invoke(app, ["ask", "anything?"])

    assert "Run `chatlore import` and `chatlore process`" in result.output


def test_ask_answers_with_sources(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _library(home, monkeypatch, embed=True)

    result = runner.invoke(app, ["ask", "How do I fix Parsec?"])

    assert result.exit_code == 0
    assert "From your conversations: About tools [1]." in result.output
    assert re.search(r"Sources\s+\[1\] About tools\s+note", result.output)
    assert "No embeddings yet" not in result.output
    assert "### Question\nHow do I fix Parsec?" in llm.chat_requests[-1]


def test_ask_without_embeddings_still_answers(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _library(home, monkeypatch, embed=False)

    result = runner.invoke(app, ["ask", "How do I fix Parsec?"])

    assert result.exit_code == 0
    assert "No embeddings yet" in result.output
    assert "[1] About tools" in result.output


def test_ask_says_when_nothing_matches(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _library(home, monkeypatch, embed=False)
    asked = len(llm.chat_requests)

    result = runner.invoke(app, ["ask", "what about quantum knitting?"])

    assert "Nothing in your conversations matches" in result.output
    assert len(llm.chat_requests) == asked


def test_ask_reports_a_failing_model(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _library(home, monkeypatch, embed=False)
    monkeypatch.setattr("chatlore.cli.make_llm", lambda: FakeLLM(fail_on_request=1))

    result = runner.invoke(app, ["ask", "How do I fix Parsec?"])

    assert result.exit_code == 1
    assert "fake model is unreachable" in result.output
