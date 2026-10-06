"""Tests for exploring the library as a graph of entities and the chats they came up in."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from chatlore.api import create_app
from chatlore.cli import app as cli
from tests.fakes import FakeEmbedder, FakeLLM

runner = CliRunner()

NOTES = {
    "Mac tools": "Parsec keeps dropping frames on the Mac. Try Moonlight with Sunshine instead.",
    "Streaming games": "Moonlight with Sunshine streams games from the Linux box to the Mac.",
    "Weekend": "Lisbon and Sintra make a good weekend. Take the train from Rossio to Sintra.",
}
THIS_MONTH = datetime.now(UTC).strftime("%Y-%m")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    home = tmp_path / "home"
    monkeypatch.setenv("CHATLORE_HOME", str(home))
    monkeypatch.setattr("chatlore.cli.make_embedder", lambda home: FakeEmbedder())
    monkeypatch.setattr("chatlore.api.make_embedder", lambda home: FakeEmbedder())
    monkeypatch.setattr("chatlore.cli.make_llm", FakeLLM)
    for title, text in NOTES.items():
        runner.invoke(cli, ["note", text, "--title", title])
    runner.invoke(cli, ["process"])
    runner.invoke(cli, ["extract"])
    with TestClient(create_app(home)) as test_client:
        yield test_client


def _by_kind(view: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {node["id"]: node for node in view["nodes"] if node["kind"] == kind}


def _named(view: dict[str, Any], name: str) -> dict[str, Any]:
    return next(node for node in view["nodes"] if node.get("name") == name)


def _titled(view: dict[str, Any], title: str) -> dict[str, Any]:
    return next(node for node in view["nodes"] if node.get("title") == title)


def test_the_overview_draws_entities_and_the_chats_they_came_up_in(client: TestClient) -> None:
    view = client.get("/explore").json()
    entities, chats = _by_kind(view, "entity"), _by_kind(view, "conversation")

    assert view["focus"] is None
    assert {chat["title"] for chat in chats.values()} == set(NOTES)
    assert {"Moonlight", "Sintra"} <= {entity["name"] for entity in entities.values()}
    mentions = {(e["source"], e["target"]) for e in view["edges"] if e["kind"] == "mentions"}
    assert (_named(view, "Sintra")["id"], _titled(view, "Weekend")["id"]) in mentions
    assert all(
        e["source"] in entities and e["target"] in chats
        for e in view["edges"]
        if e["kind"] == "mentions"
    )
    assert all(e["target"] in entities for e in view["edges"] if e["kind"] == "related")
    assert all(chat["source"] == "note" and chat["created_at"] for chat in chats.values())


def test_an_entity_brings_its_related_entities_and_the_chats_that_mention_it(
    client: TestClient,
) -> None:
    moonlight = _named(client.get("/explore").json(), "Moonlight")

    view = client.get("/explore", params={"entity": moonlight["id"]}).json()
    chats = {chat["title"] for chat in _by_kind(view, "conversation").values()}

    assert view["focus"] == moonlight["id"]
    assert view["nodes"][0]["id"] == moonlight["id"]
    assert chats == {"Mac tools", "Streaming games"}
    assert "Sunshine" in {entity["name"] for entity in _by_kind(view, "entity").values()}


def test_a_chat_brings_its_entities_and_the_chats_that_share_them(client: TestClient) -> None:
    mac = _titled(client.get("/explore").json(), "Mac tools")

    view = client.get("/explore", params={"conversation": mac["id"]}).json()
    entities = {entity["name"] for entity in _by_kind(view, "entity").values()}
    chats = [chat["title"] for chat in _by_kind(view, "conversation").values()]

    assert view["focus"] == mac["id"]
    assert {"Parsec", "Moonlight", "Sunshine", "Mac"} <= entities
    assert "Sintra" not in entities
    assert chats[0] == "Mac tools" and "Streaming games" in chats and "Weekend" not in chats


def test_a_topic_brings_its_entities(client: TestClient) -> None:
    topic = client.get("/topics").json()[0]

    view = client.get("/explore", params={"topic": topic["id"]}).json()

    assert {entity["name"] for entity in _by_kind(view, "entity").values()} <= set(
        client.get(f"/topics/{topic['id']}").json()["entities"]
    )
    assert _by_kind(view, "conversation")


def test_the_window_keeps_only_some_chats(client: TestClient) -> None:
    mac = _titled(client.get("/explore").json(), "Mac tools")

    other_source = client.get("/explore", params={"source": "chatgpt"}).json()
    future = client.get("/explore", params={"since": "2999-01"}).json()
    this_month = client.get("/explore", params={"since": THIS_MONTH, "until": THIS_MONTH}).json()
    focused = client.get("/explore", params={"conversation": mac["id"], "since": "2999-01"}).json()

    assert other_source["nodes"] == [] and future["nodes"] == []
    assert len(_by_kind(this_month, "conversation")) == 3
    assert list(_by_kind(focused, "conversation")) == [mac["id"]]  # the focus always stays


def test_unknown_places_and_bad_dates_are_refused(client: TestClient) -> None:
    for params in ({"entity": "missing"}, {"conversation": "missing"}, {"topic": "missing"}):
        assert client.get("/explore", params=params).status_code == 404
    assert client.get("/explore", params={"since": "last week"}).status_code == 422


def test_the_timeline_counts_chats_by_month(client: TestClient) -> None:
    assert client.get("/explore/timeline").json() == [{"month": THIS_MONTH, "conversations": 3}]
    assert client.get("/explore/timeline", params={"source": "claude"}).json() == []


def test_a_conversation_lists_the_entities_it_mentions(client: TestClient) -> None:
    mac = _titled(client.get("/explore").json(), "Mac tools")

    conversation = client.get(f"/conversations/{mac['id']}").json()
    names = [entity["name"] for entity in conversation["entities"]]
    message = conversation["messages"][0]["id"]

    assert {"Parsec", "Moonlight", "Sunshine", "Mac"} <= set(names)
    assert all(entity["messages"] == [message] for entity in conversation["entities"])


def test_the_web_interface_has_the_explore_view(client: TestClient) -> None:
    page = client.get("/").text
    script = client.get("/app.js").text

    assert 'id="view-explore"' in page and 'id="explore-panel"' in page
    assert 'id="theme-toggle"' in page
    assert "/explore?" in script and "linkEntities" in script
