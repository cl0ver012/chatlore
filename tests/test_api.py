"""Tests for the REST API and streaming chat."""

from __future__ import annotations

import json
from collections.abc import Iterator
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
    "Weekend": "Lisbon and Sintra make a good weekend. Take the train from Rossio to Sintra.",
}


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "home"
    monkeypatch.setenv("CHATLORE_HOME", str(target))
    monkeypatch.setattr("chatlore.cli.make_embedder", lambda home: FakeEmbedder())
    monkeypatch.setattr("chatlore.api.make_embedder", lambda home: FakeEmbedder())
    return target


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    fake = FakeLLM()
    monkeypatch.setattr("chatlore.cli.make_llm", lambda: fake)
    monkeypatch.setattr("chatlore.api.make_llm", lambda: fake)
    return fake


@pytest.fixture
def client(home: Path, llm: FakeLLM) -> Iterator[TestClient]:
    for title, text in NOTES.items():
        runner.invoke(cli, ["note", text, "--title", title])
    runner.invoke(cli, ["process"])
    runner.invoke(cli, ["extract"])
    with TestClient(create_app(home)) as test_client:
        yield test_client


def _events(body: str) -> list[tuple[str, Any]]:
    """Server-sent events as (event, data) pairs."""
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def test_health_and_stats(client: TestClient) -> None:
    assert client.get("/health").json()["status"] == "ok"
    stats = client.get("/stats").json()
    assert stats["conversations"] == 2
    assert stats["chunks"] == stats["embeddings"] > 0
    assert stats["entities"] > 0


def test_search_by_words_and_by_everything(client: TestClient) -> None:
    words = client.get("/search", params={"q": "Sintra", "mode": "words"}).json()
    hybrid = client.get("/search", params={"q": "Sintra"}).json()

    assert words[0]["title"] == "Weekend"
    assert words[0]["matched"] == ["words"]
    assert "[Sintra]" in words[0]["snippet"]
    assert hybrid[0]["title"] == "Weekend"
    assert {"words", "meaning"} <= set(hybrid[0]["matched"])
    assert client.get("/search", params={"q": "x", "mode": "other"}).status_code == 422


def test_conversations_can_be_listed_and_read(client: TestClient) -> None:
    listed = client.get("/conversations").json()
    one = client.get(f"/conversations/{listed[0]['id']}").json()

    assert {item["title"] for item in listed} == set(NOTES)
    assert one["messages"][0]["text"] in NOTES.values()
    assert client.get("/conversations/missing").status_code == 404


def test_entities_can_be_found_and_explored(client: TestClient) -> None:
    top = client.get("/entities").json()
    found = client.get("/entities", params={"q": "parsec"}).json()
    parsec = client.get(f"/entities/{found[0]['id']}").json()

    assert top
    assert found[0]["name"] == "Parsec"
    assert parsec["summary"]
    assert parsec["conversations"] == [
        {"id": parsec["conversations"][0]["id"], "title": "Mac tools"}
    ]
    assert {related["name"] for related in parsec["related"]} & {"Mac", "Moonlight"}
    assert client.get("/entities/missing").status_code == 404


def test_topics_can_be_listed_and_read(client: TestClient) -> None:
    listed = client.get("/topics").json()
    one = client.get(f"/topics/{listed[0]['id']}").json()

    assert listed[0]["title"]
    assert len(one["members"]) == one["size"]
    assert client.get("/topics", params={"q": "zzzz"}).json() == []
    assert client.get("/topics/missing").status_code == 404


def test_chat_streams_sources_then_tokens_then_citations(client: TestClient) -> None:
    response = client.post("/chat", json={"question": "How do I fix Parsec?"})

    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    names = [name for name, _ in events]
    assert names[0] == "sources"
    assert set(names[1:-1]) == {"token"}
    assert names[-1] == "done"
    sources = events[0][1]
    assert sources[0]["title"] == "Mac tools"
    assert "Parsec" in sources[0]["text"]
    answer = "".join(data["text"] for name, data in events if name == "token")
    assert "Mac tools [1]" in answer
    assert events[-1][1] == {"cited": [source["number"] for source in sources], "found": True}


def test_chat_with_nothing_to_go_on_does_not_call_the_model(
    client: TestClient, llm: FakeLLM, home: Path
) -> None:
    asked = len(llm.chat_requests)
    empty = home.parent / "empty"

    with TestClient(create_app(empty)) as fresh:
        events = _events(fresh.post("/chat", json={"question": "Anything?"}).text)

    assert events == [("sources", []), ("done", {"cited": [], "found": False})]
    assert len(llm.chat_requests) == asked


def test_chat_reports_a_failing_model(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chatlore.api.make_llm", lambda: FakeLLM(fail_on_request=1))

    events = _events(client.post("/chat", json={"question": "How do I fix Parsec?"}).text)

    assert events[0][0] == "sources"
    assert events[-1] == ("error", {"message": "fake model is unreachable"})


def test_chat_rejects_an_empty_question(client: TestClient) -> None:
    assert client.post("/chat", json={"question": ""}).status_code == 422
    assert client.post("/chat", json={"question": "Hi", "sources": 99}).status_code == 422


def test_serve_starts_uvicorn_on_this_machine_by_default(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **options: started.append(options))

    local = runner.invoke(cli, ["serve", "--port", "8123"])
    public = runner.invoke(cli, ["serve", "--host", "0.0.0.0"])

    assert started[0] == {"host": "127.0.0.1", "port": 8123, "log_level": "warning"}
    assert "http://127.0.0.1:8123" in local.output
    assert "Listening beyond this machine" not in local.output
    assert "Listening beyond this machine" in public.output
