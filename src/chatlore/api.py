"""REST API over the library, with streaming chat.

`chatlore serve` runs it. Everything the command line shows is here too:
search, conversations, entities, topics, and chat. Chat streams server-sent
events: the sources first, then the answer as it is written, then which sources
it cited. The web interface and the MCP server are built on these endpoints.

Each request opens the store and closes it again, so requests never share a
database connection across threads. The server listens on 127.0.0.1 unless told
otherwise, because the library is private.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

from chatlore import __version__
from chatlore.chat import MAX_SOURCES, Context, Source, answer, cited, retrieve
from chatlore.embeddings import Embedder, EmbeddingError, make_embedder, normalise
from chatlore.llm import LLMError, make_llm
from chatlore.paths import default_home
from chatlore.search import hybrid_search
from chatlore.store import EdgeType, GraphStore, Label, Node, open_store


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    sources: int = Field(default=MAX_SOURCES, ge=1, le=20)


def _source(source: Source) -> dict[str, Any]:
    return {
        "number": source.number,
        "title": source.title,
        "source": source.source,
        "date": source.date,
        "role": source.role,
        "conversation_id": source.conversation_id,
        "message_id": source.message_id,
        "text": source.text,
    }


def _entity(node: Node) -> dict[str, Any]:
    props = node.props
    return {
        "id": node.id,
        "name": props.get("name"),
        "type": props.get("type"),
        "summary": props.get("summary"),
        "mentions": props.get("mentions"),
    }


def _topic(node: Node, entities: int | None = None) -> dict[str, Any]:
    props = node.props
    return {
        "id": node.id,
        "title": props.get("title"),
        "summary": props.get("summary"),
        "findings": props.get("findings", []),
        "size": props.get("size"),
        "entities": props.get("entities", [])[: entities or None],
    }


def create_app(home: Path | None = None) -> FastAPI:
    """The API over the library in ``home``, or the default ChatLore home."""
    library = home or default_home()
    app = FastAPI(
        title="ChatLore",
        version=__version__,
        description="All your AI conversations, one graph, one chat.",
    )
    embedders: list[Embedder] = []

    def store() -> GraphStore:
        return open_store(library)

    def embed(text: str, graph: GraphStore) -> list[float] | None:
        """The query's embedding, or None when the library has no embeddings."""
        if graph.count_embeddings() == 0:
            return None
        if not embedders:
            embedders.append(make_embedder(library))
        try:
            return normalise(embedders[0].embed_query(text))
        except EmbeddingError as error:
            raise HTTPException(503, str(error)) from error

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/stats")
    def stats() -> dict[str, int]:
        with store() as graph:
            return {
                "conversations": graph.count_nodes(Label.CONVERSATION),
                "messages": graph.count_nodes(Label.MESSAGE),
                "chunks": graph.count_nodes(Label.CHUNK),
                "embeddings": graph.count_embeddings(),
                "entities": graph.count_nodes(Label.ENTITY),
                "topics": graph.count_nodes(Label.TOPIC),
            }

    @app.get("/search")
    def search(
        q: Annotated[str, Query(min_length=1)],
        limit: Annotated[int, Query(ge=1, le=100)] = 10,
        source: Annotated[list[str] | None, Query()] = None,
        mode: Annotated[str, Query(pattern="^(words|hybrid)$")] = "hybrid",
    ) -> list[dict[str, Any]]:
        """Messages matching ``q``: by words only, or by words, meaning, and entities."""
        with store() as graph:
            vector = embed(q, graph) if mode == "hybrid" else None
            if vector is None:
                return [
                    {
                        "message_id": hit.node_id,
                        "conversation_id": hit.conversation_id,
                        "title": hit.title,
                        "source": hit.source,
                        "snippet": hit.snippet,
                        "matched": ["words"],
                    }
                    for hit in graph.search_text(
                        q, limit=limit, sources=source, labels=[Label.MESSAGE]
                    )
                ]
            return [
                {
                    "message_id": hit.message_id,
                    "conversation_id": hit.conversation_id,
                    "title": hit.title,
                    "source": hit.source,
                    "snippet": hit.snippet,
                    "matched": [
                        name
                        for name, found in (
                            ("words", hit.by_words),
                            ("meaning", hit.by_meaning),
                            ("entities", hit.by_entity),
                        )
                        if found
                    ],
                }
                for hit in hybrid_search(graph, q, vector, limit=limit, sources=source)
            ]

    @app.get("/conversations")
    def conversations(
        source: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> list[dict[str, Any]]:
        with store() as graph:
            return [
                {
                    "id": item.id,
                    "source": item.source,
                    "title": item.title,
                    "created_at": item.created_at,
                    "updated_at": item.updated_at,
                    "messages": item.message_count,
                }
                for item in graph.list_conversations(source=source, limit=limit, offset=offset)
            ]

    @app.get("/conversations/{conversation_id}")
    def conversation(conversation_id: str) -> dict[str, Any]:
        with store() as graph:
            found = graph.get_conversation(conversation_id)
        if found is None:
            raise HTTPException(404, "no such conversation")
        return {
            "id": found.id,
            "source": found.source.value,
            "title": found.title,
            "created_at": found.created_at.isoformat() if found.created_at else None,
            "messages": [
                {
                    "id": message.id,
                    "parent_id": message.parent_id,
                    "role": message.role.value,
                    "created_at": message.created_at.isoformat() if message.created_at else None,
                    "text": message.text,
                }
                for message in found.messages
            ],
        }

    @app.get("/entities")
    def entities(
        q: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 50
    ) -> list[dict[str, Any]]:
        """Entities matching ``q`` by name or summary, or the most mentioned ones."""
        with store() as graph:
            if q:
                hits = graph.search_text(q, limit=limit, labels=[Label.ENTITY])
                nodes = [node for hit in hits if (node := graph.get_node(hit.node_id))]
            else:
                nodes = sorted(
                    graph.find_nodes(Label.ENTITY), key=lambda node: -int(node.props["mentions"])
                )[:limit]
            return [_entity(node) for node in nodes]

    @app.get("/entities/{entity_id}")
    def entity(entity_id: str) -> dict[str, Any]:
        with store() as graph:
            node = graph.get_node(entity_id)
            if node is None or node.label != Label.ENTITY:
                raise HTTPException(404, "no such entity")
            related = sorted(
                graph.neighbors(entity_id, [EdgeType.RELATED_TO], "both", limit=1_000_000),
                key=lambda pair: -int(pair[0].props.get("weight", 0)),
            )
            chunks = graph.neighbors(entity_id, [EdgeType.MENTIONS], "in", limit=1_000_000)
            titles = {
                str(chunk.props.get("conversation_id")): chunk.props.get("title")
                for _, chunk in chunks
            }
            topics = graph.neighbors(entity_id, [EdgeType.IN_TOPIC])
            return {
                **_entity(node),
                "descriptions": node.props.get("descriptions", []),
                "also_called": [
                    str(other.props["name"])
                    for _, other in graph.neighbors(entity_id, [EdgeType.SAME_AS], "both")
                ],
                "topic": _topic(topics[0][1], entities=10) if topics else None,
                "related": [
                    {
                        **_entity(other),
                        "relationship": (edge.props.get("descriptions") or [None])[0],
                        "weight": edge.props.get("weight"),
                    }
                    for edge, other in related[:20]
                ],
                "conversations": [
                    {"id": conversation_id, "title": title}
                    for conversation_id, title in titles.items()
                ],
            }

    @app.get("/topics")
    def topics(
        q: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 50
    ) -> list[dict[str, Any]]:
        """Topics whose report matches ``q``, or all topics, largest first."""
        with store() as graph:
            if q:
                hits = graph.search_text(q, limit=limit, labels=[Label.TOPIC])
                nodes = [node for hit in hits if (node := graph.get_node(hit.node_id))]
            else:
                nodes = sorted(
                    graph.find_nodes(Label.TOPIC), key=lambda node: -int(node.props["size"])
                )[:limit]
            return [_topic(node, entities=10) for node in nodes]

    @app.get("/topics/{topic_id}")
    def topic(topic_id: str) -> dict[str, Any]:
        with store() as graph:
            node = graph.get_node(topic_id)
            if node is None or node.label != Label.TOPIC:
                raise HTTPException(404, "no such topic")
            members = graph.neighbors(topic_id, [EdgeType.IN_TOPIC], "in", limit=1_000_000)
            return {
                **_topic(node),
                "members": [
                    _entity(member)
                    for _, member in sorted(
                        members, key=lambda pair: -int(pair[1].props["mentions"])
                    )
                ],
            }

    @app.post("/chat", response_class=EventSourceResponse)
    def chat(request: ChatRequest) -> Iterator[ServerSentEvent]:
        """Answer a question, streaming `sources`, then `token`s, then `done` or `error`.

        `sources` lists the numbered passages the answer may cite. Each `token`
        carries the next piece of text. `done` lists the numbers the answer
        cited; `error` says why it stopped.
        """
        # All database work happens before the first event, in one thread.
        try:
            with store() as graph:
                context: Context = retrieve(
                    graph, request.question, embed(request.question, graph), limit=request.sources
                )
        except HTTPException as error:
            yield ServerSentEvent(event="error", data={"message": str(error.detail)})
            return
        yield ServerSentEvent(event="sources", data=[_source(s) for s in context.sources])
        if context.empty:
            yield ServerSentEvent(event="done", data={"cited": [], "found": False})
            return
        try:
            llm = make_llm()
        except LLMError as error:
            yield ServerSentEvent(event="error", data={"message": str(error)})
            return
        written: list[str] = []
        try:
            for piece in answer(llm, context):
                written.append(piece)
                yield ServerSentEvent(event="token", data={"text": piece})
        except LLMError as error:
            yield ServerSentEvent(event="error", data={"message": str(error)})
            return
        finally:
            llm.close()
        numbers = [source.number for source in cited("".join(written), context)]
        yield ServerSentEvent(event="done", data={"cited": numbers, "found": True})

    return app
