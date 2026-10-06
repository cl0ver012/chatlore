"""Language models behind one small interface.

ChatLore talks to any OpenAI-compatible chat API. The default is OpenRouter,
which serves many inexpensive open models behind one key. Ollama, vLLM,
LM Studio, and other local servers work by pointing the base URL at them.
Anthropic's and Google's own APIs work too, with ``CHATLORE_LLM_PROVIDER`` set
to ``anthropic`` or ``gemini`` and their optional SDKs installed
(``chatlore.llm_anthropic``, ``chatlore.llm_gemini``). Anything with the
``LLM`` shape can replace the client.

Importing and searching never need a model. Only extraction and chat do.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol
from urllib.parse import urlparse

import openai

if TYPE_CHECKING:
    import httpx2
    from openai.types.chat import ChatCompletionMessageParam

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
DEFAULT_REASONING = "off"

BASE_URL_ENV = "CHATLORE_LLM_BASE_URL"
MODEL_ENV = "CHATLORE_LLM_MODEL"
API_KEY_ENV = "CHATLORE_LLM_API_KEY"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
REASONING_ENV = "CHATLORE_LLM_REASONING"
PROVIDER_ENV = "CHATLORE_LLM_PROVIDER"

PROVIDERS = ("openai", "anthropic", "gemini")
"""Where the model runs: any OpenAI-compatible API (the default), or Anthropic's or Google's own."""

PROVIDER_DEFAULTS = {
    "anthropic": ("https://api.anthropic.com", "claude-opus-5-5", ("ANTHROPIC_API_KEY",)),
    "gemini": (
        "https://generativelanguage.googleapis.com",
        "gemini-3.5-flash-lite",
        ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    ),
}
"""Base URL, model, and the variables holding the key, for each provider with its own API."""

REASONING_LEVELS = ("off", "low", "medium", "high", "default")
"""How much the model may think before answering; "default" leaves it to the model.

Extraction asks many short, well-specified questions, where reasoning mostly
costs time: measured on a real export, a reasoning model spent 70% of its output
on hidden reasoning and took 65 to 90 seconds per request.
"""

_NO_KEY = "not-needed"
"""Sent to servers that take no key, since the client insists on one."""


class LLMError(Exception):
    """The model could not be reached or did not return a usable answer."""


class LLMAnswerError(LLMError):
    """The model answered, but the answer cannot be used: empty or cut off.

    Unlike an unreachable server, this can happen to one request among many, so
    callers working through a batch may skip it and ask again later.
    """


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One turn of a conversation with the model."""

    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class Completion:
    """What the model answered, and what it cost in tokens."""

    text: str
    model: str
    input_tokens: int
    output_tokens: int


class LLM(Protocol):
    """What extraction and chat need from a language model."""

    name: str

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        json_output: bool = False,
        max_tokens: int | None = None,
    ) -> Completion:
        """Answer ``messages``. With ``json_output`` the answer is a JSON object."""
        ...


class StreamingLLM(LLM, Protocol):
    """A model that can also hand over its answer piece by piece, as chat needs."""

    def stream(
        self, messages: Sequence[ChatMessage], *, max_tokens: int | None = None
    ) -> Iterator[str]:
        """Answer ``messages``, yielding text as it is written."""
        ...


class ClosableLLM(StreamingLLM, Protocol):
    """A configured model, holding a connection until it is closed."""

    base_url: str
    reasoning: str

    def close(self) -> None: ...


def _params(messages: Sequence[ChatMessage]) -> list[ChatCompletionMessageParam]:
    params: list[ChatCompletionMessageParam] = []
    for message in messages:
        if message.role == "system":
            params.append({"role": "system", "content": message.content})
        elif message.role == "user":
            params.append({"role": "user", "content": message.content})
        else:
            params.append({"role": "assistant", "content": message.content})
    return params


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """Which model to use and where, read from the environment."""

    base_url: str
    model: str
    api_key: str | None
    reasoning: str = DEFAULT_REASONING
    provider: str = "openai"

    @property
    def is_openrouter(self) -> bool:
        return self.provider == "openai" and _is_openrouter(self.base_url)

    @property
    def key_env(self) -> str | None:
        """The variable to set for a key, or None when the server needs none."""
        if self.provider in PROVIDER_DEFAULTS:
            return PROVIDER_DEFAULTS[self.provider][2][0]
        return OPENROUTER_KEY_ENV if self.is_openrouter else None


def _is_openrouter(base_url: str) -> bool:
    host = urlparse(base_url).hostname or ""
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def _reasoning_body(reasoning: str, base_url: str) -> dict[str, object]:
    """The request fields that set ``reasoning``; OpenRouter has its own shape.

    Other OpenAI-compatible servers get the standard ``reasoning_effort`` field,
    with "none" for off. Nothing is sent for "default", so servers that do not
    know either field are not affected.
    """
    if reasoning == "default":
        return {}
    if _is_openrouter(base_url):
        if reasoning == "off":
            return {"reasoning": {"enabled": False}}
        return {"reasoning": {"effort": reasoning}}
    return {"reasoning_effort": "none" if reasoning == "off" else reasoning}


class OpenAICompatibleLLM:
    """A chat model behind an OpenAI-compatible API."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 120.0,
        max_retries: int = 2,
        http_client: httpx2.Client | None = None,
        reasoning: str = "default",
    ) -> None:
        self.name = model
        self.base_url = base_url
        self.reasoning = reasoning
        self._extra_body = _reasoning_body(reasoning, base_url)
        self._client = openai.OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )

    def close(self) -> None:
        self._client.close()

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        json_output: bool = False,
        max_tokens: int | None = None,
    ) -> Completion:
        try:
            response = self._client.chat.completions.create(
                model=self.name,
                messages=_params(messages),
                response_format={"type": "json_object"} if json_output else openai.omit,
                max_tokens=max_tokens if max_tokens is not None else openai.omit,
                extra_body=self._extra_body or None,
            )
        except openai.APIStatusError as error:
            raise LLMError(
                f"{self.name} at {self.base_url} failed with HTTP {error.status_code}: "
                f"{error.message}"
            ) from error
        except openai.APIConnectionError as error:
            raise LLMError(f"could not reach {self.base_url}: {error}") from error

        if not response.choices:
            raise LLMAnswerError(f"{self.name} returned no answer")
        choice = response.choices[0]
        if choice.finish_reason == "length":
            raise LLMAnswerError(f"{self.name} stopped at the token limit before finishing")
        text = choice.message.content or ""
        if not text.strip():
            raise LLMAnswerError(f"{self.name} returned an empty answer")
        usage = response.usage
        return Completion(
            text=text,
            model=response.model or self.name,
            input_tokens=usage.prompt_tokens if usage is not None else 0,
            output_tokens=usage.completion_tokens if usage is not None else 0,
        )

    def stream(
        self, messages: Sequence[ChatMessage], *, max_tokens: int | None = None
    ) -> Iterator[str]:
        """Yield the answer as it is written.

        Errors are the same as for ``complete``. An answer cut off at the token
        limit raises ``LLMAnswerError`` after the text written so far.
        """
        written = False
        try:
            response = self._client.chat.completions.create(
                model=self.name,
                messages=_params(messages),
                max_tokens=max_tokens if max_tokens is not None else openai.omit,
                extra_body=self._extra_body or None,
                stream=True,
            )
            try:
                for chunk in response:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.delta.content:
                        written = True
                        yield choice.delta.content
                    if choice.finish_reason == "length":
                        raise LLMAnswerError(
                            f"{self.name} stopped at the token limit before finishing"
                        )
            finally:
                response.close()
        except openai.APIStatusError as error:
            raise LLMError(
                f"{self.name} at {self.base_url} failed with HTTP {error.status_code}: "
                f"{error.message}"
            ) from error
        except openai.APIConnectionError as error:
            raise LLMError(f"could not reach {self.base_url}: {error}") from error
        if not written:
            raise LLMAnswerError(f"{self.name} returned an empty answer")


def llm_settings() -> LLMSettings:
    """Read the model settings from the environment, falling back to the defaults.

    ``OPENROUTER_API_KEY`` is only used when the base URL is OpenRouter, so the
    key is never sent to another server by accident. Likewise
    ``ANTHROPIC_API_KEY`` is only used for Anthropic, and ``GEMINI_API_KEY`` or
    ``GOOGLE_API_KEY`` only for Gemini.
    """
    provider = (os.environ.get(PROVIDER_ENV) or "openai").strip().lower()
    if provider not in PROVIDERS:
        raise LLMError(f"{PROVIDER_ENV} must be one of {', '.join(PROVIDERS)}")
    reasoning = (os.environ.get(REASONING_ENV) or DEFAULT_REASONING).strip().lower()
    if reasoning not in REASONING_LEVELS:
        raise LLMError(f"{REASONING_ENV} must be one of {', '.join(REASONING_LEVELS)}")
    key = os.environ.get(API_KEY_ENV) or None
    if provider in PROVIDER_DEFAULTS:
        base_url, model, key_envs = PROVIDER_DEFAULTS[provider]
        for name in key_envs:
            key = key or os.environ.get(name) or None
    else:
        base_url, model = DEFAULT_BASE_URL, DEFAULT_MODEL
        if key is None and _is_openrouter(os.environ.get(BASE_URL_ENV) or base_url):
            key = os.environ.get(OPENROUTER_KEY_ENV) or None
    return LLMSettings(
        os.environ.get(BASE_URL_ENV) or base_url,
        os.environ.get(MODEL_ENV) or model,
        key,
        reasoning,
        provider,
    )


def make_llm() -> ClosableLLM:
    """Return the configured model, or explain what is missing."""
    settings = llm_settings()
    if settings.api_key is None and settings.is_openrouter:
        raise LLMError(
            f"no API key for OpenRouter. Set {OPENROUTER_KEY_ENV}, or point "
            f"{BASE_URL_ENV} at a local server such as http://localhost:11434/v1"
        )
    if settings.provider in PROVIDER_DEFAULTS:
        return _provider_llm(settings)
    return OpenAICompatibleLLM(
        settings.model,
        settings.base_url,
        settings.api_key or _NO_KEY,
        reasoning=settings.reasoning,
    )


def _provider_llm(settings: LLMSettings) -> ClosableLLM:
    """The client for Anthropic's or Google's own API, from its optional SDK."""
    name = settings.provider
    if settings.api_key is None:
        raise LLMError(f"no API key for {name}. Set {settings.key_env} or {API_KEY_ENV}")
    try:
        if name == "anthropic":
            from chatlore.llm_anthropic import AnthropicLLM as Client
        else:
            from chatlore.llm_gemini import GeminiLLM as Client  # type: ignore[assignment]
    except ImportError as error:
        raise LLMError(
            f"{name} needs its SDK: pip install 'chatlore[{name}]' "
            f"(or uv tool install 'chatlore[{name}]')"
        ) from error
    return Client(
        settings.model, settings.api_key, base_url=settings.base_url, reasoning=settings.reasoning
    )
