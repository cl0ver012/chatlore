"""Tests for serving a public demo: question limits, the demo flag, and MCP over HTTP."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from chatlore.api import _Allowance, create_app
from chatlore.chat import ChatLimits
from chatlore.cli import app as cli
from tests.fakes import FakeEmbedder, FakeLLM

runner = CliRunner()

NOTES = {
    "Mac tools": "Parsec keeps dropping frames on the Mac. Try Moonlight with Sunshine instead.",
    "Weekend": "Lisbon and Sintra make a good weekend. Take the train from Rossio to Sintra.",
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "home"
    monkeypatch.setenv("CHATLORE_HOME", str(target))
    for module in ("cli", "api", "mcp_server"):
        monkeypatch.setattr(f"chatlore.{module}.make_embedder", lambda home: FakeEmbedder())
    fake = FakeLLM()
    monkeypatch.setattr("chatlore.cli.make_llm", lambda: fake)
    monkeypatch.setattr("chatlore.api.make_llm", lambda: fake)
    for title, text in NOTES.items():
        runner.invoke(cli, ["note", text, "--title", title])
    runner.invoke(cli, ["process"])
    runner.invoke(cli, ["extract"])
    return target


@pytest.fixture
def public(home: Path) -> Iterator[TestClient]:
    limits = ChatLimits(per_visitor_hour=2, per_day=3)
    with TestClient(create_app(home, public=True, limits=limits)) as client:
        yield client


def ask(client: TestClient) -> list[str]:
    """Ask a question and return the kinds of events streamed back."""
    response = client.post("/chat", json={"question": "Which train goes to Sintra?"})
    return [
        line.split(": ", 1)[1] for line in response.text.splitlines() if line.startswith("event")
    ]


def test_a_visitor_gets_a_few_questions_an_hour() -> None:
    now = [1_000_000.0]
    allowance = _Allowance(ChatLimits(per_visitor_hour=2, per_day=100), clock=lambda: now[0])

    first, second, third = (allowance.take("a") for _ in range(3))
    other = allowance.take("b")
    now[0] += 3600
    later = allowance.take("a")

    assert first is None and second is None
    assert third is not None and "2 questions an hour" in third
    assert other is None
    assert later is None


def test_all_visitors_share_a_daily_limit_that_resets_at_midnight_utc() -> None:
    now = [86_400.0 * 20_000 + 3600]
    allowance = _Allowance(ChatLimits(per_visitor_hour=10, per_day=2), clock=lambda: now[0])

    answers = [allowance.take(visitor) for visitor in ("a", "b", "c")]
    now[0] += 86_400
    tomorrow = allowance.take("c")

    assert answers[:2] == [None, None]
    assert answers[2] is not None and "all the questions it can today" in answers[2]
    assert tomorrow is None


def test_a_public_server_limits_questions_and_says_why(public: TestClient) -> None:
    events = [ask(public) for _ in range(3)]

    assert events[0][-1] == "done"
    assert events[1][-1] == "done"
    assert events[2] == ["sources", "error"]
    refused = public.post("/chat", json={"question": "Which train goes to Sintra?"}).text
    assert "2 questions an hour for each visitor" in refused
    assert "pip install chatlore" in refused


def test_questions_that_find_nothing_are_not_counted(tmp_path: Path) -> None:
    limits = ChatLimits(per_visitor_hour=1, per_day=1)
    with TestClient(create_app(tmp_path / "empty", public=True, limits=limits)) as client:
        for _ in range(3):
            assert ask(client) == ["sources", "done"]


def test_the_health_check_says_whether_the_server_is_public(public: TestClient, home: Path) -> None:
    with TestClient(create_app(home)) as private:
        assert private.get("/health").json()["public"] is False
    assert public.get("/health").json()["public"] is True


def test_a_private_server_answers_questions_without_limits(home: Path) -> None:
    with TestClient(create_app(home)) as private:
        for _ in range(5):
            response = private.post("/chat", json={"question": "Which train goes to Sintra?"})
            assert "event: done" in response.text


def test_mcp_is_served_over_http(public: TestClient) -> None:
    listed = public.post("/mcp", json=LIST_TOOLS, headers=MCP_HEADERS).json()
    called = public.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": "Sintra"}},
        },
        headers=MCP_HEADERS,
    ).json()

    assert [tool["name"] for tool in listed["result"]["tools"]][:2] == ["search", "ask_context"]
    assert "Weekend" in called["result"]["content"][0]["text"]


def test_a_private_server_only_answers_mcp_requests_for_this_machine(home: Path) -> None:
    with TestClient(create_app(home)) as private:
        foreign = private.post(
            "/mcp", json=LIST_TOOLS, headers={**MCP_HEADERS, "Host": "evil.example"}
        )
        local = private.post(
            "/mcp", json=LIST_TOOLS, headers={**MCP_HEADERS, "Host": "127.0.0.1:8000"}
        )

    assert foreign.status_code == 421
    assert local.status_code == 200
    assert json.loads(local.text)["result"]["tools"]


def test_serve_public_trusts_the_proxy_and_sets_the_limits(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **options: started.append((app, options)))

    result = runner.invoke(
        cli, ["serve", "--public", "--host", "0.0.0.0", "--questions-per-hour", "4"]
    )

    app, options = started[0]
    assert result.exit_code == 0
    assert options["proxy_headers"] is True
    assert options["forwarded_allow_ips"] == "*"
    assert "ask 4 questions an hour each, 300 a day in all" in " ".join(result.output.split())
    assert "Listening beyond this machine" not in result.output
    with TestClient(app) as client:
        assert client.get("/health").json()["public"] is True
