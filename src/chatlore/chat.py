"""Chat: answer a question from the library, citing the passages it came from.

Retrieval gathers the passages worth reading. Two ranked lists of chunks are
merged with reciprocal rank fusion: the chunks nearest to the question's
meaning, when embeddings exist, and the chunks that mention an entity the
question names. Names are matched directly rather than through full-text search,
which needs every word of a question to match. The model also gets the summaries
of those entities and the reports of their topics.

The model then answers from those numbered sources only, citing them like [2],
and says so when they do not hold the answer.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from chatlore.extraction import entity_key
from chatlore.llm import ChatMessage, StreamingLLM
from chatlore.store import EdgeType, GraphStore, Label, Node

RRF_K = 60
MAX_SOURCES = 8
MAX_ENTITIES = 6
MAX_TOPICS = 2
CHUNKS_PER_ENTITY = 3
NEAR_CANDIDATES = 25
"""Chunks ranked by meaning, per source asked for, that named entities choose from."""
MAX_ANSWER_TOKENS = 1500
MAX_NAME_WORDS = 4


@dataclass(frozen=True, slots=True)
class ChatLimits:
    """How many questions a public server sends to the language model."""

    per_visitor_hour: int = 10
    """Questions one visitor, told apart by address, may ask in any hour."""
    per_day: int = 300
    """Questions all visitors together may ask in a day, counted in UTC."""


_WORD = re.compile(r"[\w][\w.+#'-]*")
_CITATION = re.compile(r"\[(\d+)\]")
_NOT_NAMES = frozenset(
    {
        "a",
        "about",
        "an",
        "and",
        "any",
        "are",
        "be",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "have",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "should",
        "that",
        "the",
        "this",
        "to",
        "use",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
    }
)
"""Words that never count as an entity's name, however the model named one."""

SYSTEM_PROMPT = """\
You answer questions about one person's past conversations with AI assistants.
Use only the numbered sources and notes below; they are excerpts of those
conversations, and "user" in them is the person asking you now.

- Cite every claim with its source numbers in square brackets, like [2] or [1][3].
  The notes are background from earlier reading and are never cited.
- If the sources do not hold the answer, say so in one sentence instead of guessing.
- When sources disagree, prefer the most recent and say what changed.
- Answer in the language of the question, briefly and directly."""


@dataclass(frozen=True, slots=True)
class Source:
    """A passage the answer may cite, numbered from 1."""

    number: int
    chunk_id: str
    message_id: str
    conversation_id: str
    title: str | None
    source: str | None
    role: str | None
    created_at: str | None
    text: str

    @property
    def date(self) -> str | None:
        return self.created_at[:10] if self.created_at else None


@dataclass(frozen=True, slots=True)
class Note:
    """Background from the knowledge graph: an entity or a topic."""

    name: str
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class Context:
    """Everything the model is given to answer one question."""

    question: str
    sources: tuple[Source, ...]
    notes: tuple[Note, ...]

    @property
    def empty(self) -> bool:
        return not self.sources


def mentioned_entities(store: GraphStore, question: str) -> list[Node]:
    """Entities the question names, longest names first.

    Every run of up to four words is compared with the entities' names the way
    entities are matched to each other, so "claude code", "Claude-Code", and
    "claude codes" all find the entity Claude Code.
    """
    by_key = {entity_key(str(node.props["name"])): node for node in store.find_nodes(Label.ENTITY)}
    words = _WORD.findall(question)
    found: dict[str, Node] = {}
    for size in range(min(MAX_NAME_WORDS, len(words)), 0, -1):
        for start in range(len(words) - size + 1):
            key = entity_key(" ".join(words[start : start + size]))
            if len(key) < 2 or key in _NOT_NAMES:
                continue
            node = by_key.get(key)
            if node is not None and node.id not in found:
                found[node.id] = node
    return list(found.values())


def _chunks_of(
    store: GraphStore, entities: Sequence[Node], nearness: dict[str, int] | None
) -> list[str]:
    """Chunks mentioning the entities, taking turns so each entity is represented.

    With ``nearness``, the rank of chunks by meaning, an entity offers its chunks
    closest to the question: "Mac" is mentioned in dozens of places, and only
    the ones about the question help. Without it, its most recent chunks.
    """
    lists: list[list[str]] = []
    for entity in entities:
        chunks = [
            chunk
            for _, chunk in store.neighbors(entity.id, [EdgeType.MENTIONS], "in", limit=1_000_000)
        ]
        if nearness is not None:
            ordered_chunks = sorted(
                (chunk.id for chunk in chunks if chunk.id in nearness), key=nearness.__getitem__
            )
        else:
            ordered_chunks = [
                chunk.id
                for chunk in sorted(
                    chunks, key=lambda chunk: str(chunk.props.get("created_at") or ""), reverse=True
                )
            ]
        lists.append(ordered_chunks[:CHUNKS_PER_ENTITY])
    ordered: list[str] = []
    for turn in range(CHUNKS_PER_ENTITY):
        for offered in lists:
            if turn < len(offered) and offered[turn] not in ordered:
                ordered.append(offered[turn])
    return ordered


def _notes(store: GraphStore, entities: Sequence[Node]) -> list[Note]:
    notes = [
        Note(str(node.props["name"]), str(node.props.get("type", "")), str(summary))
        for node in entities[:MAX_ENTITIES]
        if (summary := node.props.get("summary"))
    ]
    counts: dict[str, int] = {}
    topics: dict[str, Node] = {}
    for node in entities:
        for _, topic in store.neighbors(node.id, [EdgeType.IN_TOPIC]):
            counts[topic.id] = counts.get(topic.id, 0) + 1
            topics[topic.id] = topic
    for topic_id in sorted(counts, key=lambda key: -counts[key])[:MAX_TOPICS]:
        topic = topics[topic_id]
        notes.append(Note(str(topic.props["title"]), "topic", str(topic.props["summary"])))
    return notes


def retrieve(
    store: GraphStore,
    question: str,
    embedding: Sequence[float] | None = None,
    limit: int = MAX_SOURCES,
) -> Context:
    """Gather the passages and notes the model needs to answer ``question``."""
    entities = mentioned_entities(store, question)
    nearness: dict[str, int] | None = None
    ranked: list[list[str]] = []
    if embedding is not None:
        hits = store.search_vector(embedding, limit=limit * NEAR_CANDIDATES, labels=[Label.CHUNK])
        nearness = {hit.node_id: rank for rank, hit in enumerate(hits)}
        ranked.append([hit.node_id for hit in hits[: limit * 3]])
    ranked.append(_chunks_of(store, entities, nearness))

    scores: dict[str, float] = {}
    for chunk_ids in ranked:
        for rank, chunk_id in enumerate(chunk_ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
    best = sorted(scores, key=lambda chunk_id: -scores[chunk_id])[:limit]

    sources: list[Source] = []
    for chunk_id in best:
        node = store.get_node(chunk_id)
        if node is None:
            continue
        props = node.props
        sources.append(
            Source(
                number=len(sources) + 1,
                chunk_id=node.id,
                message_id=str(props.get("message_id") or ""),
                conversation_id=str(props.get("conversation_id") or ""),
                title=str(props["title"]) if props.get("title") else None,
                source=str(props["source"]) if props.get("source") else None,
                role=str(props["role"]) if props.get("role") else None,
                created_at=str(props["created_at"]) if props.get("created_at") else None,
                text=str(props.get("text", "")),
            )
        )
    return Context(question, tuple(sources), tuple(_notes(store, entities)))


def answer_messages(context: Context) -> list[ChatMessage]:
    """The request that asks the model to answer from ``context``."""
    lines = ["### Sources"]
    for source in context.sources:
        about = ", ".join(part for part in (source.date, source.role) if part)
        heading = f"[{source.number}] {source.title or '(untitled)'}"
        lines += ["", f"{heading} ({about})" if about else heading, source.text]
    if context.notes:
        lines += ["", "### Notes from the knowledge graph"]
        lines += [
            f"- Topic {note.name}: {note.text}"
            if note.kind == "topic"
            else f"- {note.name} ({note.kind}): {note.text}"
            for note in context.notes
        ]
    lines += ["", "### Question", context.question]
    return [ChatMessage("system", SYSTEM_PROMPT), ChatMessage("user", "\n".join(lines))]


def answer(llm: StreamingLLM, context: Context) -> Iterator[str]:
    """Stream the model's answer to the question in ``context``."""
    return llm.stream(answer_messages(context), max_tokens=MAX_ANSWER_TOKENS)


def cited(text: str, context: Context) -> list[Source]:
    """The sources an answer cites, in the order of their numbers."""
    numbers = {int(number) for number in _CITATION.findall(text)}
    return [source for source in context.sources if source.number in numbers]
