"""Tests for duplicates, topics, and entity search in the knowledge graph."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from chatlore.extraction import (
    EntityCard,
    ExtractionCache,
    ExtractionError,
    duplicate_candidates,
    duplicate_messages,
    entity_key,
    parse_duplicates,
)
from chatlore.llm import LLMError
from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind
from chatlore.pipeline import (
    pending_duplicates,
    pending_extractions,
    pending_topics,
    sync_chunks,
    sync_duplicates,
    sync_entities,
    sync_extractions,
    sync_summaries,
    sync_topics,
)
from chatlore.store import EdgeType, GraphStore, Label, SQLiteStore
from chatlore.topics import (
    MIN_TOPIC_SIZE,
    Link,
    Member,
    TopicReport,
    find_topics,
    parse_report,
    report_messages,
)
from tests.fakes import FakeLLM


def _conversation(conversation_id: str, *texts: str) -> Conversation:
    messages: list[Message] = []
    for position, text in enumerate(texts):
        messages.append(
            Message(
                id=f"{conversation_id}-{position}",
                parent_id=f"{conversation_id}-{position - 1}" if position else None,
                role=Role.USER if position % 2 == 0 else Role.ASSISTANT,
                content=[ContentPart(text=text)],
            )
        )
    return Conversation(
        id=conversation_id,
        source=SourceKind.CLAUDE,
        external_id=conversation_id,
        title=f"Title {conversation_id}",
        messages=messages,
    )


@pytest.fixture
def store() -> Iterator[GraphStore]:
    backend = SQLiteStore(":memory:")
    try:
        yield backend
    finally:
        backend.close()


@pytest.fixture
def cache() -> Iterator[ExtractionCache]:
    with ExtractionCache() as backend:
        yield backend


def _build(store: GraphStore, cache: ExtractionCache, llm: FakeLLM, *texts: str) -> None:
    """Run the whole pipeline on one conversation, the way `chatlore extract` does."""
    conversation = _conversation("one", *texts)
    store.upsert_conversation(conversation)
    sync_chunks(store, [conversation])
    sync_extractions(store, llm, cache)
    sync_entities(store, cache, llm.name)
    sync_summaries(store, llm, cache)
    sync_duplicates(store, llm, cache)
    sync_topics(store, llm, cache)


# -- empty answers -------------------------------------------------------------


def test_an_empty_answer_is_skipped_and_the_run_goes_on(
    store: GraphStore, cache: ExtractionCache
) -> None:
    conversation = _conversation("one", "Alice here.", "Bob here.", "Carol here.")
    store.upsert_conversation(conversation)
    sync_chunks(store, [conversation])

    report = sync_extractions(store, FakeLLM(empty_requests=[2]), cache, batch_size=1, workers=1)

    assert (report.extracted, report.failed) == (2, 1)
    assert len(pending_extractions(store, cache, "fake-llm")) == 1


def test_an_unreachable_model_still_stops_the_run(
    store: GraphStore, cache: ExtractionCache
) -> None:
    conversation = _conversation("one", "Alice here.", "Bob here.")
    store.upsert_conversation(conversation)
    sync_chunks(store, [conversation])

    with pytest.raises(LLMError, match="unreachable"):
        sync_extractions(store, FakeLLM(fail_on_request=1), cache, batch_size=1, workers=1)


# -- possible duplicates -------------------------------------------------------


def _keys(*names: str) -> list[str]:
    return [entity_key(name) for name in names]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Postgres", "PostgreSQL"),
        ("Claude Code", "Claude Code CLI"),
        ("Postgres", "Postgres database"),
        ("Know Your Customer", "KYC"),
        ("Kubernetes", "Kubernets"),
        ("Visual Studio Code", "Microsoft Visual Studio Code"),
    ],
)
def test_names_that_may_be_one_thing_are_candidates(first: str, second: str) -> None:
    keys = _keys(first, second)

    assert duplicate_candidates(keys) == [(min(keys), max(keys))]


@pytest.mark.parametrize(
    ("first", "second"),
    [("Llama", "Ollama"), ("Rust", "Python"), ("Code", "Claude Code"), ("C", "C++")],
)
def test_unrelated_names_are_not_candidates(first: str, second: str) -> None:
    assert duplicate_candidates(_keys(first, second)) == []


def test_the_duplicate_request_shows_both_sides_of_every_pair() -> None:
    system, user = duplicate_messages(
        [(EntityCard("Postgres", "tool", "A database."), EntityCard("PostgreSQL", "tool", "Db."))]
    )

    assert "same" in system.content
    assert user.content == "### Pair 1\n- Postgres (tool): A database.\n- PostgreSQL (tool): Db."


def test_verdicts_are_read_per_pair() -> None:
    answer = json.dumps(
        {"pairs": [{"pair": 2, "same": True}, {"pair": 1, "same": "yes"}, {"pair": 5}]}
    )

    assert parse_duplicates(answer, 2) == [None, True]
    with pytest.raises(ExtractionError):
        parse_duplicates("[]", 1)


def test_the_same_thing_under_two_names_is_linked_not_deleted(
    store: GraphStore, cache: ExtractionCache
) -> None:
    llm = FakeLLM()
    _build(store, cache, llm, "Postgres is slow.", "PostgreSQL needs an index.", "Rust is fast.")

    names = {str(node.props["name"]): node for node in store.find_nodes(Label.ENTITY)}
    assert {"Postgres", "PostgreSQL", "Rust"} <= set(names)
    [(_, other)] = store.neighbors(names["Postgres"].id, [EdgeType.SAME_AS], "both")
    assert other.id == names["PostgreSQL"].id
    assert llm.duplicate_requests == [[("Postgres", "PostgreSQL")]]


def test_verdicts_are_cached_and_restored_after_a_rebuild(
    store: GraphStore, cache: ExtractionCache
) -> None:
    llm = FakeLLM()
    _build(store, cache, llm, "Postgres is slow.", "PostgreSQL needs an index.")
    asked = len(llm.duplicate_requests)

    sync_entities(store, cache, llm.name)
    report = sync_duplicates(store, llm, cache)

    assert (report.candidates, report.asked, report.same) == (1, 0, 1)
    assert len(llm.duplicate_requests) == asked
    assert pending_duplicates(store, cache, llm.name) == 0


# -- topics --------------------------------------------------------------------


def _clique(prefix: str, size: int) -> list[tuple[str, str, int]]:
    names = [f"{prefix}{index}" for index in range(size)]
    return [(a, b, 5) for index, a in enumerate(names) for b in names[index + 1 :]]


def test_densely_linked_entities_form_topics_largest_first() -> None:
    links = [*_clique("a", 5), *_clique("b", 4), ("a0", "b0", 1)]
    entities = {name for link in links for name in link[:2]} | {"lonely"}

    topics = find_topics(entities, links)

    assert topics == [[f"a{i}" for i in range(5)], [f"b{i}" for i in range(4)]]
    assert find_topics(entities, links) == topics


def test_small_groups_are_not_topics_and_same_things_count_once() -> None:
    links = [("x", "y", 3), *_clique("a", MIN_TOPIC_SIZE)]
    entities = {"x", "y", "y2", *[f"a{i}" for i in range(MIN_TOPIC_SIZE)]}

    assert find_topics(entities, links) == [[f"a{i}" for i in range(MIN_TOPIC_SIZE)]]
    assert find_topics(entities, links, same=[("y", "y2")]) == [
        [f"a{i}" for i in range(MIN_TOPIC_SIZE)],
        ["x", "y", "y2"],
    ]


def test_no_relationships_means_no_topics() -> None:
    assert find_topics({"a", "b", "c"}, []) == []


def test_the_report_request_shows_the_best_connected_parts_first() -> None:
    _, user = report_messages(
        [Member("Rust", "tool", "A language.", 2), Member("Cargo", "tool", "Its builder.", 5)],
        [Link("Cargo", "Rust", "Builds it.", 9)],
    )

    assert user.content == (
        "### Entities\n- Cargo (tool): Its builder.\n- Rust (tool): A language.\n\n"
        "### Relationships\n- Cargo -> Rust: Builds it."
    )


def test_a_report_is_read_and_its_findings_kept_short() -> None:
    answer = json.dumps(
        {"title": " Rust tooling ", "summary": "About Rust.", "findings": ["a", "", 3, *"bcdef"]}
    )

    report = parse_report(answer)

    assert report == TopicReport("Rust tooling", "About Rust.", ("a", "b", "c", "d", "e"))
    assert TopicReport.from_json(report.to_json()) == report
    with pytest.raises(ExtractionError):
        parse_report(json.dumps({"title": "No summary"}))


def _topic_texts(*groups: str) -> list[str]:
    """One message per group, naming its three members, so each group is a topic."""
    return [f"{a} and {b} and {c}." for a, b, c in (group.split() for group in groups)]


def test_topics_get_reports_and_their_members_link_to_them(
    store: GraphStore, cache: ExtractionCache
) -> None:
    llm = FakeLLM()
    _build(store, cache, llm, *_topic_texts("Alice Bob Carol", "Rust Cargo Clippy"))

    topics = store.find_nodes(Label.TOPIC)
    assert sorted(str(topic.props["title"]).split()[0] for topic in topics) == ["About", "About"]
    assert {topic.props["size"] for topic in topics} == {3}
    alice = next(n for n in store.find_nodes(Label.ENTITY) if n.props["name"] == "Alice")
    [(_, topic)] = store.neighbors(alice.id, [EdgeType.IN_TOPIC])
    assert set(topic.props["entities"]) == {"Alice", "Bob", "Carol"}
    assert [hit.node_id for hit in store.search_text("Clippy", labels=[Label.TOPIC])]


def test_an_unchanged_topic_keeps_its_cached_report(
    store: GraphStore, cache: ExtractionCache
) -> None:
    llm = FakeLLM()
    _build(store, cache, llm, *_topic_texts("Alice Bob Carol"))
    [before] = store.find_nodes(Label.TOPIC)

    sync_entities(store, cache, llm.name)
    report = sync_topics(store, llm, cache)

    assert (report.topics, report.reported) == (1, 0)
    assert len(llm.report_requests) == 1
    assert store.find_nodes(Label.TOPIC) == [before]
    assert pending_topics(store, cache, llm.name) == 0


def test_an_unreadable_report_leaves_the_topic_out_until_the_next_run(
    store: GraphStore, cache: ExtractionCache
) -> None:
    conversation = _conversation("one", *_topic_texts("Alice Bob Carol"))
    store.upsert_conversation(conversation)
    sync_chunks(store, [conversation])
    sync_extractions(store, FakeLLM(), cache)
    sync_entities(store, cache, "fake-llm")

    first = sync_topics(store, FakeLLM(broken_requests=[1]), cache)
    second = sync_topics(store, FakeLLM(), cache)

    assert (first.topics, first.failed) == (0, 1)
    assert (second.topics, second.reported) == (1, 1)


# -- entities in search --------------------------------------------------------


def test_entities_are_found_by_name_and_summary(store: GraphStore, cache: ExtractionCache) -> None:
    _build(store, cache, FakeLLM(), "Postgres is slow.", "Postgres needs an index.")

    hits = store.search_text("postgres index", labels=[Label.ENTITY])

    [node] = [store.get_node(hit.node_id) for hit in hits]
    assert node is not None
    assert node.props["name"] == "Postgres"
    assert node.props["text"] == f"Postgres: {node.props['summary']}"
