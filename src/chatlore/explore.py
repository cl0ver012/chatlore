"""Exploring the library as a graph of what was discussed and where.

The knowledge graph links entities to each other; ``explore`` adds the
conversations they came up in, so one picture shows both what was talked about
and the chats to read. A view is built around one entity, one conversation, or
one topic, or gives an overview, and can be narrowed to some sources and to a
stretch of time. The web interface's Explore view draws it.

An entity is linked to a conversation when the conversation's chunks mention
it, weighted by how many do. Conversations are kept or left out by the date
they started and by their source; entities follow the conversations that remain.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any

from chatlore.store import EdgeType, GraphStore, Label, Node

ENTITIES = 40
"""Entities in a view, the one in focus included."""
CONVERSATIONS = 16
"""Conversations in a view, the one in focus included."""
CANDIDATES = 150
"""Most mentioned entities considered for an overview."""


class NotFoundError(LookupError):
    """The entity, conversation, or topic to explore around does not exist."""


@dataclass(frozen=True, slots=True)
class Window:
    """Which conversations count: from some sources, started within some dates.

    ``since`` and ``until`` are ISO dates or months, compared with the start of
    each conversation, both ends included.
    """

    sources: frozenset[str] = frozenset()
    since: str | None = None
    until: str | None = None

    def admits(self, conversation: Node) -> bool:
        if self.sources and conversation.props.get("source") not in self.sources:
            return False
        started = _started(conversation)
        if self.since and (started is None or started[: len(self.since)] < self.since):
            return False
        return not (self.until and (started is None or started[: len(self.until)] > self.until))

    @property
    def narrowed(self) -> bool:
        return bool(self.sources or self.since or self.until)


def _started(conversation: Node) -> str | None:
    value = conversation.props.get("created_at") or conversation.props.get("updated_at")
    return str(value) if value else None


def _mentions(graph: GraphStore, entity_ids: Iterable[str]) -> dict[str, Counter[str]]:
    """For each entity, how many chunks of each conversation mention it."""
    found: dict[str, Counter[str]] = {}
    for entity_id, pairs in graph.neighbors_many(entity_ids, [EdgeType.MENTIONS], "in").items():
        found[entity_id] = Counter(
            str(chunk.props["conversation_id"])
            for _, chunk in pairs
            if chunk.props.get("conversation_id")
        )
    return found


def _mentioned_in(graph: GraphStore, conversation_id: str) -> Counter[str]:
    """How many chunks of a conversation mention each entity."""
    chunks = graph.find_nodes(Label.CHUNK, {"conversation_id": conversation_id})
    counts: Counter[str] = Counter()
    for edges in graph.edges_many((chunk.id for chunk in chunks), [EdgeType.MENTIONS]).values():
        counts.update(edge.dst for edge in edges)
    return counts


def explore(
    graph: GraphStore,
    *,
    entity: str | None = None,
    conversation: str | None = None,
    topic: str | None = None,
    window: Window | None = None,
    entities: int = ENTITIES,
    conversations: int = CONVERSATIONS,
) -> dict[str, Any]:
    """The entities and conversations to draw, the links among them, and their topics.

    Around an ``entity``: it, the entities most strongly related to it, and the
    conversations that mention it most. Around a ``conversation``: it, the
    entities it mentions most, and the conversations that share the most of
    them. For a ``topic``: its entities and the conversations that mention them.
    Otherwise the most mentioned entities, within the window when it is narrowed,
    and the conversations that mention them most.
    """
    window = window or Window()
    focus: str | None = None
    if entity:
        center = graph.get_node(entity)
        if center is None or center.label != Label.ENTITY:
            raise NotFoundError("no such entity")
        focus = center.id
        related = sorted(
            graph.neighbors(entity, [EdgeType.RELATED_TO], "both", limit=1_000_000),
            key=lambda pair: -int(pair[0].props.get("weight", 0)),
        )
        candidates = [center.id, *dict.fromkeys(other.id for _, other in related)]
    elif conversation:
        center = graph.get_node(conversation)
        if center is None or center.label != Label.CONVERSATION:
            raise NotFoundError("no such conversation")
        focus = center.id
        candidates = [entity_id for entity_id, _ in _mentioned_in(graph, center.id).most_common()]
    elif topic:
        found = graph.get_node(topic)
        if found is None or found.label != Label.TOPIC:
            raise NotFoundError("no such topic")
        members = graph.neighbors(topic, [EdgeType.IN_TOPIC], "in", limit=1_000_000)
        members.sort(key=lambda pair: -int(pair[1].props.get("mentions", 0)))
        candidates = [node.id for _, node in members]
    else:
        every = sorted(graph.find_nodes(Label.ENTITY), key=lambda n: -int(n.props["mentions"]))
        candidates = [node.id for node in every[:CANDIDATES]]

    mentions = _mentions(graph, candidates)
    conversation_nodes = graph.get_nodes(
        dict.fromkeys(cid for counts in mentions.values() for cid in counts)
        | ({focus: None} if conversation and focus else {})
    )
    admitted = {
        cid for cid, node in conversation_nodes.items() if window.admits(node) or cid == focus
    }

    # Entities: in the window, ranked by mentions there; the focus always stays.
    def weight(entity_id: str) -> int:
        return sum(n for cid, n in mentions.get(entity_id, Counter()).items() if cid in admitted)

    if entity or conversation or topic:
        ranked = [e for e in candidates if e == entity or weight(e) > 0 or not window.narrowed]
    else:
        ranked = sorted((e for e in candidates if weight(e) > 0), key=lambda e: -weight(e))
    chosen_entities = list(dict.fromkeys(ranked))[: max(1, entities)]

    # Conversations: the ones these entities come up in most, the focus first.
    score: Counter[str] = Counter()
    if conversation and focus:
        shared = set(chosen_entities)
        for entity_id in shared:
            for cid, n in mentions.get(entity_id, Counter()).items():
                if cid != focus and cid in admitted:
                    score[cid] += min(n, 3)
    else:
        for entity_id in [entity] if entity else chosen_entities:
            for cid, n in mentions.get(entity_id, Counter()).items():
                if cid in admitted:
                    score[cid] += n
    limit = max(0, conversations - (1 if conversation else 0))
    chosen_conversations = ([focus] if conversation and focus else []) + [
        cid
        for cid, _ in sorted(
            score.items(), key=lambda item: (-item[1], _started(conversation_nodes[item[0]]) or "")
        )[:limit]
    ]

    return _view(graph, focus, chosen_entities, chosen_conversations, mentions, conversation_nodes)


def _view(
    graph: GraphStore,
    focus: str | None,
    entity_ids: list[str],
    conversation_ids: list[str],
    mentions: dict[str, Counter[str]],
    conversation_nodes: dict[str, Node],
) -> dict[str, Any]:
    entity_nodes = graph.get_nodes(entity_ids)
    entity_ids = [e for e in entity_ids if e in entity_nodes]
    kept_entities = set(entity_ids)
    kept_conversations = set(conversation_ids)

    topic_of = {
        entity_id: edges[0].dst
        for entity_id, edges in graph.edges_many(entity_ids, [EdgeType.IN_TOPIC]).items()
        if edges
    }
    topic_nodes = graph.get_nodes(dict.fromkeys(topic_of.values()))

    nodes: list[dict[str, Any]] = []
    for entity_id in entity_ids:
        props = entity_nodes[entity_id].props
        topic_id = topic_of.get(entity_id)
        nodes.append(
            {
                "id": entity_id,
                "kind": "entity",
                "name": props.get("name"),
                "type": props.get("type"),
                "mentions": props.get("mentions"),
                "topic": topic_id if topic_id in topic_nodes else None,
            }
        )
    for cid in conversation_ids:
        props = conversation_nodes[cid].props
        nodes.append(
            {
                "id": cid,
                "kind": "conversation",
                "title": props.get("title"),
                "source": props.get("source"),
                "created_at": _started(conversation_nodes[cid]),
                "messages": props.get("message_count"),
            }
        )

    edges: list[dict[str, Any]] = []
    for entity_id, links in graph.edges_many(entity_ids, [EdgeType.RELATED_TO]).items():
        for link in links:
            if link.dst in kept_entities:
                edges.append(
                    {
                        "source": entity_id,
                        "target": link.dst,
                        "kind": "related",
                        "weight": link.props.get("weight", 1),
                    }
                )
    for entity_id in entity_ids:
        for cid, n in mentions.get(entity_id, Counter()).items():
            if cid in kept_conversations:
                edges.append({"source": entity_id, "target": cid, "kind": "mentions", "weight": n})

    return {
        "focus": focus,
        "nodes": nodes,
        "edges": edges,
        "topics": [
            {"id": topic_id, "title": topic_nodes[topic_id].props.get("title")}
            for topic_id in dict.fromkeys(topic_of.values())
            if topic_id in topic_nodes
        ],
    }


def timeline(graph: GraphStore, sources: Collection[str] = ()) -> list[dict[str, Any]]:
    """Conversations started in each month, oldest first, from some sources or all."""
    months: Counter[str] = Counter()
    for node in graph.find_nodes(Label.CONVERSATION):
        started = _started(node)
        if started and (not sources or node.props.get("source") in sources):
            months[started[:7]] += 1
    return [{"month": month, "conversations": months[month]} for month in sorted(months)]


def conversation_entities(graph: GraphStore, conversation_id: str) -> list[dict[str, Any]]:
    """The entities a conversation mentions, most mentioned first, with the messages."""
    chunks = graph.find_nodes(Label.CHUNK, {"conversation_id": conversation_id})
    messages: dict[str, dict[str, None]] = {}
    counts: Counter[str] = Counter()
    by_chunk = {chunk.id: str(chunk.props.get("message_id")) for chunk in chunks}
    for chunk_id, edges in graph.edges_many(by_chunk, [EdgeType.MENTIONS]).items():
        for edge in edges:
            counts[edge.dst] += 1
            messages.setdefault(edge.dst, {})[by_chunk[chunk_id]] = None
    found = graph.get_nodes(counts)
    return [
        {
            "id": entity_id,
            "name": found[entity_id].props.get("name"),
            "type": found[entity_id].props.get("type"),
            "mentions": count,
            "messages": list(messages[entity_id]),
        }
        for entity_id, count in counts.most_common()
        if entity_id in found
    ]
