"""Test doubles."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import threading
from collections.abc import Iterator, Sequence

from chatlore.llm import ChatMessage, Completion, LLMAnswerError, LLMError

DIMENSION = 32


class FakeEmbedder:
    """Deterministic bag-of-words vectors: texts sharing words land close together.

    No model, no network, and it counts how many texts it was asked to embed so
    tests can prove that caching and idempotence avoid repeated work.
    """

    def __init__(self, name: str = "fake-model") -> None:
        self.name = name
        self.embedded: list[str] = []

    def _vector(self, text: str) -> list[float]:
        vector = [0.0] * DIMENSION
        for word in text.lower().split():
            token = "".join(ch for ch in word if ch.isalnum())
            if token:
                bucket = int(hashlib.sha256(token.encode()).hexdigest(), 16) % DIMENSION
                vector[bucket] += 1.0
        return vector

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        self.embedded.extend(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class FakeLLM:
    """Answers every kind of ChatLore request from what it is sent, without a model.

    Extraction: capitalised words become entities and consecutive entities in a
    passage are related, so tests can predict the graph from the text. Summary:
    an entity's descriptions joined. Duplicates: two names are the same when one
    starts with the other, ignoring case. Topic report: titled after its first
    entity. Each kind of request is recorded separately, and any request, counted
    across all kinds, can be made to return an unreadable answer, an empty one,
    or to fail as if the model were unreachable.
    """

    def __init__(
        self,
        name: str = "fake-llm",
        broken_requests: Sequence[int] = (),
        fail_on_request: int | None = None,
        empty_requests: Sequence[int] = (),
    ) -> None:
        self.name = name
        self.requests: list[list[str]] = []
        self.summary_requests: list[list[str]] = []
        self.duplicate_requests: list[list[tuple[str, str]]] = []
        self.report_requests: list[list[str]] = []
        self.chat_requests: list[str] = []
        self._broken = set(broken_requests)
        self._empty = set(empty_requests)
        self._fail_on = fail_on_request
        self._calls = 0
        self._lock = threading.Lock()

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        json_output: bool = False,
        max_tokens: int | None = None,
    ) -> Completion:
        content = messages[-1].content
        with self._lock:
            self._calls += 1
            number = self._calls
            if content.startswith("### Entity "):
                answer = self._summaries(content)
            elif content.startswith("### Pair "):
                answer = self._duplicates(content)
            elif content.startswith("### Entities"):
                answer = self._report(content)
            else:
                answer = self._extraction(content)
        if number == self._fail_on:
            raise LLMError("fake model is unreachable")
        if number in self._empty:
            raise LLMAnswerError("fake model returned an empty answer")
        if number in self._broken:
            return Completion("this is not JSON", self.name, 10, 5)
        return answer

    def _extraction(self, content: str) -> Completion:
        passages = re.split(r"^### Passage \d+\n", content, flags=re.MULTILINE)[1:]
        passages = [passage.strip() for passage in passages]
        self.requests.append(passages)
        answer = {
            "passages": [
                self._passage(index, passage) for index, passage in enumerate(passages, start=1)
            ]
        }
        return Completion(json.dumps(answer), self.name, 10 * len(passages), 20 * len(passages))

    def _summaries(self, content: str) -> Completion:
        blocks = re.split(r"^### Entity \d+: ", content, flags=re.MULTILINE)[1:]
        self.summary_requests.append([block.split(" (", 1)[0] for block in blocks])
        summaries = [
            {
                "entity": index,
                "summary": " / ".join(
                    line[2:] for line in block.splitlines() if line.startswith("- ")
                ),
            }
            for index, block in enumerate(blocks, start=1)
        ]
        return Completion(json.dumps({"summaries": summaries}), self.name, 5, 5)

    def _duplicates(self, content: str) -> Completion:
        blocks = re.split(r"^### Pair \d+\n", content, flags=re.MULTILINE)[1:]
        pairs = []
        for block in blocks:
            first, second = (line[2:].split(" (", 1)[0] for line in block.strip().splitlines())
            pairs.append((first, second))
        self.duplicate_requests.append(pairs)
        verdicts = [
            {
                "pair": index,
                "same": first.lower().startswith(second.lower())
                or second.lower().startswith(first.lower()),
            }
            for index, (first, second) in enumerate(pairs, start=1)
        ]
        return Completion(json.dumps({"pairs": verdicts}), self.name, 5, 5)

    def _report(self, content: str) -> Completion:
        names = [
            line[2:].split(" (", 1)[0]
            for line in content.split("### Relationships")[0].splitlines()
            if line.startswith("- ") and " (" in line
        ]
        self.report_requests.append(names)
        report = {
            "title": f"About {names[0]}",
            "summary": f"A topic with {', '.join(names)}.",
            "findings": [f"{name} is part of it." for name in names],
        }
        return Completion(json.dumps(report), self.name, 5, 5)

    @staticmethod
    def _passage(index: int, text: str) -> dict[str, object]:
        names = list(dict.fromkeys(re.findall(r"\b[A-Z][A-Za-z]+\b", text)))
        return {
            "passage": index,
            "entities": [
                {"name": name, "type": "concept", "description": f"{name} appears in: {text}"}
                for name in names
            ],
            "relationships": [
                {"source": a, "target": b, "description": f"{a} with {b}", "strength": 6}
                for a, b in itertools.pairwise(names)
            ],
        }

    def stream(
        self, messages: Sequence[ChatMessage], *, max_tokens: int | None = None
    ) -> Iterator[str]:
        """Answer a chat request by naming each source's title and citing it."""
        content = messages[-1].content
        with self._lock:
            self._calls += 1
            number = self._calls
            self.chat_requests.append(content)
        if number == self._fail_on:
            raise LLMError("fake model is unreachable")
        if number in self._empty:
            raise LLMAnswerError("fake model returned an empty answer")
        yield "From your conversations: "
        for index, title in re.findall(r"^\[(\d+)\] (.+?)(?: \(.*\))?$", content, re.MULTILINE):
            yield f"{title} [{index}]. "

    def close(self) -> None:
        pass
