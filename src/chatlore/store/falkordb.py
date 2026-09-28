"""A ``GraphStore`` on FalkorDB, the graph database that runs as a Redis module.

Every node carries the label ``Node`` and these properties:

- ``_id`` and ``_label``, the node's id and ChatLore label, both indexed;
- ``_props``, all of its props as JSON, since FalkorDB properties cannot hold
  maps or nulls;
- a ``p_<name>`` copy of each prop that is a string, number, or boolean, so
  ``find_nodes`` and search filters can match on it (``text`` is left out: it
  is only ever read back from ``_props``);
- ``_ft_title`` and ``_ft_text``, folded copies of the title and text of a node
  that has text, in a full-text index;
- ``_embedding``, the node's vector, in a vector index.

Edges are relationships of their own type with their props as JSON in
``_props``, and store-wide state lives on ``Meta`` nodes.

FalkorDB differs from the SQLite store in ways this module makes up for. Its
full-text search does not fold accents, so the indexed copies and the queries
are folded here, and matching words are marked in snippets here too. It accepts
a vector of the wrong length and an edge to a missing node without complaint, so
both are checked first. It returns at most 10,000 rows per query, so long reads
are fetched in pages. Every query is atomic, but FalkorDB has no transactions
that span queries, so ``transaction`` only groups calls for the reader.
"""

from __future__ import annotations

import atexit
import json
import re
import threading
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from falkordb import FalkorDB
from redis.exceptions import ResponseError

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

_PAGE = 5_000
"""Rows fetched or written per query, well under FalkorDB's 10,000-row limit."""
_TOKEN = re.compile(r"[^\W_]+")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SNIPPET_TOKENS = 16
_UNSEARCHABLE = frozenset({"text"})
_DIMENSION = "embedding_dimension"
_ALL = 1_000_000_000

# One client per server for the whole process. Connecting takes several round
# trips, seconds to a server far away, and the web interface and MCP server open
# the store for every request. Indexes are created once per server and graph.
_clients: dict[str, FalkorDB] = {}
_prepared: set[tuple[str, str]] = set()
_lock = threading.Lock()


def _client(url: str) -> FalkorDB:
    with _lock:
        if url not in _clients:
            # Networks drop connections that sit idle, so one unused for half a
            # minute is checked before it is used again.
            _clients[url] = FalkorDB.from_url(url, health_check_interval=30, socket_keepalive=True)
        return _clients[url]


@atexit.register
def _disconnect() -> None:
    """Close every client's sockets; a client made from a URL does not close them itself."""
    with _lock:
        for client in _clients.values():
            client.connection.connection_pool.disconnect()
        _clients.clear()


class FalkorDBStore(GraphStore):
    """A ``GraphStore`` on one graph of a FalkorDB server."""

    def __init__(self, url: str, graph: str) -> None:
        self.url = url
        self.name = graph
        self._db = _client(url)
        self._graph = self._db.select_graph(graph)
        with _lock:
            if (url, graph) not in _prepared:
                self._create_indexes()
                _prepared.add((url, graph))
        dimension = self.get_meta(_DIMENSION)
        self._dimension = int(dimension) if dimension is not None else None

    def close(self) -> None:
        """Nothing to release: the connection stays open for the next store on this server."""

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Group writes for the reader. Each query is atomic on its own in FalkorDB."""
        yield

    def drop(self) -> None:
        """Delete the whole graph."""
        if self.name in self._db.list_graphs():
            self._graph.delete()
        _prepared.discard((self.url, self.name))

    # -- nodes and edges -----------------------------------------------------

    def upsert_nodes(self, nodes: Iterable[Node]) -> None:
        # One row per node, the last one given winning, as with SQLite: within one
        # query FalkorDB would apply the first.
        rows = list({node.id: _node_row(node) for node in nodes}.values())
        for page in _pages(rows):
            # Replacing every property keeps the indexes in step; the embedding
            # is carried over, as the SQLite store keeps it apart from the node.
            self._query(
                "UNWIND $rows AS row "
                "MERGE (n:Node {_id: row._id}) "
                "WITH n, row, n._embedding AS embedding "
                "SET n = row "
                "SET n._embedding = embedding",
                {"rows": page},
            )

    def upsert_edges(self, edges: Iterable[Edge]) -> None:
        # One row per edge, the last one given winning, as for nodes.
        by_type: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
        for edge in edges:
            by_type.setdefault(_name(edge.type), {})[(edge.src, edge.dst)] = {
                "src": edge.src,
                "dst": edge.dst,
                "props": _dumps(edge.props),
            }
        ends = {end for rows in by_type.values() for pair in rows for end in pair}
        missing = ends - self._existing(ends)
        if missing:
            raise ValueError(
                f"FOREIGN KEY constraint failed: no node {sorted(missing)[0]!r} for an edge"
            )
        for edge_type, rows in by_type.items():
            for page in _pages(list(rows.values())):
                self._query(
                    "UNWIND $rows AS row "
                    "MATCH (a:Node {_id: row.src}), (b:Node {_id: row.dst}) "
                    f"MERGE (a)-[r:{edge_type}]->(b) "
                    "SET r._props = row.props",
                    {"rows": page},
                )

    def get_node(self, node_id: str) -> Node | None:
        rows = self._query(
            "MATCH (n:Node {_id: $id}) RETURN n._id, n._label, n._props", {"id": node_id}
        )
        return _node(rows[0]) if rows else None

    def get_nodes(self, node_ids: Iterable[str]) -> dict[str, Node]:
        found: dict[str, Node] = {}
        for page in _pages(list(dict.fromkeys(node_ids))):
            rows = self._query(
                "MATCH (n:Node) WHERE n._id IN $ids RETURN n._id, n._label, n._props",
                {"ids": page},
            )
            found.update((row[0], _node(row)) for row in rows)
        return found

    def delete_nodes(self, node_ids: Iterable[str]) -> None:
        for page in _pages(list(node_ids)):
            # Edges and index entries go with the node.
            self._query("UNWIND $ids AS id MATCH (n:Node {_id: id}) DETACH DELETE n", {"ids": page})

    def neighbors(
        self,
        node_id: str,
        edge_types: Sequence[str] | None = None,
        direction: Direction = "out",
        limit: int = 100,
    ) -> list[tuple[Edge, Node]]:
        rows = self._paged(
            f"MATCH (a:Node {{_id: $id}}) MATCH {_pattern(edge_types, direction)} "
            f"RETURN {_NEIGHBOR} ORDER BY type(r), endNode(r)._id, startNode(r)._id",
            {"id": node_id},
            limit,
        )
        return [_neighbor(row) for row in rows]

    def neighbors_many(
        self,
        node_ids: Iterable[str],
        edge_types: Sequence[str] | None = None,
        direction: Direction = "out",
    ) -> dict[str, list[tuple[Edge, Node]]]:
        wanted = list(dict.fromkeys(node_ids))
        found: dict[str, list[tuple[Edge, Node]]] = {node_id: [] for node_id in wanted}
        for page in _pages(wanted):
            rows = self._paged(
                f"MATCH (a:Node) WHERE a._id IN $ids MATCH {_pattern(edge_types, direction)} "
                f"RETURN a._id, {_NEIGHBOR} "
                "ORDER BY a._id, type(r), endNode(r)._id, startNode(r)._id",
                {"ids": page},
                _ALL,
            )
            for row in rows:
                found[row[0]].append(_neighbor(row[1:]))
        return found

    def find_nodes(
        self, label: str, where: Mapping[str, Any] | None = None, limit: int = 1_000_000
    ) -> list[Node]:
        clauses = ["n._label = $label"]
        params: dict[str, Any] = {"label": label}
        for number, (key, value) in enumerate((where or {}).items()):
            if not _NAME.fullmatch(key):
                raise ValueError(f"invalid property name: {key!r}")
            clauses.append(f"n.p_{key} = $value{number}")
            params[f"value{number}"] = value
        rows = self._paged(
            f"MATCH (n:Node) WHERE {' AND '.join(clauses)} "
            "RETURN n._id, n._label, n._props ORDER BY n._id",
            params,
            limit,
        )
        return [_node(row) for row in rows]

    def count_nodes(self, label: str | None = None) -> int:
        if label is None:
            rows = self._query("MATCH (n:Node) RETURN count(n)")
        else:
            rows = self._query(
                "MATCH (n:Node) WHERE n._label = $label RETURN count(n)", {"label": label}
            )
        return int(rows[0][0])

    # -- conversations -------------------------------------------------------

    def upsert_conversation(self, conversation: Conversation) -> None:
        nodes, edges = conversation_to_graph(conversation)
        keep = {message.id for message in conversation.messages}
        # As in SQLite: messages still present are updated in place, so their
        # chunks and embeddings survive; only messages that disappeared go.
        self._delete_messages(conversation.id, keep=keep)
        self._query(
            f"MATCH (:Node {{_id: $id}})-[:{EdgeType.HAS_MESSAGE}]->(:Node)"
            f"-[r:{EdgeType.REPLIES_TO}]->() DELETE r",
            {"id": conversation.id},
        )
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
        where = "n._label = $label" + (" AND n.p_source = $source" if source is not None else "")
        rows = self._query(
            f"MATCH (n:Node) WHERE {where} RETURN n._id, n._props "
            "ORDER BY coalesce(n.p_updated_at, n.p_created_at, '') DESC, n._id "
            "SKIP $offset LIMIT $limit",
            {"label": Label.CONVERSATION.value, "source": source, "offset": offset, "limit": limit},
        )
        summaries: list[ConversationSummary] = []
        for node_id, raw in rows:
            props = _loads(raw)
            summaries.append(
                ConversationSummary(
                    id=node_id,
                    source=str(props.get("source")),
                    title=props.get("title"),
                    created_at=props.get("created_at"),
                    updated_at=props.get("updated_at"),
                    message_count=int(props.get("message_count", 0)),
                )
            )
        return summaries

    def delete_conversation(self, conversation_id: str) -> None:
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
        words = fold_words(query)
        if not words:
            return []
        clauses = []
        if sources:
            clauses.append("node.p_source IN $sources")
        if labels:
            clauses.append("node._label IN $labels")
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        rows = self._query(
            "CALL db.idx.fulltext.queryNodes('Node', $query) YIELD node, score "
            f"{where}"
            "RETURN node._id, node._label, node._props, score "
            "ORDER BY score DESC, node._id LIMIT $limit",
            {
                "query": " ".join(words),
                "sources": list(sources or []),
                "labels": list(labels or []),
                "limit": limit,
            },
        )
        hits: list[TextHit] = []
        for node_id, label, raw, score in rows:
            props = _loads(raw)
            hits.append(
                TextHit(
                    node_id=node_id,
                    label=label,
                    conversation_id=props.get("conversation_id"),
                    source=props.get("source"),
                    title=props.get("title") or None,
                    snippet=snippet(str(props.get("text", "")), set(words)),
                    score=float(score),
                )
            )
        return hits

    def set_embedding(self, node_id: str, embedding: Sequence[float]) -> None:
        vector = [float(value) for value in embedding]
        if not vector:
            raise ValueError("embedding is empty")
        if self.get_node(node_id) is None:
            raise KeyError(node_id)
        if self._dimension is None:
            self._query(
                "CREATE VECTOR INDEX FOR (n:Node) ON (n._embedding) "
                f"OPTIONS {{dimension: {len(vector)}, similarityFunction: 'euclidean'}}"
            )
            self.set_meta(_DIMENSION, str(len(vector)))
            self._dimension = len(vector)
        elif len(vector) != self._dimension:
            raise ValueError(f"embedding has {len(vector)} dimensions, store has {self._dimension}")
        self._query(
            "MATCH (n:Node {_id: $id}) SET n._embedding = vecf32($vector)",
            {"id": node_id, "vector": vector},
        )

    def nodes_without_embedding(self, label: str, limit: int = 100) -> list[Node]:
        rows = self._paged(
            "MATCH (n:Node) WHERE n._label = $label AND n._embedding IS NULL "
            "RETURN n._id, n._label, n._props ORDER BY n._id",
            {"label": label},
            limit,
        )
        return [_node(row) for row in rows]

    def count_embeddings(self) -> int:
        rows = self._query("MATCH (n:Node) WHERE n._embedding IS NOT NULL RETURN count(n)")
        return int(rows[0][0])

    def clear_embeddings(self) -> None:
        if self._dimension is not None:
            self._query("DROP VECTOR INDEX FOR (n:Node) ON (n._embedding)")
        self._query("MATCH (n:Node) WHERE n._embedding IS NOT NULL SET n._embedding = NULL")
        self._query(
            "MATCH (m:Meta) WHERE m.key IN $keys DELETE m",
            {"keys": [_DIMENSION, "embedding_model"]},
        )
        self._dimension = None

    def get_meta(self, key: str) -> str | None:
        rows = self._query("MATCH (m:Meta {key: $key}) RETURN m.value", {"key": key})
        return str(rows[0][0]) if rows else None

    def set_meta(self, key: str, value: str) -> None:
        self._query("MERGE (m:Meta {key: $key}) SET m.value = $value", {"key": key, "value": value})

    def search_vector(
        self, embedding: Sequence[float], limit: int = 20, labels: Sequence[str] | None = None
    ) -> list[VectorHit]:
        if self._dimension is None:
            return []
        vector = [float(value) for value in embedding]
        if len(vector) != self._dimension:
            raise ValueError(f"embedding has {len(vector)} dimensions, store has {self._dimension}")
        wanted = set(labels or [])
        # Label filtering happens after the k-nearest query, so ask for extra candidates.
        k = limit * 4 if wanted else limit
        if k <= 0:
            return []
        rows = self._query(
            "CALL db.idx.vector.queryNodes('Node', '_embedding', $k, vecf32($vector)) "
            "YIELD node, score RETURN node._id, node._label, score ORDER BY score, node._id",
            {"k": k, "vector": vector},
        )
        hits = [
            VectorHit(node_id, label, float(score))
            for node_id, label, score in rows
            if not wanted or label in wanted
        ]
        return hits[:limit]

    # -- internals -----------------------------------------------------------

    def _query(self, query: str, params: dict[str, Any] | None = None) -> list[list[Any]]:
        rows: list[list[Any]] = self._graph.query(query, params).result_set
        return rows

    def _paged(self, query: str, params: dict[str, Any], limit: int) -> list[list[Any]]:
        """Up to ``limit`` rows of an ordered query, fetched a page at a time."""
        rows: list[list[Any]] = []
        while len(rows) < limit:
            size = min(_PAGE, limit - len(rows))
            page = self._query(
                f"{query} SKIP $skip LIMIT $size", {**params, "skip": len(rows), "size": size}
            )
            rows.extend(page)
            if len(page) < size:
                break
        return rows

    def _existing(self, ids: set[str]) -> set[str]:
        found: set[str] = set()
        for page in _pages(sorted(ids)):
            rows = self._query("MATCH (n:Node) WHERE n._id IN $ids RETURN n._id", {"ids": page})
            found.update(row[0] for row in rows)
        return found

    def _delete_messages(self, conversation_id: str, keep: set[str] | None = None) -> None:
        """Delete a conversation's messages, except ``keep``, along with their chunks."""
        rows = self._query(
            f"MATCH (:Node {{_id: $id}})-[:{EdgeType.HAS_MESSAGE}]->(m:Node) RETURN m._id",
            {"id": conversation_id},
        )
        doomed = [row[0] for row in rows if not keep or row[0] not in keep]
        chunks: list[str] = []
        for page in _pages(doomed):
            chunk_rows = self._query(
                f"MATCH (m:Node)-[:{EdgeType.HAS_CHUNK}]->(c:Node) WHERE m._id IN $ids "
                "RETURN c._id",
                {"ids": page},
            )
            chunks.extend(row[0] for row in chunk_rows)
        self.delete_nodes([*chunks, *doomed])

    def _create_indexes(self) -> None:
        statements = [
            "CREATE INDEX FOR (n:Node) ON (n._id)",
            "CREATE INDEX FOR (n:Node) ON (n._label)",
            "CREATE INDEX FOR (n:Node) ON (n.p_conversation_id)",
            "CREATE INDEX FOR (m:Meta) ON (m.key)",
            # No stop words and no stemming, so a word matches that word only,
            # as in the SQLite store. The title counts twice.
            "CALL db.idx.fulltext.createNodeIndex({label: 'Node', stopwords: []}, "
            "{field: '_ft_title', nostem: true, weight: 2}, "
            "{field: '_ft_text', nostem: true})",
        ]
        for statement in statements:
            try:
                self._query(statement)
            except ResponseError as error:
                if "already indexed" not in str(error):
                    raise


def fold_words(text: str) -> list[str]:
    """The words of ``text``, lowercased and without accents, as they are indexed.

    Letters and digits make words and everything else separates them, so
    ``customer_id`` is two words and ``café's`` is ``cafe`` and ``s``.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _TOKEN.findall(plain.casefold())


def snippet(text: str, words: set[str]) -> str:
    """About sixteen words of ``text`` around the most matches, matching words in [ ]."""
    spans = [(match.start(), match.end()) for match in _TOKEN.finditer(text)]
    if not spans:
        return text
    matched = [bool(set(fold_words(text[start:end])) & words) for start, end in spans]
    window = min(_SNIPPET_TOKENS, len(spans))
    best = max(
        range(len(spans) - window + 1),
        key=lambda first: (sum(matched[first : first + window]), -first),
    )
    last = best + window - 1
    begin = 0 if best == 0 else spans[best][0]
    end = len(text) if last == len(spans) - 1 else spans[last][1]
    pieces: list[str] = []
    cursor = begin
    for (start, stop), hit in zip(spans[best : last + 1], matched[best : last + 1], strict=True):
        pieces.append(text[cursor:start])
        pieces.append(f"[{text[start:stop]}]" if hit else text[start:stop])
        cursor = stop
    pieces.append(text[cursor:end])
    body = "".join(pieces).strip()
    return ("..." if best > 0 else "") + body + ("..." if end < len(text) else "")


def _node_row(node: Node) -> dict[str, Any]:
    row: dict[str, Any] = {"_id": node.id, "_label": node.label, "_props": _dumps(node.props)}
    for key, value in node.props.items():
        if key in _UNSEARCHABLE or not _NAME.fullmatch(key):
            continue
        if isinstance(value, str | int | float | bool):
            row[f"p_{key}"] = value
    text = node.props.get("text")
    if isinstance(text, str) and text.strip():
        row["_ft_text"] = " ".join(fold_words(text))
        row["_ft_title"] = " ".join(fold_words(str(node.props.get("title") or "")))
    return row


def _node(row: Sequence[Any]) -> Node:
    return Node(row[0], row[1], _loads(row[2]))


_NEIGHBOR = "type(r), startNode(r)._id, endNode(r)._id, r._props, b._id, b._label, b._props"
"""What a neighbour query returns: the edge, then the node at its other end."""


def _neighbor(row: Sequence[Any]) -> tuple[Edge, Node]:
    return Edge(row[1], row[0], row[2], _loads(row[3])), _node(row[4:7])


def _pattern(edge_types: Sequence[str] | None, direction: Direction) -> str:
    """The pattern from node ``a`` over ``r`` to its neighbour ``b``."""
    types = ":" + "|".join(_name(edge_type) for edge_type in edge_types) if edge_types else ""
    return {
        "out": f"(a)-[r{types}]->(b:Node)",
        "in": f"(a)<-[r{types}]-(b:Node)",
        "both": f"(a)-[r{types}]-(b:Node)",
    }[direction]


def _name(value: str) -> str:
    """An edge type, which FalkorDB cannot take as a parameter, checked before use."""
    if not _NAME.fullmatch(value):
        raise ValueError(f"invalid edge type: {value!r}")
    return value


def _pages[T](items: list[T]) -> Iterator[list[T]]:
    for start in range(0, len(items), _PAGE):
        yield items[start : start + _PAGE]


def _dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: str | None) -> dict[str, Any]:
    data = json.loads(value) if value else {}
    return data if isinstance(data, dict) else {}
