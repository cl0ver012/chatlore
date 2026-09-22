"""Processing steps that turn imported conversations into a searchable graph.

Every step is idempotent. Running it again on unchanged data does nothing, and
running it after a re-import touches only what changed, so expensive results
such as embeddings are never recomputed without a reason.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from chatlore.chunking import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    chunk_conversation,
)
from chatlore.embeddings import Embedder, EmbeddingCache, normalise, text_hash
from chatlore.extraction import (
    EntityCard,
    EntityToSummarise,
    Extraction,
    ExtractionCache,
    ExtractionError,
    canonical_type,
    duplicate_candidates,
    duplicate_messages,
    entity_key,
    extraction_messages,
    pair_key,
    parse_duplicates,
    parse_extractions,
    parse_summaries,
    summary_messages,
)
from chatlore.ids import content_hash, entity_id, topic_id
from chatlore.llm import LLM, ChatMessage, Completion, LLMAnswerError
from chatlore.models import Conversation
from chatlore.store import Edge, EdgeType, GraphStore, Label, Node
from chatlore.store.mapping import chunks_to_graph
from chatlore.topics import (
    REPORT_PROMPT_VERSION,
    Link,
    Member,
    TopicReport,
    find_topics,
    parse_report,
    report_messages,
)


@dataclass(slots=True)
class ChunkReport:
    """What a chunking run did."""

    conversations: int = 0
    added: int = 0
    removed: int = 0
    unchanged: int = 0

    @property
    def total(self) -> int:
        return self.added + self.unchanged


def sync_chunks(
    store: GraphStore,
    conversations: Iterable[Conversation],
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> ChunkReport:
    """Make the chunks in ``store`` match the current text of ``conversations``.

    Chunk ids depend on their text, so comparing id sets is enough to find what
    is new and what is stale. Chunks whose text did not change are left alone,
    which keeps their embeddings.
    """
    report = ChunkReport()
    with store.transaction():
        for conversation in conversations:
            report.conversations += 1
            wanted = chunk_conversation(conversation, max_tokens, overlap_tokens)
            wanted_ids = {chunk.id for chunk in wanted}
            existing_ids = {
                node.id
                for node in store.find_nodes(Label.CHUNK, {"conversation_id": conversation.id})
            }

            stale = existing_ids - wanted_ids
            fresh = [chunk for chunk in wanted if chunk.id not in existing_ids]
            if stale:
                store.delete_nodes(stale)
            if fresh:
                nodes, edges = chunks_to_graph(conversation, fresh)
                store.upsert_nodes(nodes)
                store.upsert_edges(edges)

            report.removed += len(stale)
            report.added += len(fresh)
            report.unchanged += len(wanted) - len(fresh)
    return report


EMBEDDING_MODEL_KEY = "embedding_model"


class EmbeddingModelMismatchError(Exception):
    """The store holds vectors from another model; mixing them would corrupt search."""


@dataclass(slots=True)
class EmbedReport:
    """What an embedding run did."""

    embedded: int = 0
    from_cache: int = 0
    already_done: int = 0

    @property
    def total(self) -> int:
        return self.embedded + self.from_cache + self.already_done


def sync_embeddings(
    store: GraphStore,
    embedder: Embedder,
    cache: EmbeddingCache | None = None,
    batch_size: int = 32,
    on_progress: Callable[[int], None] | None = None,
) -> EmbedReport:
    """Give every chunk that lacks one an embedding. Safe to interrupt and resume.

    Each batch is committed on its own, so stopping halfway keeps the work done
    so far. Vectors are normalised to unit length before they are stored.
    """
    stored_model = store.get_meta(EMBEDDING_MODEL_KEY)
    if stored_model is not None and stored_model != embedder.name:
        raise EmbeddingModelMismatchError(
            f"the database was embedded with '{stored_model}' but '{embedder.name}' is configured"
        )

    report = EmbedReport(already_done=store.count_embeddings())
    while True:
        batch = store.nodes_without_embedding(Label.CHUNK, limit=batch_size)
        if not batch:
            return report

        hashes = {node.id: text_hash(str(node.props.get("text", ""))) for node in batch}
        known = cache.get_many(embedder.name, list(hashes.values())) if cache is not None else {}
        missing = [node for node in batch if hashes[node.id] not in known]
        if missing:
            vectors = embedder.embed_passages([str(node.props.get("text", "")) for node in missing])
            fresh = {
                hashes[node.id]: normalise(vector)
                for node, vector in zip(missing, vectors, strict=True)
            }
            if cache is not None:
                cache.put_many(embedder.name, fresh)
            known.update(fresh)

        with store.transaction():
            for node in batch:
                store.set_embedding(node.id, known[hashes[node.id]])
            store.set_meta(EMBEDDING_MODEL_KEY, embedder.name)

        report.embedded += len(missing)
        report.from_cache += len(batch) - len(missing)
        if on_progress is not None:
            on_progress(len(batch))


@dataclass(slots=True)
class ExtractReport:
    """What an extraction run did."""

    extracted: int = 0
    already_done: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def pending_extractions(store: GraphStore, cache: ExtractionCache, model: str) -> list[Node]:
    """Chunks whose text ``model`` has not read yet, one per distinct text, in id order."""
    pending: dict[str, Node] = {}
    chunks = sorted(store.find_nodes(Label.CHUNK), key=lambda node: node.id)
    hashes = {node.id: text_hash(str(node.props.get("text", ""))) for node in chunks}
    known = cache.get_many(model, sorted(set(hashes.values())))
    for node in chunks:
        if hashes[node.id] not in known:
            pending.setdefault(hashes[node.id], node)
    return list(pending.values())


def _answers[T](
    llm: LLM,
    batches: Sequence[list[T]],
    messages: Callable[[list[T]], list[ChatMessage]],
    workers: int,
) -> Iterator[tuple[list[T], Completion | None]]:
    """Send one JSON request per batch, several at once, yielding answers as they arrive.

    An answer the model gave but that cannot be used, such as an empty one, comes
    back as ``None`` so the caller can skip the batch and ask again next run. Any
    other ``LLMError``, such as an unreachable server, is raised to the caller,
    and requests that have not started yet are cancelled.
    """
    executor = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = {
            executor.submit(llm.complete, messages(batch), json_output=True): batch
            for batch in batches
        }
        for future in as_completed(futures):
            try:
                completion: Completion | None = future.result()
            except LLMAnswerError:
                completion = None
            yield futures[future], completion
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def _batches[T](items: Sequence[T], size: int) -> list[list[T]]:
    return [list(items[start : start + size]) for start in range(0, len(items), size)]


def sync_extractions(
    store: GraphStore,
    llm: LLM,
    cache: ExtractionCache,
    batch_size: int = 4,
    workers: int = 8,
    limit: int | None = None,
    on_progress: Callable[[int], None] | None = None,
) -> ExtractReport:
    """Ask ``llm`` for the entities and relationships of every chunk it has not read.

    Several chunks go into one request and several requests run at once. Each
    answer is cached as soon as it arrives, so an interrupted run keeps what it
    finished. An answer that cannot be read is skipped and asked again next run.
    If the model cannot be reached at all, the run stops with ``LLMError``.
    """
    pending = pending_extractions(store, cache, llm.name)
    waiting = {text_hash(str(node.props.get("text", ""))) for node in pending}
    report = ExtractReport(
        already_done=sum(
            text_hash(str(node.props.get("text", ""))) not in waiting
            for node in store.find_nodes(Label.CHUNK)
        )
    )
    if limit is not None:
        pending = pending[:limit]

    def messages(batch: list[Node]) -> list[ChatMessage]:
        return extraction_messages([str(node.props.get("text", "")) for node in batch])

    for batch, completion in _answers(llm, _batches(pending, batch_size), messages, workers):
        found: list[Extraction | None] = [None] * len(batch)
        if completion is not None:
            report.input_tokens += completion.input_tokens
            report.output_tokens += completion.output_tokens
            with suppress(ExtractionError):
                found = parse_extractions(completion.text, len(batch))
        fresh = {
            text_hash(str(node.props.get("text", ""))): extraction
            for node, extraction in zip(batch, found, strict=True)
            if extraction is not None
        }
        cache.put_many(llm.name, fresh)
        report.extracted += len(fresh)
        report.failed += len(batch) - len(fresh)
        if on_progress is not None:
            on_progress(len(batch))
    return report


@dataclass(slots=True)
class EntityReport:
    """What the assembled entity graph holds."""

    entities: int = 0
    relationships: int = 0
    mentions: int = 0


@dataclass(slots=True)
class _EntityDraft:
    names: Counter[str] = field(default_factory=Counter)
    types: Counter[str] = field(default_factory=Counter)
    descriptions: dict[str, None] = field(default_factory=dict)
    chunks: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _RelationshipDraft:
    descriptions: dict[str, None] = field(default_factory=dict)
    weight: int = 0
    count: int = 0


def _to_summarise(node: Node) -> EntityToSummarise:
    return EntityToSummarise(
        str(node.props.get("name", "")),
        str(node.props.get("type", "")),
        tuple(str(description) for description in node.props.get("descriptions", [])),
    )


def _searchable(props: dict[str, Any]) -> dict[str, str]:
    """The title and text full-text search reads for an entity: its name and summary."""
    descriptions = props.get("descriptions") or [""]
    summary = props.get("summary") or descriptions[0]
    return {"title": str(props["name"]), "text": f"{props['name']}: {summary}"}


def _with_summary(props: dict[str, Any], summary: str) -> dict[str, Any]:
    updated = {**props, "summary": summary}
    return {**updated, **_searchable(updated)}


def sync_entities(store: GraphStore, cache: ExtractionCache, model: str) -> EntityReport:
    """Rebuild entities and relationships from ``model``'s cached reading of the chunks.

    The entity graph is replaced as a whole, so it always matches the chunks that
    exist now: entities found only in text that was since removed disappear. Two
    mentions are one entity when their names match by ``entity_key``; its name
    and type are the ones used most often, with invented types mapped onto the
    suggested ones where they clearly mean the same, and every distinct
    description is kept. An entity described once uses that description as its
    summary; one described more often gets its cached summary, or ``None`` until
    ``sync_summaries`` writes one. Every chunk links to the entities it mentions.
    """
    chunks = sorted(store.find_nodes(Label.CHUNK), key=lambda node: node.id)
    hashes = {node.id: text_hash(str(node.props.get("text", ""))) for node in chunks}
    known = cache.get_many(model, sorted(set(hashes.values())))

    entities: dict[str, _EntityDraft] = {}
    relationships: dict[tuple[str, str], _RelationshipDraft] = {}
    for chunk in chunks:
        extraction = known.get(hashes[chunk.id])
        if extraction is None:
            continue
        for entity in extraction.entities:
            draft = entities.setdefault(entity_key(entity.name), _EntityDraft())
            draft.names[entity.name] += 1
            draft.types[canonical_type(entity.type)] += 1
            if entity.description:
                draft.descriptions[entity.description] = None
            draft.chunks.add(chunk.id)
        for relationship in extraction.relationships:
            ends = (entity_key(relationship.source), entity_key(relationship.target))
            if ends[0] == ends[1]:
                continue  # two names that now count as one entity
            link = relationships.setdefault(ends, _RelationshipDraft())
            if relationship.description:
                link.descriptions[relationship.description] = None
            link.weight += relationship.strength
            link.count += 1

    nodes = [
        Node(
            entity_id(key),
            Label.ENTITY,
            {
                "name": draft.names.most_common(1)[0][0],
                "type": draft.types.most_common(1)[0][0],
                "descriptions": list(draft.descriptions),
                "mentions": len(draft.chunks),
                "model": model,
            },
        )
        for key, draft in sorted(entities.items())
    ]
    pending = [_to_summarise(node) for node in nodes if len(node.props["descriptions"]) > 1]
    summaries = cache.get_summaries(model, [entity.key for entity in pending])
    for node in nodes:
        descriptions = node.props["descriptions"]
        if len(descriptions) == 1:
            node.props["summary"] = descriptions[0]
        elif descriptions:
            node.props["summary"] = summaries.get(_to_summarise(node).key)
        else:
            node.props["summary"] = ""
        node.props.update(_searchable(node.props))
    mentions = [
        Edge(chunk_id, EdgeType.MENTIONS, entity_id(key))
        for key, draft in sorted(entities.items())
        for chunk_id in sorted(draft.chunks)
    ]
    links = [
        Edge(
            entity_id(source),
            EdgeType.RELATED_TO,
            entity_id(target),
            {"descriptions": list(link.descriptions), "weight": link.weight, "count": link.count},
        )
        for (source, target), link in sorted(relationships.items())
    ]
    with store.transaction():
        # Deleting the old entities removes their MENTIONS and RELATED_TO edges too.
        store.delete_nodes(node.id for node in store.find_nodes(Label.ENTITY))
        store.upsert_nodes(nodes)
        store.upsert_edges(mentions)
        store.upsert_edges(links)
    return EntityReport(len(nodes), len(links), len(mentions))


@dataclass(slots=True)
class SummaryReport:
    """What a summary run did."""

    summarised: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def pending_summaries(store: GraphStore) -> list[Node]:
    """Entities described more than once that have no summary yet, in id order."""
    return sorted(
        (node for node in store.find_nodes(Label.ENTITY) if node.props.get("summary") is None),
        key=lambda node: node.id,
    )


def sync_summaries(
    store: GraphStore,
    llm: LLM,
    cache: ExtractionCache,
    batch_size: int = 10,
    workers: int = 8,
    on_progress: Callable[[int], None] | None = None,
) -> SummaryReport:
    """Ask ``llm`` to merge the descriptions of every entity that has no summary yet.

    Run after ``sync_entities``. Summaries are cached by the entity's name and set
    of descriptions, so they are written once and survive rebuilding the graph,
    and an entity gets a new summary only when a new description turns up. An
    answer that cannot be read is skipped and asked again next run.
    """
    pending = pending_summaries(store)
    report = SummaryReport()

    def messages(batch: list[Node]) -> list[ChatMessage]:
        return summary_messages([_to_summarise(node) for node in batch])

    for batch, completion in _answers(llm, _batches(pending, batch_size), messages, workers):
        found: list[str | None] = [None] * len(batch)
        if completion is not None:
            report.input_tokens += completion.input_tokens
            report.output_tokens += completion.output_tokens
            with suppress(ExtractionError):
                found = parse_summaries(completion.text, len(batch))
        written = [(node, text) for node, text in zip(batch, found, strict=True) if text]
        cache.put_summaries(llm.name, {_to_summarise(node).key: text for node, text in written})
        store.upsert_nodes(
            Node(node.id, node.label, _with_summary(node.props, text)) for node, text in written
        )
        report.summarised += len(written)
        report.failed += len(batch) - len(written)
        if on_progress is not None:
            on_progress(len(batch))
    return report


@dataclass(slots=True)
class DuplicateReport:
    """What a duplicate check did."""

    candidates: int = 0
    asked: int = 0
    same: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def _card(node: Node) -> EntityCard:
    descriptions = node.props.get("descriptions") or [""]
    summary = node.props.get("summary") or descriptions[0]
    return EntityCard(str(node.props["name"]), str(node.props["type"]), str(summary))


def _duplicate_state(
    store: GraphStore, cache: ExtractionCache, model: str
) -> tuple[dict[str, Node], list[tuple[str, str]], dict[tuple[str, str], str], dict[str, bool]]:
    by_key = {entity_key(str(node.props["name"])): node for node in store.find_nodes(Label.ENTITY)}
    pairs = duplicate_candidates(by_key)
    keys = {pair: pair_key(*pair) for pair in pairs}
    return by_key, pairs, keys, cache.get_verdicts(model, sorted(set(keys.values())))


def pending_duplicates(store: GraphStore, cache: ExtractionCache, model: str) -> int:
    """How many possible duplicates ``model`` has not judged yet."""
    _, pairs, keys, verdicts = _duplicate_state(store, cache, model)
    return sum(keys[pair] not in verdicts for pair in pairs)


def sync_duplicates(
    store: GraphStore,
    llm: LLM,
    cache: ExtractionCache,
    batch_size: int = 20,
    workers: int = 8,
    on_progress: Callable[[int], None] | None = None,
) -> DuplicateReport:
    """Link entities that are the same thing under two names with ``SAME_AS``.

    Run after ``sync_entities`` and ``sync_summaries``. Possible duplicates come
    from ``duplicate_candidates``; the model judges each pair it has not judged
    before, and verdicts are cached, so a rebuilt graph gets its links back
    without model calls. Nothing is deleted: both entities stay, joined by the
    link, and topics treat them as one.
    """
    by_key, pairs, keys, verdicts = _duplicate_state(store, cache, llm.name)
    report = DuplicateReport(candidates=len(pairs))
    pending = [pair for pair in pairs if keys[pair] not in verdicts]

    def messages(batch: list[tuple[str, str]]) -> list[ChatMessage]:
        return duplicate_messages([(_card(by_key[a]), _card(by_key[b])) for a, b in batch])

    for batch, completion in _answers(llm, _batches(pending, batch_size), messages, workers):
        found: list[bool | None] = [None] * len(batch)
        if completion is not None:
            report.input_tokens += completion.input_tokens
            report.output_tokens += completion.output_tokens
            with suppress(ExtractionError):
                found = parse_duplicates(completion.text, len(batch))
        fresh = {
            keys[pair]: same for pair, same in zip(batch, found, strict=True) if same is not None
        }
        cache.put_verdicts(llm.name, fresh)
        verdicts.update(fresh)
        report.asked += len(fresh)
        report.failed += len(batch) - len(fresh)
        if on_progress is not None:
            on_progress(len(batch))

    same = [pair for pair in pairs if verdicts.get(keys[pair])]
    store.upsert_edges(Edge(by_key[a].id, EdgeType.SAME_AS, by_key[b].id) for a, b in same)
    report.same = len(same)
    return report


@dataclass(slots=True)
class TopicsReport:
    """What a topic run did."""

    topics: int = 0
    reported: int = 0
    failed: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(slots=True)
class _TopicState:
    entities: dict[str, Node]
    links: list[tuple[str, str, int, str]]
    groups: list[list[str]]
    keys: list[str]
    reports: dict[str, str]


def _topic_state(store: GraphStore, cache: ExtractionCache, model: str) -> _TopicState:
    entities = {node.id: node for node in store.find_nodes(Label.ENTITY)}
    links: list[tuple[str, str, int, str]] = []
    same: list[tuple[str, str]] = []
    for node_id in sorted(entities):
        for edge, other in store.neighbors(
            node_id, [EdgeType.RELATED_TO, EdgeType.SAME_AS], limit=1_000_000
        ):
            if edge.type == EdgeType.SAME_AS:
                same.append((node_id, other.id))
            else:
                descriptions = edge.props.get("descriptions") or [""]
                links.append((node_id, other.id, int(edge.props.get("weight", 1)), descriptions[0]))
    groups = find_topics(entities, [(s, t, w) for s, t, w, _ in links], same)
    keys = [
        content_hash(sorted(entity_key(str(entities[i].props["name"])) for i in group))
        for group in groups
    ]
    reports = cache.get_reports(model, REPORT_PROMPT_VERSION, keys)
    return _TopicState(entities, links, groups, keys, reports)


def pending_topics(store: GraphStore, cache: ExtractionCache, model: str) -> int:
    """How many topics have no report from ``model`` yet."""
    state = _topic_state(store, cache, model)
    return sum(key not in state.reports for key in state.keys)


def sync_topics(
    store: GraphStore,
    llm: LLM,
    cache: ExtractionCache,
    workers: int = 8,
    on_progress: Callable[[int], None] | None = None,
) -> TopicsReport:
    """Group entities into topics and give each a report.

    Run last, after ``sync_duplicates``. Topics are found again on every run and
    replace the old ones; a topic whose entities are the same as before keeps its
    cached report, and the model writes one for each new or changed topic. A topic
    whose report could not be read is left out and asked again next run. Each
    entity in a topic links to it with ``IN_TOPIC``.
    """
    state = _topic_state(store, cache, llm.name)
    report = TopicsReport()
    pending = [index for index, key in enumerate(state.keys) if key not in state.reports]

    def messages(batch: list[int]) -> list[ChatMessage]:
        group = set(state.groups[batch[0]])
        members = [
            Member(card.name, card.type, card.summary, int(state.entities[i].props["mentions"]))
            for i in sorted(group)
            for card in [_card(state.entities[i])]
        ]
        links = [
            Link(
                str(state.entities[source].props["name"]),
                str(state.entities[target].props["name"]),
                description,
                weight,
            )
            for source, target, weight, description in state.links
            if source in group and target in group
        ]
        return report_messages(members, links)

    for batch, completion in _answers(llm, [[index] for index in pending], messages, workers):
        key = state.keys[batch[0]]
        if completion is not None:
            report.input_tokens += completion.input_tokens
            report.output_tokens += completion.output_tokens
            with suppress(ExtractionError):
                written = parse_report(completion.text).to_json()
                cache.put_reports(llm.name, REPORT_PROMPT_VERSION, {key: written})
                state.reports[key] = written
                report.reported += 1
        report.failed += key not in state.reports
        if on_progress is not None:
            on_progress(1)

    nodes: list[Node] = []
    members: list[Edge] = []
    for group, key in zip(state.groups, state.keys, strict=True):
        if key not in state.reports:
            continue
        topic = TopicReport.from_json(state.reports[key])
        node_id = topic_id(key)
        names = sorted(
            (state.entities[i] for i in group), key=lambda node: -int(node.props["mentions"])
        )
        nodes.append(
            Node(
                node_id,
                Label.TOPIC,
                {
                    "title": topic.title,
                    "summary": topic.summary,
                    "findings": list(topic.findings),
                    "size": len(group),
                    "entities": [str(node.props["name"]) for node in names],
                    "text": topic.text,
                    "model": llm.name,
                },
            )
        )
        members += [Edge(entity, EdgeType.IN_TOPIC, node_id) for entity in group]
    with store.transaction():
        store.delete_nodes(node.id for node in store.find_nodes(Label.TOPIC))
        store.upsert_nodes(nodes)
        store.upsert_edges(members)
    report.topics = len(nodes)
    return report
