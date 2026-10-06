"""Tests for Anthropic's and Google's own APIs as model providers, and choosing one."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from chatlore.llm import ChatMessage, LLMAnswerError, LLMError, llm_settings, make_llm

MESSAGES = [ChatMessage("system", "Be brief."), ChatMessage("user", "Hi")]


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CHATLORE_LLM_PROVIDER",
        "CHATLORE_LLM_BASE_URL",
        "CHATLORE_LLM_MODEL",
        "CHATLORE_LLM_API_KEY",
        "CHATLORE_LLM_REASONING",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


# -- choosing a provider -------------------------------------------------------


def test_anthropic_uses_its_own_key_model_and_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHATLORE_LLM_PROVIDER", "Anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")

    settings = llm_settings()

    assert (settings.provider, settings.model, settings.api_key) == (
        "anthropic",
        "claude-opus-5-5",
        "sk-ant",
    )
    assert settings.base_url == "https://api.anthropic.com"
    assert not settings.is_openrouter and settings.key_env == "ANTHROPIC_API_KEY"


def test_gemini_takes_either_google_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHATLORE_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")

    settings = llm_settings()

    assert (settings.model, settings.api_key) == ("gemini-3.5-flash-lite", "g-key")


def test_provider_keys_are_never_sent_elsewhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")

    assert llm_settings().api_key is None  # OpenRouter, the default, gets neither
    monkeypatch.setenv("CHATLORE_LLM_PROVIDER", "gemini")
    assert llm_settings().api_key == "g-key"


def test_the_model_and_key_can_be_set_for_any_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHATLORE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("CHATLORE_LLM_MODEL", "claude-haiku-4-5")
    monkeypatch.setenv("CHATLORE_LLM_API_KEY", "sk-chatlore")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")

    settings = llm_settings()

    assert (settings.model, settings.api_key) == ("claude-haiku-4-5", "sk-chatlore")


def test_an_unknown_provider_or_a_missing_key_is_explained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHATLORE_LLM_PROVIDER", "cohere")
    with pytest.raises(LLMError, match="openai, anthropic, gemini"):
        llm_settings()

    monkeypatch.setenv("CHATLORE_LLM_PROVIDER", "gemini")
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        make_llm()


def test_a_missing_sdk_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHATLORE_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    monkeypatch.setitem(sys.modules, "chatlore.llm_anthropic", None)

    with pytest.raises(LLMError, match=r"pip install 'chatlore\[anthropic\]'"):
        make_llm()


# -- Anthropic -----------------------------------------------------------------


@pytest.fixture
def anthropic_client() -> Iterator[Callable[..., Any]]:
    pytest.importorskip("anthropic")
    import httpx2

    from chatlore.llm_anthropic import AnthropicLLM

    clients: list[AnthropicLLM] = []

    def build(handler: Callable[[Any], Any], **options: Any) -> AnthropicLLM:
        client = AnthropicLLM(
            options.pop("model", "claude-opus-5-5"),
            "sk-ant",
            max_retries=0,
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
            **options,
        )
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.close()


def _message(text: str, stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": text},
        ],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }


def test_anthropic_sends_system_apart_and_reads_only_the_text(
    anthropic_client: Callable[..., Any],
) -> None:
    import httpx2

    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=_message('```json\n{"passages": []}\n```'))

    llm = anthropic_client(handler, reasoning="off")
    completion = llm.complete(MESSAGES, json_output=True)
    body = json.loads(seen[0].content)

    assert completion.text == '{"passages": []}'
    assert (completion.input_tokens, completion.output_tokens) == (12, 3)
    assert body["system"] == "Be brief."
    assert body["messages"] == [{"role": "user", "content": "Hi"}]
    assert body["max_tokens"] == 16_000
    assert body["output_config"] == {"effort": "low"}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen[0].headers["anthropic-beta"]
    assert seen[0].headers["x-api-key"] == "sk-ant"


def test_anthropic_models_without_fallbacks_get_neither_it_nor_an_effort_by_default(
    anthropic_client: Callable[..., Any],
) -> None:
    import httpx2

    bodies: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(200, json=_message("Hello"))

    anthropic_client(handler, model="claude-haiku-4-5").complete(MESSAGES, max_tokens=50)

    assert "fallbacks" not in bodies[0] and "output_config" not in bodies[0]
    assert bodies[0]["max_tokens"] == 50


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_anthropic_answers_that_cannot_be_used_are_skippable(
    anthropic_client: Callable[..., Any], stop_reason: str
) -> None:
    import httpx2

    llm = anthropic_client(lambda request: httpx2.Response(200, json=_message("x", stop_reason)))

    with pytest.raises(LLMAnswerError):
        llm.complete(MESSAGES)


def test_anthropic_errors_and_unreachable_servers_stop(
    anthropic_client: Callable[..., Any],
) -> None:
    import httpx2

    def refused(request: httpx2.Request) -> httpx2.Response:
        error = {"type": "invalid_request_error", "message": "bad model"}
        return httpx2.Response(400, json={"type": "error", "error": error})

    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route", request=request)

    with pytest.raises(LLMError, match="HTTP 400") as status:
        anthropic_client(refused).complete(MESSAGES)
    with pytest.raises(LLMError, match="could not reach") as connection:
        anthropic_client(unreachable).complete(MESSAGES)

    assert not isinstance(status.value, LLMAnswerError)
    assert not isinstance(connection.value, LLMAnswerError)


def _sse(*events: tuple[str, dict[str, Any]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def test_anthropic_streams_the_answer(anthropic_client: Callable[..., Any]) -> None:
    import httpx2

    start = {**_message(""), "content": [], "stop_reason": None}
    body = _sse(
        ("message_start", {"type": "message_start", "message": start}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        *(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": piece},
                },
            )
            for piece in ("Hel", "lo")
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    assert list(anthropic_client(handler).stream(MESSAGES)) == ["Hel", "lo"]


# -- Gemini --------------------------------------------------------------------


@pytest.fixture
def gemini_client() -> Iterator[Callable[..., Any]]:
    pytest.importorskip("google.genai")
    import httpx

    from chatlore.llm_gemini import GeminiLLM

    clients: list[GeminiLLM] = []

    def build(handler: Callable[[Any], Any], **options: Any) -> GeminiLLM:
        client = GeminiLLM(
            "gemini-3.5-flash-lite",
            "g-key",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            **options,
        )
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.close()


def _candidate(text: str, finish_reason: str = "STOP") -> dict[str, Any]:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": finish_reason}
        ],
        "usageMetadata": {
            "promptTokenCount": 12,
            "candidatesTokenCount": 3,
            "thoughtsTokenCount": 2,
        },
        "modelVersion": "gemini-3.5-flash-lite",
    }


def test_gemini_sends_the_instruction_apart_and_asks_for_json(
    gemini_client: Callable[..., Any],
) -> None:
    import httpx

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_candidate('{"passages": []}'))

    llm = gemini_client(handler, reasoning="off")
    completion = llm.complete([*MESSAGES, ChatMessage("assistant", "Hello")], json_output=True)
    body = json.loads(seen[0].content)

    assert completion.text == '{"passages": []}'
    assert (completion.input_tokens, completion.output_tokens) == (12, 5)
    assert seen[0].url.path.endswith("/models/gemini-3.5-flash-lite:generateContent")
    assert seen[0].headers["x-goog-api-key"] == "g-key"
    assert body["systemInstruction"]["parts"] == [{"text": "Be brief."}]
    assert [turn["role"] for turn in body["contents"]] == ["user", "model"]
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert list(body["generationConfig"]["thinkingConfig"].values()) == ["MINIMAL"]


@pytest.mark.parametrize("finish_reason", ["MAX_TOKENS", "SAFETY"])
def test_gemini_answers_that_cannot_be_used_are_skippable(
    gemini_client: Callable[..., Any], finish_reason: str
) -> None:
    import httpx

    llm = gemini_client(lambda request: httpx.Response(200, json=_candidate("x", finish_reason)))

    with pytest.raises(LLMAnswerError):
        llm.complete(MESSAGES)


def test_gemini_errors_stop(gemini_client: Callable[..., Any]) -> None:
    import httpx

    def refused(request: httpx.Request) -> httpx.Response:
        error = {"code": 400, "message": "model not found", "status": "INVALID_ARGUMENT"}
        return httpx.Response(400, json={"error": error})

    with pytest.raises(LLMError, match="HTTP 400") as status:
        gemini_client(refused).complete(MESSAGES)

    assert not isinstance(status.value, LLMAnswerError)


def test_gemini_streams_the_answer(gemini_client: Callable[..., Any]) -> None:
    import httpx

    first = _candidate("Hel", "FINISH_REASON_UNSPECIFIED")
    del first["candidates"][0]["finishReason"]
    body = "".join(
        f"data: {json.dumps(chunk)}\r\n\r\n" for chunk in (first, _candidate("lo"))
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "streamGenerateContent" in request.url.path
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    assert list(gemini_client(handler).stream(MESSAGES)) == ["Hel", "lo"]
