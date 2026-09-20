"""Embedding models and a disk cache for their output.

The default embedder runs locally through fastembed (ONNX, no PyTorch), so
semantic search works offline and no text leaves the machine. Anything with the
``Embedder`` shape can replace it.

Vectors are cached by model name and text hash in a small SQLite file. The
graph database can be dropped and rebuilt without paying for embeddings again.
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
from array import array
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, Self

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
MODEL_ENV = "CHATLORE_EMBEDDING_MODEL"


class EmbeddingError(Exception):
    """The embedding model could not be loaded or used."""


class Embedder(Protocol):
    """What the pipeline and search need from an embedding model."""

    name: str

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed stored text."""
        ...

    def embed_query(self, text: str) -> list[float]:
        """Embed a search query. Some models treat queries differently from passages."""
        ...


class FastEmbedEmbedder:
    """Local embeddings through fastembed. The model loads on first use."""

    def __init__(self, model: str = DEFAULT_MODEL, cache_dir: Path | None = None) -> None:
        self.name = model
        self._cache_dir = cache_dir
        self._model: Any = None

    def _load(self) -> Any:
        if self._model is None:
            # The Hugging Face native downloader crashes on some Windows machines where
            # security or licensing software injects a DLL into every process. The
            # pure-Python downloader is slower but works everywhere; models are small.
            os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            try:
                from fastembed import TextEmbedding  # heavy, so loaded on first use

                cache = str(self._cache_dir) if self._cache_dir is not None else None
                try:
                    # Once the model is on disk, load it without asking the hub anything:
                    # faster, silent, and it keeps search working offline.
                    self._model = TextEmbedding(self.name, cache_dir=cache, local_files_only=True)
                except Exception:
                    self._model = TextEmbedding(self.name, cache_dir=cache)
            except Exception as error:
                raise EmbeddingError(
                    f"could not load embedding model '{self.name}': {error}"
                ) from error
        return self._model

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        return [[float(x) for x in vector] for vector in self._load().passage_embed(list(texts))]

    def embed_query(self, text: str) -> list[float]:
        vector = next(iter(self._load().query_embed([text])))
        return [float(x) for x in vector]


def make_embedder(home: Path) -> Embedder:
    """Return the configured embedder, keeping model files under ``home``."""
    model = os.environ.get(MODEL_ENV) or DEFAULT_MODEL
    return FastEmbedEmbedder(model, cache_dir=home / "cache" / "models")


def normalise(vector: Sequence[float]) -> list[float]:
    """Scale a vector to unit length so L2 distance ranks like cosine similarity."""
    length = math.sqrt(sum(x * x for x in vector))
    if length == 0.0:
        return [float(x) for x in vector]
    return [float(x) / length for x in vector]


def text_hash(text: str) -> str:
    """Hash used as the cache key for a piece of text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class EmbeddingCache:
    """Vectors on disk, keyed by model name and text hash."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path))
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS embeddings ("
            "model TEXT NOT NULL, text_hash TEXT NOT NULL, vector BLOB NOT NULL, "
            "PRIMARY KEY (model, text_hash))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_many(self, model: str, hashes: Sequence[str]) -> dict[str, list[float]]:
        found: dict[str, list[float]] = {}
        for digest in hashes:
            row = self._connection.execute(
                "SELECT vector FROM embeddings WHERE model = ? AND text_hash = ?",
                (model, digest),
            ).fetchone()
            if row is not None:
                values = array("f")
                values.frombytes(row[0])
                found[digest] = list(values)
        return found

    def put_many(self, model: str, vectors: Mapping[str, Sequence[float]]) -> None:
        self._connection.executemany(
            "INSERT OR REPLACE INTO embeddings (model, text_hash, vector) VALUES (?, ?, ?)",
            [(model, digest, array("f", vector).tobytes()) for digest, vector in vectors.items()],
        )
        self._connection.commit()
