"""SQLite backend: nodes and edges in tables, FTS5 for text, sqlite-vec for vectors.

Zero setup: one file, no server. The whole graph lives in ``chatlore.db``
inside the ChatLore home directory, or in memory for tests.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sqlite_vec

from chatlore.models import Conversation
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
from chatlore.store.mapping import conversation_to_graph, graph_to_conversation

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
    id    TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    props TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nodes_label ON nodes (label);
CREATE TABLE IF NOT EXISTS edges (
    src   TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    type  TEXT NOT NULL,
    dst   TEXT NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    props TEXT NOT NULL,
    PRIMARY KEY (src, type, dst)
);
CREATE INDEX IF NOT EXISTS edges_dst ON edges (dst, type);
CREATE TABLE IF NOT EXISTS node_search (
    id              INTEGER PRIMARY KEY,
    node_id         TEXT NOT NULL UNIQUE REFERENCES nodes (id) ON DELETE CASCADE,
    label           TEXT NOT NULL,
    conversation_id TEXT,
    source          TEXT,
    title           TEXT NOT NULL,
    text            TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS node_text USING fts5 (
    title,
    text,
    content = 'node_search',
    content_rowid = 'id',
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS node_search_ai AFTER INSERT ON node_search BEGIN
    INSERT INTO node_text (rowid, title, text) VALUES (new.id, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS node_search_ad AFTER DELETE ON node_search BEGIN
    INSERT INTO node_text (node_text, rowid, title, text)
    VALUES ('delete', old.id, old.title, old.text);
END;
"""

_WORD = re.compile(r"\w+", re.UNICODE)


class SQLiteStore(GraphStore):
    """A ``GraphStore`` on a single SQLite file, or ``":memory:"``."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        self.path = path
        self._connection = sqlite3.connect(str(path), isolation_level=None)
        self._connection.row_factory = sqlite3.Row
        self._connection.enable_load_extension(True)
        sqlite_vec.load(self._connection)
        self._connection.enable_load_extension(False)
        self._connection.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            # Durable against application crashes; only an OS crash or power loss
            # in the same instant can lose the last transaction. Much faster on disk.
            self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.executescript(_SCHEMA)
        self._set_meta("schema_version", str(SCHEMA_VERSION))
        self._dimension = self._read_dimension()

    def close(self) -> None:
        self._connection.close()

    # -- nodes and edges -----------------------------------------------------

    def upsert_nodes(self, nodes: Iterable[Node]) -> None:
        with self.transaction() as db:
            for node in nodes:
                db.execute(
                    "INSERT INTO nodes (id, label, props) VALUES (?, ?, ?) "
                    "ON CONFLICT (id) DO UPDATE SET label = excluded.label, props = excluded.props",
                    (node.id, node.label, _dumps(node.props)),
                )
                db.execute("DELETE FROM node_search WHERE node_id = ?", (node.id,))
                text = node.props.get("text")
                if isinstance(text, str) and text.strip():
                    db.execute(
                        "INSERT INTO node_search "
                        "(node_id, label, conversation_id, source, title, text) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            node.id,
                            node.label,
                            node.props.get("conversation_id"),
                            node.props.get("source"),
                            node.props.get("title") or "",
                            text,
                        ),
                    )

    def upsert_edges(self, edges: Iterable[Edge]) -> None:
        with self.transaction() as db:
            for edge in edges:
                db.execute(
                    "INSERT INTO edges (src, type, dst, props) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (src, type, dst) DO UPDATE SET props = excluded.props",
                    (edge.src, edge.type, edge.dst, _dumps(edge.props)),
                )

    def get_node(self, node_id: str) -> Node | None:
        row = self._connection.execute(
            "SELECT id, label, props FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return _node(row) if row is not None else None

    def delete_nodes(self, node_ids: Iterable[str]) -> None:
        ids = list(node_ids)
        with self.transaction() as db:
            for node_id in ids:
                if self._dimension is not None:
                    db.execute("DELETE FROM node_vec WHERE node_id = ?", (node_id,))
                # node_search rows and their FTS entries go with the node through
                # the foreign key cascade and the delete trigger.
                db.execute("DELETE FROM nodes WHERE id = ?", (node_id,))

    def neighbors(
        self,
        node_id: str,
        edge_types: Sequence[str] | None = None,
        direction: Direction = "out",
        limit: int = 100,
    ) -> list[tuple[Edge, Node]]:
        clauses: list[str] = []
        params: list[Any] = []
        if direction == "out":
            clauses.append("e.src = ?")
            params.append(node_id)
        elif direction == "in":
            clauses.append("e.dst = ?")
            params.append(node_id)
        else:
            clauses.append("(e.src = ? OR e.dst = ?)")
            params.extend([node_id, node_id])
        if edge_types:
            clauses.append(f"e.type IN ({', '.join('?' for _ in edge_types)})")
            params.extend(edge_types)
        params.append(limit)

        rows = self._connection.execute(
            "SELECT e.src, e.type, e.dst, e.props AS edge_props, "
            "n.id, n.label, n.props AS node_props "
            "FROM edges e JOIN nodes n ON n.id = CASE WHEN e.src = ? THEN e.dst ELSE e.src END "
            f"WHERE {' AND '.join(clauses)} ORDER BY e.type, e.dst, e.src LIMIT ?",
            [node_id, *params],
        ).fetchall()
        return [
            (
                Edge(row["src"], row["type"], row["dst"], _loads(row["edge_props"])),
                Node(row["id"], row["label"], _loads(row["node_props"])),
            )
            for row in rows
        ]

    def count_nodes(self, label: str | None = None) -> int:
        if label is None:
            row = self._connection.execute("SELECT COUNT(*) FROM nodes").fetchone()
        else:
            row = self._connection.execute(
                "SELECT COUNT(*) FROM nodes WHERE label = ?", (label,)
            ).fetchone()
        return int(row[0])

    # -- conversations -------------------------------------------------------

    def upsert_conversation(self, conversation: Conversation) -> None:
        nodes, edges = conversation_to_graph(conversation)
        with self.transaction():
            self._delete_messages(conversation.id)
            self.upsert_nodes(nodes)
            self.upsert_edges(edges)

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        node = self.get_node(conversation_id)
        if node is None or node.label != Label.CONVERSATION:
            return None
        messages = [
            neighbor
            for _, neighbor in self.neighbors(
                conversation_id, [EdgeType.HAS_MESSAGE], "out", limit=1_000_000
            )
        ]
        return graph_to_conversation(node, messages)

    def list_conversations(
        self, source: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[ConversationSummary]:
        where = "WHERE label = ?"
        params: list[Any] = [Label.CONVERSATION.value]
        if source is not None:
            where += " AND json_extract(props, '$.source') = ?"
            params.append(source)
        rows = self._connection.execute(
            "SELECT id, props FROM nodes "
            f"{where} "
            "ORDER BY COALESCE(json_extract(props, '$.updated_at'), "
            "json_extract(props, '$.created_at'), '') DESC, id "
            "LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        summaries: list[ConversationSummary] = []
        for row in rows:
            props = _loads(row["props"])
            summaries.append(
                ConversationSummary(
                    id=row["id"],
                    source=str(props.get("source")),
                    title=props.get("title"),
                    created_at=props.get("created_at"),
                    updated_at=props.get("updated_at"),
                    message_count=int(props.get("message_count", 0)),
                )
            )
        return summaries

    def delete_conversation(self, conversation_id: str) -> None:
        with self.transaction():
            self._delete_messages(conversation_id)
            self.delete_nodes([conversation_id])

    # -- search --------------------------------------------------------------

    def search_text(
        self,
        query: str,
        limit: int = 20,
        sources: Sequence[str] | None = None,
        labels: Sequence[str] | None = None,
    ) -> list[TextHit]:
        match = fts_query(query)
        if not match:
            return []
        clauses = ["node_text MATCH ?"]
        params: list[Any] = [match]
        if sources:
            clauses.append(f"s.source IN ({', '.join('?' for _ in sources)})")
            params.extend(sources)
        if labels:
            clauses.append(f"s.label IN ({', '.join('?' for _ in labels)})")
            params.extend(labels)
        params.append(limit)

        rows = self._connection.execute(
            "SELECT s.node_id, s.label, s.conversation_id, s.source, s.title, "
            "snippet(node_text, 1, '[', ']', '...', 16) AS snippet, "
            "bm25(node_text, 2.0, 1.0) AS rank "
            "FROM node_text JOIN node_search s ON s.id = node_text.rowid "
            f"WHERE {' AND '.join(clauses)} ORDER BY rank LIMIT ?",
            params,
        ).fetchall()
        return [
            TextHit(
                node_id=row["node_id"],
                label=row["label"],
                conversation_id=row["conversation_id"],
                source=row["source"],
                title=row["title"] or None,
                snippet=row["snippet"],
                score=-float(row["rank"]),
            )
            for row in rows
        ]

    def set_embedding(self, node_id: str, embedding: Sequence[float]) -> None:
        vector = list(embedding)
        if not vector:
            raise ValueError("embedding is empty")
        if self.get_node(node_id) is None:
            raise KeyError(node_id)
        with self.transaction() as db:
            self._ensure_vector_table(len(vector))
            db.execute("DELETE FROM node_vec WHERE node_id = ?", (node_id,))
            db.execute(
                "INSERT INTO node_vec (node_id, embedding) VALUES (?, ?)",
                (node_id, sqlite_vec.serialize_float32(vector)),
            )

    def search_vector(
        self, embedding: Sequence[float], limit: int = 20, labels: Sequence[str] | None = None
    ) -> list[VectorHit]:
        if self._dimension is None:
            return []
        vector = list(embedding)
        if len(vector) != self._dimension:
            raise ValueError(f"embedding has {len(vector)} dimensions, store has {self._dimension}")
        wanted = set(labels or [])
        # Label filtering happens after the k-nearest query, so ask for extra candidates.
        k = limit * 4 if wanted else limit
        rows = self._connection.execute(
            "SELECT v.node_id, v.distance, n.label FROM node_vec v "
            "JOIN nodes n ON n.id = v.node_id "
            "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
            (sqlite_vec.serialize_float32(vector), k),
        ).fetchall()
        hits = [
            VectorHit(row["node_id"], row["label"], float(row["distance"]))
            for row in rows
            if not wanted or row["label"] in wanted
        ]
        return hits[:limit]

    # -- internals -----------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group writes into one transaction. Nested calls join the outer one."""
        db = self._connection
        if db.in_transaction:
            yield db
            return
        db.execute("BEGIN")
        try:
            yield db
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    def _delete_messages(self, conversation_id: str) -> None:
        rows = self._connection.execute(
            "SELECT dst FROM edges WHERE src = ? AND type = ?",
            (conversation_id, EdgeType.HAS_MESSAGE.value),
        ).fetchall()
        self.delete_nodes(row["dst"] for row in rows)

    def _ensure_vector_table(self, dimension: int) -> None:
        if self._dimension is None:
            self._connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS node_vec USING vec0 ("
                f"node_id TEXT PRIMARY KEY, embedding FLOAT[{dimension}])"
            )
            self._set_meta("embedding_dimension", str(dimension))
            self._dimension = dimension
        elif dimension != self._dimension:
            raise ValueError(f"embedding has {dimension} dimensions, store has {self._dimension}")

    def _read_dimension(self) -> int | None:
        row = self._connection.execute(
            "SELECT value FROM meta WHERE key = 'embedding_dimension'"
        ).fetchone()
        return int(row["value"]) if row is not None else None

    def _set_meta(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 query: every word must match, in any order."""
    words = _WORD.findall(query)
    return " ".join(f'"{word}"' for word in words)


def _node(row: sqlite3.Row) -> Node:
    return Node(row["id"], row["label"], _loads(row["props"]))


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: str) -> dict[str, Any]:
    data = json.loads(value)
    return data if isinstance(data, dict) else {}
