"""Graph storage: one interface, pluggable backends."""

from __future__ import annotations

from pathlib import Path

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

DATABASE_NAME = "chatlore.db"

__all__ = [
    "DATABASE_NAME",
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
    "open_store",
]


def open_store(home: Path) -> GraphStore:
    """Open the store that lives in a ChatLore home directory, creating it if needed."""
    home.mkdir(parents=True, exist_ok=True)
    return SQLiteStore(home / DATABASE_NAME)
