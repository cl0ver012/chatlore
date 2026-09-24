"""Tests for the MCP server."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from typer.testing import CliRunner

from chatlore import mcp_server
from chatlore.cli import app as cli
from chatlore.mcp_server import create_server
from chatlore.models import ContentPart, Message, Role
from chatlore.store import Label, open_store
from tests.fakes import FakeEmbedder, FakeLLM

runner = CliRunner()

NOTES = {
    "Mac tools": "Parsec keeps dropping frames on the Mac. Try Moonlight with Sunshine instead.",
    "Weekend": "Lisbon and Sintra make a good weekend. Take the train from Rossio to Sintra.",
}


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "home"
    monkeypatch.setenv("CHATLORE_HOME", str(target))
    monkeypatch.setattr("chatlore.cli.make_embedder", lambda home: FakeEmbedder())
    monkeypatch.setattr("chatlore.mcp_server.make_embedder", lambda home: FakeEmbedder())
    monkeypatch.setattr("chatlore.cli.make_llm", FakeLLM)
    return target


@pytest.fixture
def server(home: Path) -> MCPServer:
    for title, text in NOTES.items():
        runner.invoke(cli, ["note", text, "--title", title])
    runner.invoke(cli, ["process"])
    runner.invoke(cli, ["extract"])
    return create_server(home)


def call(server: MCPServer, tool: str, **arguments: Any) -> tuple[str, bool]:
    """Call a tool through an MCP client, as an assistant would: its text and whether it failed."""

    async def run() -> tuple[str, bool]:
        async with Client(server) as client:
            result = await client.call_tool(tool, arguments)
        text = "\n".join(part.text for part in result.content if part.type == "text")
        return text, bool(result.is_error)

    return anyio.run(run)


def test_the_tools_are_listed_read_only_with_instructions(server: MCPServer) -> None:
    async def run() -> tuple[str | None, list[Any]]:
        async with Client(server) as client:
            return client.instructions, (await client.list_tools()).tools

    instructions, tools = anyio.run(run)

    assert instructions and "ask_context" in instructions
    assert [tool.name for tool in tools] == [
        "search",
        "ask_context",
        "entity",
        "topics",
        "topic",
        "conversation",
    ]
    assert all(tool.annotations and tool.annotations.read_only_hint for tool in tools)


def test_ask_context_numbers_the_passages_with_their_ids_and_notes(server: MCPServer) -> None:
    text, failed = call(server, "ask_context", question="which train goes from Rossio to Sintra")

    assert not failed
    assert "cite them like [2]" in text
    assert "[1] Weekend (note" in text
    assert "Take the train from Rossio to Sintra." in text
    assert "conversation conv_" in text and "message msg_" in text
    assert "Background from the knowledge graph" in text


def test_the_tools_say_when_the_library_is_empty(home: Path) -> None:
    empty = create_server(home)

    assert "library is empty" in call(empty, "ask_context", question="anything")[0]
    assert "library is empty" in call(empty, "search", query="anything")[0]
    assert "No topics yet" in call(empty, "topics")[0]


def test_search_by_words_meaning_and_source(server: MCPServer) -> None:
    text, failed = call(server, "search", query="Sintra", limit=5)
    other, _ = call(server, "search", query="Sintra", source="chatgpt")

    assert not failed
    first = text.split("\n\n")[1]
    assert first.startswith("1. Weekend (note; matched by words, meaning")
    assert "[Sintra]" in first
    assert "No messages match" in other


def test_search_without_embeddings_matches_words(home: Path) -> None:
    runner.invoke(cli, ["note", NOTES["Weekend"], "--title", "Weekend"])

    text, failed = call(create_server(home), "search", query="Rossio")

    assert not failed
    assert "Weekend (note; matched by words)" in text


def test_entity_by_name_or_id_and_unknown(server: MCPServer) -> None:
    text, failed = call(server, "entity", name="parsec")
    entity_id = text.split("id ", 1)[1].split(")", 1)[0]
    again, _ = call(server, "entity", name=entity_id)
    missing, missing_failed = call(server, "entity", name="nothing like this")

    assert not failed
    assert text.startswith("Parsec (")
    assert "Mentioned in:" in text and "Mac tools" in text
    assert again == text
    assert missing_failed
    assert "No entity named" in missing


def test_topics_and_a_topic_by_id_or_title(server: MCPServer) -> None:
    listed, _ = call(server, "topics")
    topic_id = listed.split("id ", 1)[1].split(")", 1)[0]
    title = listed.split(" (", 1)[0]
    by_id, failed = call(server, "topic", topic=topic_id)
    by_title, _ = call(server, "topic", topic=title)
    missing, missing_failed = call(server, "topic", topic="topic_missing")

    assert not failed
    assert by_id.startswith(f"{title} (")
    assert "Entities:" in by_id
    assert by_title == by_id
    assert missing_failed
    assert "No topic" in missing


def test_conversation_is_read_whole_or_around_a_message(
    server: MCPServer, home: Path, fixtures: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(cli, ["import", str(fixtures / "claude" / "conversations.json")])
    with open_store(home) as graph:
        summary = max(graph.list_conversations(limit=100), key=lambda item: item.message_count)
        found = graph.get_conversation(summary.id)
    assert found is not None and len(found.linear_messages()) >= 3
    last = found.linear_messages()[-1]

    whole, failed = call(server, "conversation", conversation_id=summary.id)
    monkeypatch.setattr(mcp_server, "MAX_CONVERSATION_CHARS", len(last.text))
    part, _ = call(server, "conversation", conversation_id=summary.id, message_id=last.id)
    missing, missing_failed = call(server, "conversation", conversation_id="conv_missing")

    assert not failed
    assert whole.count("(message ") == len(found.linear_messages())
    assert "Showing" not in whole
    assert part.count("(message ") == 1
    assert f"(message {last.id})" in part
    assert "Pass the id of a message outside them" in part
    assert missing_failed
    assert "No conversation" in missing


def test_window_grows_around_the_message_while_it_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [
        Message(id=f"m{i}", role=Role.USER, content=[ContentPart(text="x" * size)])
        for i, size in enumerate([10, 10, 10, 50, 10])
    ]
    monkeypatch.setattr(mcp_server, "MAX_CONVERSATION_CHARS", 30)

    assert mcp_server._window(messages, 1) == (0, 3)
    assert mcp_server._window(messages, 4) == (4, 5)
    assert mcp_server._window(messages, 3) == (3, 4)


def test_the_mcp_command_serves_over_standard_input_and_output(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[bool] = []

    class Fake:
        def run(self) -> None:
            served.append(True)

    monkeypatch.setattr("chatlore.mcp_server.create_server", lambda home=None: Fake())

    result = runner.invoke(cli, ["mcp"])

    assert result.exit_code == 0
    assert result.output == ""
    assert served == [True]


def test_the_library_is_only_read(server: MCPServer, home: Path) -> None:
    with open_store(home) as graph:
        before = graph.count_nodes(Label.CHUNK), graph.count_nodes(Label.ENTITY)
    for tool, arguments in [
        ("search", {"query": "Parsec"}),
        ("ask_context", {"question": "What about Parsec?"}),
        ("topics", {}),
    ]:
        call(server, tool, **arguments)
    with open_store(home) as graph:
        assert (graph.count_nodes(Label.CHUNK), graph.count_nodes(Label.ENTITY)) == before
