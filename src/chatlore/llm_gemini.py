"""Gemini through Google's own API, with the official SDK.

Chosen with ``CHATLORE_LLM_PROVIDER=gemini`` and installed with
``pip install 'chatlore[gemini]'``. It has the same shape as the
OpenAI-compatible client, so extraction and chat do not know which one runs.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import httpx
import httpx2
from google import genai
from google.genai import errors, types

from chatlore.llm import ChatMessage, Completion, LLMAnswerError, LLMError

_LEVELS = {
    "off": types.ThinkingLevel.MINIMAL,
    "low": types.ThinkingLevel.LOW,
    "medium": types.ThinkingLevel.MEDIUM,
    "high": types.ThinkingLevel.HIGH,
}
"""Reasoning as Gemini's thinking levels; current models cannot stop thinking, so off is minimal."""

_DECLINED = {
    types.FinishReason.SAFETY,
    types.FinishReason.RECITATION,
    types.FinishReason.BLOCKLIST,
    types.FinishReason.PROHIBITED_CONTENT,
    types.FinishReason.SPII,
}


class GeminiLLM:
    """A Gemini model behind the Gemini API."""

    def __init__(
        self,
        model: str,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float = 120.0,
        max_retries: int = 2,
        http_client: httpx.Client | None = None,
        reasoning: str = "default",
    ) -> None:
        self.name = model
        self.base_url = base_url or "https://generativelanguage.googleapis.com"
        self.reasoning = reasoning
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                base_url=self.base_url,
                timeout=int(timeout * 1000),
                retry_options=types.HttpRetryOptions(attempts=max_retries + 1),
                httpx_client=http_client,
            ),
        )

    def close(self) -> None:
        self._client.close()

    def _request(
        self, messages: Sequence[ChatMessage], json_output: bool, max_tokens: int | None
    ) -> tuple[list[types.ContentUnionDict], types.GenerateContentConfig]:
        """The turns, with the assistant as "model", and the system turns as the instruction."""
        contents: list[types.ContentUnionDict] = [
            types.Content(
                role="model" if message.role == "assistant" else "user",
                parts=[types.Part(text=message.content)],
            )
            for message in messages
            if message.role != "system"
        ]
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        level = _LEVELS.get(self.reasoning)
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=max_tokens,
            response_mime_type="application/json" if json_output else None,
            thinking_config=types.ThinkingConfig(thinking_level=level) if level else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        return contents, config

    def _failed(self, error: Exception) -> LLMError:
        if isinstance(error, errors.APIError):
            return LLMError(
                f"{self.name} at {self.base_url} failed with HTTP {error.code}: {error.message}"
            )
        return LLMError(f"could not reach {self.base_url}: {error}")

    def _check(self, finish_reason: types.FinishReason | None) -> None:
        if finish_reason == types.FinishReason.MAX_TOKENS:
            raise LLMAnswerError(f"{self.name} stopped at the token limit before finishing")
        if finish_reason in _DECLINED:
            raise LLMAnswerError(f"{self.name} declined to answer ({finish_reason.value})")

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        json_output: bool = False,
        max_tokens: int | None = None,
    ) -> Completion:
        contents, config = self._request(messages, json_output, max_tokens)
        try:
            response = self._client.models.generate_content(
                model=self.name, contents=contents, config=config
            )
        except (errors.APIError, httpx.HTTPError, httpx2.HTTPError) as error:
            raise self._failed(error) from error
        if not response.candidates:
            raise LLMAnswerError(f"{self.name} returned no answer")
        self._check(response.candidates[0].finish_reason)
        text = response.text or ""
        if not text.strip():
            raise LLMAnswerError(f"{self.name} returned an empty answer")
        usage = response.usage_metadata
        return Completion(
            text=text,
            model=response.model_version or self.name,
            input_tokens=(usage.prompt_token_count or 0) if usage else 0,
            output_tokens=(
                (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
                if usage
                else 0
            ),
        )

    def stream(
        self, messages: Sequence[ChatMessage], *, max_tokens: int | None = None
    ) -> Iterator[str]:
        """Yield the answer as it is written, with the same errors as ``complete``."""
        contents, config = self._request(messages, False, max_tokens)
        written = False
        finish_reason: types.FinishReason | None = None
        try:
            for chunk in self._client.models.generate_content_stream(
                model=self.name, contents=contents, config=config
            ):
                if chunk.candidates and chunk.candidates[0].finish_reason:
                    finish_reason = chunk.candidates[0].finish_reason
                text = chunk.text
                if text:
                    written = True
                    yield text
        except (errors.APIError, httpx.HTTPError, httpx2.HTTPError) as error:
            raise self._failed(error) from error
        self._check(finish_reason)
        if not written:
            raise LLMAnswerError(f"{self.name} returned an empty answer")
