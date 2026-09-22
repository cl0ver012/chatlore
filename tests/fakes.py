"""Test doubles."""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import threading
from collections.abc import Sequence

from chatlore.llm import ChatMessage, Completion, LLMError

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
    """Answers extraction requests from the passages it is sent, without a model.

    Capitalised words become entities and consecutive entities in a passage are
    related, so tests can predict the graph from the text. A summary joins an
    entity's descriptions. It records extraction and summary requests separately,
    and can return an unreadable answer or fail outright on chosen requests.
    """

    def __init__(
        self,
        name: str = "fake-llm",
        broken_requests: Sequence[int] = (),
        fail_on_request: int | None = None,
    ) -> None:
        self.name = name
        self.requests: list[list[str]] = []
        self.summary_requests: list[list[str]] = []
        self._broken = set(broken_requests)
        self._fail_on = fail_on_request
        self._lock = threading.Lock()

    def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        json_output: bool = False,
        max_tokens: int | None = None,
    ) -> Completion:
        if messages[-1].content.startswith("### Entity"):
            return self._summaries(messages[-1].content)
        passages = re.split(r"^### Passage \d+\n", messages[-1].content, flags=re.MULTILINE)[1:]
        passages = [passage.strip() for passage in passages]
        with self._lock:
            self.requests.append(passages)
            number = len(self.requests) + len(self.summary_requests)
        if number == self._fail_on:
            raise LLMError("fake model is unreachable")
        if number in self._broken:
            return Completion("this is not JSON", self.name, 10, 5)
        answer = {
            "passages": [
                self._passage(index, passage) for index, passage in enumerate(passages, start=1)
            ]
        }
        return Completion(json.dumps(answer), self.name, 10 * len(passages), 20 * len(passages))

    def _summaries(self, content: str) -> Completion:
        blocks = re.split(r"^### Entity \d+: ", content, flags=re.MULTILINE)[1:]
        names = [block.split(" (", 1)[0] for block in blocks]
        with self._lock:
            self.summary_requests.append(names)
            number = len(self.requests) + len(self.summary_requests)
        if number == self._fail_on:
            raise LLMError("fake model is unreachable")
        if number in self._broken:
            return Completion("this is not JSON", self.name, 10, 5)
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

    def close(self) -> None:
        pass
