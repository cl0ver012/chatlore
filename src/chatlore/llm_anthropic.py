"""Claude through Anthropic's own API, with the official SDK.

Chosen with ``CHATLORE_LLM_PROVIDER=anthropic`` and installed with
``pip install 'chatlore[anthropic]'``. It has the same shape as the
OpenAI-compatible client, so extraction and chat do not know which one runs.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

import anthropic

from chatlore.llm import ChatMessage, Completion, LLMAnswerError, LLMError

if TYPE_CHECKING:
    import httpx2

MAX_TOKENS = 16_000
"""The ceiling on an answer, which this API requires; extraction answers stay far below it."""

FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)
"""Models whose declined requests the API re-runs on its recommended fallback model."""

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _unfenced(text: str) -> str:
    """The JSON in an answer, without the code fence a model sometimes wraps it in."""
    match = _FENCE.match(text.strip())
    return match.group(1) if match else text


class AnthropicLLM:
    """A Claude model behind Anthropic's Messages API."""

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        http_client: httpx2.Client | None = None,
        reasoning: str = "default",
    ) -> None:
        self.name = model
        self.base_url = base_url or "https://api.anthropic.com"
        self.reasoning = reasoning
        self._client = anthropic.Anthropic(
            api_key=api_key,
            base_url=self.base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, messages: Sequence[ChatMessage], max_tokens: int | None) -> dict[str, Any]:
        """The request fields: system turns go in ``system``, the rest in ``messages``.

        Reasoning sets the effort, "off" being the lowest, since current Claude
        models always think a little; "default" leaves it to the model. Models
        that support it get the server-side fallback for declined requests.
        """
        request: dict[str, Any] = {
            "model": self.name,
            "max_tokens": max_tokens if max_tokens is not None else MAX_TOKENS,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
                if message.role != "system"
            ],
        }
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        if system:
            request["system"] = system
        if self.reasoning != "default":
            effort = "low" if self.reasoning == "off" else self.reasoning
            request["output_config"] = {"effort": effort}
        if self.name in FALLBACK_MODELS:
            request["betas"] = [FALLBACK_BETA]
            request["fallbacks"] = "default"
        return request

    def _failed(self, error: anthropic.APIError) -> LLMError:
        if isinstance(error, anthropic.APIStatusError):
            return LLMError(
                f"{self.name} at {self.base_url} failed with HTTP {error.status_code}: "
                f"{error.message}"
            )
        return LLMError(f"could not reach {self.base_url}: {error}")

    def _check(self, stop_reason: str | None) -> None:
        if stop_reason == "refusal":
            raise LLMAnswerError(f"{self.name} declined to answer")
        if stop_reason == "max_tokens":
            raise LLMAnswerError(f"{self.name} stopped at the token limit before finishing")

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        json_output: bool = False,
        max_tokens: int | None = None,
    ) -> Completion:
        """Answer ``messages``; with ``json_output``, JSON read without a code fence."""
        try:
            response = self._client.beta.messages.create(**self._request(messages, max_tokens))
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as error:
            raise self._failed(error) from error
        self._check(response.stop_reason)
        text = "".join(block.text for block in response.content if block.type == "text")
        if json_output:
            text = _unfenced(text)
        if not text.strip():
            raise LLMAnswerError(f"{self.name} returned an empty answer")
        return Completion(
            text=text,
            model=response.model or self.name,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def stream(
        self, messages: Sequence[ChatMessage], *, max_tokens: int | None = None
    ) -> Iterator[str]:
        """Yield the answer as it is written, with the same errors as ``complete``."""
        written = False
        try:
            with self._client.beta.messages.stream(**self._request(messages, max_tokens)) as stream:
                for text in stream.text_stream:
                    written = written or bool(text)
                    yield text
                final = stream.get_final_message()
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as error:
            raise self._failed(error) from error
        self._check(final.stop_reason)
        if not written:
            raise LLMAnswerError(f"{self.name} returned an empty answer")
