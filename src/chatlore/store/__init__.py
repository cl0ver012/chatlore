"""Graph storage: one interface, pluggable backends.

SQLite, in the library folder, is the default. ``CHATLORE_STORE=falkordb`` keeps
the graph in a FalkorDB server instead: ``CHATLORE_FALKORDB_URL`` says where
(``redis://localhost:6379`` by default) and ``CHATLORE_FALKORDB_GRAPH`` which
graph (``chatlore``). The FalkorDB client is an optional dependency, installed
with ``chatlore[falkordb]``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from chatlore.store.base import (
    ConversationSummary,
    Direction,
    Edge,
    EdgeType,
    GraphStore,
    Label,
    Node,
    TextHit,
    VectorHit,
)
from chatlore.store.sqlite import SQLiteStore

if TYPE_CHECKING:
    from chatlore.store.falkordb import FalkorDBStore

DATABASE_NAME = "chatlore.db"
STORE_ENV = "CHATLORE_STORE"
FALKORDB_URL_ENV = "CHATLORE_FALKORDB_URL"
FALKORDB_GRAPH_ENV = "CHATLORE_FALKORDB_GRAPH"
FALKORDB_DEFAULT_URL = "redis://localhost:6379"
FALKORDB_DEFAULT_GRAPH = "chatlore"

__all__ = [
    "DATABASE_NAME",
    "FALKORDB_GRAPH_ENV",
    "FALKORDB_URL_ENV",
    "STORE_ENV",
    "ConversationSummary",
    "Direction",
    "Edge",
    "EdgeType",
    "GraphStore",
    "Label",
    "Node",
    "SQLiteStore",
    "TextHit",
    "VectorHit",
    "describe_store",
    "drop_store",
    "open_store",
]


def open_store(home: Path) -> GraphStore:
    """Open the library's store, creating it if needed."""
    home.mkdir(parents=True, exist_ok=True)
    if _backend() == "falkordb":
        return _falkordb()
    return SQLiteStore(home / DATABASE_NAME)


def drop_store(home: Path) -> None:
    """Delete the library's store, so the next ``open_store`` starts empty."""
    if _backend() == "falkordb":
        store = _falkordb()
        try:
            store.drop()
        finally:
            store.close()
        return
    for name in (DATABASE_NAME, f"{DATABASE_NAME}-wal", f"{DATABASE_NAME}-shm"):
        (home / name).unlink(missing_ok=True)


def describe_store(home: Path) -> str:
    """Where the library's graph is kept, for people to read."""
    if _backend() == "falkordb":
        url, graph = _falkordb_settings()
        return f"FalkorDB graph '{graph}' at {_without_password(url)}"
    return f"SQLite at {home / DATABASE_NAME}"


def _without_password(url: str) -> str:
    """The URL with any password replaced, so it can be shown."""
    parts = urlsplit(url)
    if parts.password is None:
        return url
    user = f"{parts.username}:" if parts.username else ":"
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit(parts._replace(netloc=f"{user}***@{host}"))


def _backend() -> str:
    backend = (os.environ.get(STORE_ENV) or "sqlite").strip().lower()
    if backend not in ("sqlite", "falkordb"):
        raise ValueError(f"{STORE_ENV} must be sqlite or falkordb, not {backend!r}")
    return backend


def _falkordb_settings() -> tuple[str, str]:
    return (
        os.environ.get(FALKORDB_URL_ENV) or FALKORDB_DEFAULT_URL,
        os.environ.get(FALKORDB_GRAPH_ENV) or FALKORDB_DEFAULT_GRAPH,
    )


def _falkordb() -> FalkorDBStore:
    try:
        from chatlore.store.falkordb import FalkorDBStore
    except ImportError as error:
        raise RuntimeError(
            f"{STORE_ENV}=falkordb needs the FalkorDB client: pip install 'chatlore[falkordb]'"
        ) from error
    return FalkorDBStore(*_falkordb_settings())
