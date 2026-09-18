"""Test doubles."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

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
