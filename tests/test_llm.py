"""Tests for the language model client and its settings."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx2
import pytest

from chatlore.llm import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ChatMessage,
    LLMError,
    OpenAICompatibleLLM,
    llm_settings,
    make_llm,
)

Handler = Callable[[httpx2.Request], httpx2.Response]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CHATLORE_LLM_BASE_URL",
        "CHATLORE_LLM_MODEL",
        "CHATLORE_LLM_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def _answer(content: str | None, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "id": "gen-1",
        "object": "chat.completion",
        "created": 0,
        "model": "z-ai/glm-5.3-flash-routed",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {"role": "assistant", "content": content},
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


@pytest.fixture
def make_client() -> Iterator[Callable[[Handler], OpenAICompatibleLLM]]:
    clients: list[OpenAICompatibleLLM] = []

    def build(handler: Handler) -> OpenAICompatibleLLM:
        client = OpenAICompatibleLLM(
            "z-ai/glm-5.3-flash",
            "https://openrouter.ai/api/v1",
            "sk-test",
            max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
        )
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.close()


def test_complete_sends_the_conversation_and_reads_the_answer(
    make_client: Callable[[Handler], OpenAICompatibleLLM],
) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_answer("Paris"))

    completion = make_client(handler).complete(
        [ChatMessage("system", "Answer briefly."), ChatMessage("user", "Capital of France?")]
    )

    assert completion.text == "Paris"
    assert completion.model == "z-ai/glm-5.3-flash-routed"
    assert (completion.input_tokens, completion.output_tokens) == (12, 3)
    request = seen[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert body["model"] == "z-ai/glm-5.3-flash"
    assert body["messages"] == [
        {"role": "system", "content": "Answer briefly."},
        {"role": "user", "content": "Capital of France?"},
    ]
    assert "response_format" not in body
    assert "max_tokens" not in body


def test_json_output_and_max_tokens_are_passed_through(
    make_client: Callable[[Handler], OpenAICompatibleLLM],
) -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=_answer('{"entities": []}'))

    completion = make_client(handler).complete(
        [ChatMessage("user", "Extract.")], json_output=True, max_tokens=500
    )

    assert json.loads(completion.text) == {"entities": []}
    assert bodies[0]["response_format"] == {"type": "json_object"}
    assert bodies[0]["max_tokens"] == 500


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx2.Response(401, json={"error": {"message": "No auth"}}), "HTTP 401"),
        (httpx2.Response(200, json=_answer("half an ans", "length")), "token limit"),
        (httpx2.Response(200, json=_answer("  ")), "empty answer"),
        (httpx2.Response(200, json=_answer(None)), "empty answer"),
    ],
)
def test_unusable_answers_raise_llm_error(
    make_client: Callable[[Handler], OpenAICompatibleLLM],
    response: httpx2.Response,
    message: str,
) -> None:
    client = make_client(lambda request: response)

    with pytest.raises(LLMError, match=message):
        client.complete([ChatMessage("user", "Hello")])


def test_an_unreachable_server_raises_llm_error(
    make_client: Callable[[Handler], OpenAICompatibleLLM],
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(LLMError, match="could not reach"):
        make_client(handler).complete([ChatMessage("user", "Hello")])


def test_defaults_are_openrouter_and_glm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")

    settings = llm_settings()
    llm = make_llm()
    llm.close()

    assert (settings.base_url, settings.model, settings.api_key) == (
        DEFAULT_BASE_URL,
        DEFAULT_MODEL,
        "sk-or",
    )
    assert settings.is_openrouter
    assert llm.name == DEFAULT_MODEL


def test_openrouter_without_a_key_explains_what_to_set() -> None:
    with pytest.raises(LLMError, match="OPENROUTER_API_KEY"):
        make_llm()


def test_a_local_server_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHATLORE_LLM_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("CHATLORE_LLM_MODEL", "qwen3:8b")

    llm = make_llm()
    llm.close()

    assert llm.name == "qwen3:8b"
    assert llm.base_url == "http://localhost:11434/v1"


def test_the_openrouter_key_is_never_sent_to_another_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")

    for lookalike in ("https://example.com/v1", "https://evilopenrouter.ai/api/v1"):
        monkeypatch.setenv("CHATLORE_LLM_BASE_URL", lookalike)
        assert llm_settings().api_key is None


def test_the_chatlore_key_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    monkeypatch.setenv("CHATLORE_LLM_API_KEY", "sk-chatlore")

    assert llm_settings().api_key == "sk-chatlore"
