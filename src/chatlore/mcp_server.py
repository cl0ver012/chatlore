"""MCP server: the library as tools for AI assistants.

`chatlore mcp` runs it over standard input and output, the way Claude Desktop,
Claude Code, Cursor, and other MCP clients start local servers. The assistant can
search your past conversations, gather the passages that answer a question, look
up entities and topics, and read a conversation. It answers with its own model,
so no model key is needed here; only the embedding model runs, locally.

Every tool is read-only and opens the store for that one call, since tools run
on worker threads and a SQLite connection must stay on the thread that opened it.
Results are plain text with the ids the other tools take.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from chatlore import __version__
from chatlore.chat import MAX_SOURCES, retrieve
from chatlore.embeddings import Embedder, EmbeddingError, make_embedder, normalise
from chatlore.extraction import entity_key
from chatlore.models import Message
from chatlore.paths import default_home
from chatlore.search import hybrid_search
from chatlore.store import EdgeType, GraphStore, Label, Node, open_store

MAX_CONVERSATION_CHARS = 30_000
"""How much of a conversation one call returns; long ones are read in parts."""

INSTRUCTIONS = """\
ChatLore holds the user's past conversations with AI assistants (ChatGPT, Claude,
Gemini, and notes), with a knowledge graph of the people, projects, tools, and
places in them. Use it when the user refers to something they discussed, decided,
or worked out before, or asks what they know about something.

Start with ask_context for a question, or search to find messages. Answer from what
the tools return, cite the conversation's title and date, and say so when the
conversations do not hold the answer. Use conversation to read more around a
passage, and entity and topics to see how things connect."""

_READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)


def create_server(home: Path | None = None) -> MCPServer:
    """The MCP server over the library in ``home``, or the default ChatLore home."""
    library = home or default_home()
    server = MCPServer(
        "ChatLore",
        title="ChatLore",
        description="Your past AI conversations, searchable and as a knowledge graph.",
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level="WARNING",
    )
    embedders: list[Embedder] = []
    loading = threading.Lock()

    def embed(text: str, graph: GraphStore) -> list[float] | None:
        """The text's embedding, or None when the library has no embeddings."""
        if graph.count_embeddings() == 0:
            return None
        with loading:
            if not embedders:
                embedders.append(make_embedder(library))
        try:
            return normalise(embedders[0].embed_query(text))
        except EmbeddingError as error:
            raise ToolError(str(error)) from error

    @server.tool(annotations=_READ_ONLY, structured_output=False)
    def search(
        query: Annotated[str, Field(min_length=1, description="Words or a description.")],
        limit: Annotated[int, Field(ge=1, le=50, description="How many messages.")] = 10,
        source: Annotated[
            str | None, Field(description="Only this source: chatgpt, claude, gemini, ...")
        ] = None,
    ) -> str:
        """Find messages in the user's past conversations by their words, their meaning,
        and the entities they mention. Each result has the ids to open its conversation."""
        sources = [source] if source else None
        with open_store(library) as graph:
            if graph.count_nodes(Label.MESSAGE) == 0:
                return _EMPTY
            vector = embed(query, graph)
            if vector is None:
                hits = [
                    (hit.title, hit.source, hit.conversation_id, hit.node_id, hit.snippet, "words")
                    for hit in graph.search_text(
                        query, limit=limit, sources=sources, labels=[Label.MESSAGE]
                    )
                ]
            else:
                hits = [
                    (
                        hit.title,
                        hit.source,
                        hit.conversation_id,
                        hit.message_id,
                        hit.snippet,
                        ", ".join(
                            name
                            for name, found in (
                                ("words", hit.by_words),
                                ("meaning", hit.by_meaning),
                                ("entities", hit.by_entity),
                            )
                            if found
                        ),
                    )
                    for hit in hybrid_search(graph, query, vector, limit=limit, sources=sources)
                ]
        if not hits:
            return f"No messages match {query!r}."
        lines = [f"{len(hits)} messages for {query!r}:"]
        for number, (title, kind, conversation_id, message_id, snippet, matched) in enumerate(
            hits, start=1
        ):
            lines += [
                "",
                f"{number}. {title or '(untitled)'} ({kind}; matched by {matched})",
                f"   conversation {conversation_id}, message {message_id}",
                f"   {' '.join(snippet.split())}",
            ]
        return "\n".join(lines)

    @server.tool(annotations=_READ_ONLY, structured_output=False)
    def ask_context(
        question: Annotated[str, Field(min_length=1, description="The user's question.")],
        sources: Annotated[
            int, Field(ge=1, le=20, description="How many passages to gather.")
        ] = MAX_SOURCES,
    ) -> str:
        """Gather the passages from the user's past conversations that best answer a
        question, numbered for citation, with background notes from the knowledge graph.
        Answer from them yourself."""
        with open_store(library) as graph:
            if graph.count_nodes(Label.CHUNK) == 0:
                return _EMPTY
            context = retrieve(graph, question, embed(question, graph), limit=sources)
        if context.empty:
            return "Nothing in the user's conversations matches that question."
        lines = [
            f"Passages from the user's past conversations for: {context.question}",
            "Answer from these passages only and cite them like [2]. When they disagree, "
            "prefer the most recent. If they do not hold the answer, say so.",
        ]
        for source in context.sources:
            about = ", ".join(part for part in (source.source, source.date, source.role) if part)
            lines += [
                "",
                f"[{source.number}] {source.title or '(untitled)'} ({about})",
                f"conversation {source.conversation_id}, message {source.message_id}",
                source.text,
            ]
        if context.notes:
            lines += ["", "Background from the knowledge graph (not to be cited):"]
            lines += [
                f"- Topic {note.name}: {note.text}"
                if note.kind == "topic"
                else f"- {note.name} ({note.kind}): {note.text}"
                for note in context.notes
            ]
        return "\n".join(lines)

    @server.tool(annotations=_READ_ONLY, structured_output=False)
    def entity(
        name: Annotated[str, Field(min_length=1, description="The entity's name or id.")],
    ) -> str:
        """What the knowledge graph knows about a person, project, tool, place, or idea:
        its summary, other names, topic, related entities, and the conversations it
        came up in."""
        with open_store(library) as graph:
            node = _find_entity(graph, name)
            if node is None:
                raise ToolError(f"No entity named {name!r}. Try search or topics.")
            props = node.props
            lines = [
                f"{props['name']} ({props.get('type')}; mentioned in {props.get('mentions')} "
                f"passages; id {node.id})",
                str(props.get("summary") or ""),
            ]
            same = graph.neighbors(node.id, [EdgeType.SAME_AS], "both")
            if same:
                lines.append("Also called: " + ", ".join(str(o.props["name"]) for _, o in same))
            topic = graph.neighbors(node.id, [EdgeType.IN_TOPIC])
            if topic:
                lines.append(f"Topic: {topic[0][1].props['title']} (id {topic[0][1].id})")
            related = sorted(
                graph.neighbors(node.id, [EdgeType.RELATED_TO], "both", limit=1_000_000),
                key=lambda pair: -int(pair[0].props.get("weight", 0)),
            )
            if related:
                lines += ["", "Related:"]
                for edge, other in related[:15]:
                    description = (edge.props.get("descriptions") or [""])[0]
                    lines.append(f"- {other.props['name']}: {description}")
            chunks = graph.neighbors(node.id, [EdgeType.MENTIONS], "in", limit=1_000_000)
            conversations = {str(chunk.props.get("conversation_id")): chunk for _, chunk in chunks}
            if conversations:
                lines += ["", "Mentioned in:"]
                for conversation_id, chunk in list(conversations.items())[:15]:
                    date = str(chunk.props.get("created_at") or "")[:10]
                    about = ", ".join(p for p in (str(chunk.props.get("source") or ""), date) if p)
                    title = chunk.props.get("title") or "(untitled)"
                    lines.append(f"- {title} ({about}; conversation {conversation_id})")
        return "\n".join(lines)

    @server.tool(annotations=_READ_ONLY, structured_output=False)
    def topics(
        words: Annotated[
            str | None, Field(description="Only topics whose report mentions these words.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100, description="How many topics.")] = 20,
    ) -> str:
        """The topics of the user's conversations: groups of closely related entities,
        largest first, each with a short summary. Use topic for the full report."""
        with open_store(library) as graph:
            if words:
                hits = graph.search_text(words, limit=limit, labels=[Label.TOPIC])
                nodes = [node for hit in hits if (node := graph.get_node(hit.node_id))]
            else:
                nodes = sorted(
                    graph.find_nodes(Label.TOPIC), key=lambda node: -int(node.props["size"])
                )[:limit]
        if not nodes:
            return "No topics mention those words." if words else _NO_TOPICS
        lines: list[str] = []
        for node in nodes:
            entities = ", ".join(str(name) for name in node.props.get("entities", [])[:8])
            lines += [
                f"{node.props['title']} ({node.props['size']} entities; id {node.id})",
                f"  {node.props['summary']}",
                f"  Entities: {entities}",
            ]
        return "\n".join(lines)

    @server.tool(annotations=_READ_ONLY, structured_output=False)
    def topic(
        topic: Annotated[str, Field(min_length=1, description="The topic's id or title.")],
    ) -> str:
        """A topic's full report: its summary, findings, and every entity in it."""
        with open_store(library) as graph:
            node = graph.get_node(topic)
            if node is None or node.label != Label.TOPIC:
                hits = graph.search_text(topic, limit=1, labels=[Label.TOPIC])
                node = graph.get_node(hits[0].node_id) if hits else None
            if node is None:
                raise ToolError(f"No topic {topic!r}. Use topics to list them.")
            members = sorted(
                graph.neighbors(node.id, [EdgeType.IN_TOPIC], "in", limit=1_000_000),
                key=lambda pair: -int(pair[1].props["mentions"]),
            )
        props = node.props
        lines = [f"{props['title']} ({props['size']} entities; id {node.id})", props["summary"]]
        if props.get("findings"):
            lines += ["", "Findings:", *(f"- {finding}" for finding in props["findings"])]
        lines += ["", "Entities:"]
        lines += [
            f"- {member.props['name']} ({member.props.get('type')}): "
            f"{member.props.get('summary') or ''}"
            for _, member in members
        ]
        return "\n".join(lines)

    @server.tool(annotations=_READ_ONLY, structured_output=False)
    def conversation(
        conversation_id: Annotated[str, Field(min_length=1, description="The conversation's id.")],
        message_id: Annotated[
            str | None, Field(description="Show the part around this message.")
        ] = None,
    ) -> str:
        """Read a conversation. Long ones are returned in parts: from the start, or
        around ``message_id``, such as a message found by search or ask_context."""
        with open_store(library) as graph:
            found = graph.get_conversation(conversation_id)
        if found is None:
            raise ToolError(f"No conversation {conversation_id!r}.")
        messages = found.linear_messages()
        if message_id and all(message.id != message_id for message in messages):
            messages = list(found.messages)
        ids = [message.id for message in messages]
        if message_id and message_id not in ids:
            raise ToolError(f"No message {message_id!r} in this conversation.")
        first, last = _window(messages, ids.index(message_id) if message_id else 0)

        date = found.created_at.date().isoformat() if found.created_at else None
        about = ", ".join(part for part in (found.source.value, date) if part)
        lines = [f"{found.title or '(untitled)'} ({about}; {len(messages)} messages)"]
        if first > 0 or last < len(messages):
            lines.append(
                f"Showing messages {first + 1} to {last} of {len(messages)}. Pass the id of "
                "a message outside them as message_id to read another part."
            )
        for message in messages[first:last]:
            when = message.created_at.date().isoformat() if message.created_at else ""
            head = ", ".join(part for part in (message.role.value, when) if part)
            lines += ["", f"[{head}] (message {message.id})", message.text]
        return "\n".join(lines)

    return server


_EMPTY = "The library is empty. Run `chatlore import` and `chatlore process` first."
_NO_TOPICS = "No topics yet. Run `chatlore extract` first."


def _find_entity(graph: GraphStore, name: str) -> Node | None:
    """The entity with this id or name, or failing that the best word match."""
    node = graph.get_node(name)
    if node is not None and node.label == Label.ENTITY:
        return node
    exact = entity_key(name)
    hits = graph.search_text(name, limit=20, labels=[Label.ENTITY])
    nodes = [node for hit in hits if (node := graph.get_node(hit.node_id)) is not None]
    for node in nodes:
        if entity_key(str(node.props["name"])) == exact:
            return node
    return nodes[0] if nodes else None


def _window(messages: list[Message], center: int) -> tuple[int, int]:
    """The messages around ``center`` that fit in the budget, as a slice.

    The message itself always fits; the rest are added one at a time on either
    side, alternating, while they fit.
    """
    first, last = center, center + 1
    used = len(messages[center].text)
    grew = True
    while grew:
        grew = False
        for index in (last, first - 1):
            if 0 <= index < len(messages) and index not in range(first, last):
                size = len(messages[index].text)
                if used + size > MAX_CONVERSATION_CHARS:
                    continue
                used += size
                first, last = min(first, index), max(last, index + 1)
                grew = True
    return first, last
