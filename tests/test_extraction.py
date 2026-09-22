"""Tests for entity extraction, its cache, and the entity graph built from it."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from chatlore.embeddings import text_hash
from chatlore.extraction import (
    SUGGESTED_TYPES,
    EntityToSummarise,
    ExtractedEntity,
    ExtractedRelationship,
    Extraction,
    ExtractionCache,
    ExtractionError,
    canonical_type,
    entity_key,
    extraction_messages,
    parse_extractions,
    parse_summaries,
    summary_messages,
)
from chatlore.llm import LLMError
from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind
from chatlore.pipeline import (
    pending_extractions,
    pending_summaries,
    sync_chunks,
    sync_entities,
    sync_extractions,
    sync_summaries,
)
from chatlore.store import EdgeType, GraphStore, Label, SQLiteStore
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
        source=SourceKind.CHATGPT,
        external_id=conversation_id,
        title=f"Title {conversation_id}",
        messages=messages,
    )


def _load(store: GraphStore, *conversations: Conversation) -> None:
    for conversation in conversations:
        store.upsert_conversation(conversation)
    sync_chunks(store, conversations)


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


# -- reading the model's answer ------------------------------------------------


def _answer(*passages: dict[str, object]) -> str:
    return json.dumps({"passages": list(passages)})


def test_a_well_formed_answer_is_read_per_passage() -> None:
    answer = _answer(
        {
            "passage": 2,
            "entities": [
                {"name": "PostgreSQL", "type": "Tool", "description": "A database."},
                {"name": "ChatLore", "type": "project", "description": "A knowledge base."},
            ],
            "relationships": [
                {
                    "source": "ChatLore",
                    "target": "postgresql",
                    "description": "Used it.",
                    "strength": 8,
                }
            ],
        },
        {"passage": 1, "entities": [], "relationships": []},
    )

    first, second = parse_extractions(answer, 2)

    assert first == Extraction()
    assert second == Extraction(
        (
            ExtractedEntity("PostgreSQL", "tool", "A database."),
            ExtractedEntity("ChatLore", "project", "A knowledge base."),
        ),
        (ExtractedRelationship("ChatLore", "PostgreSQL", "Used it.", 8),),
    )


def test_malformed_items_are_dropped_not_the_whole_answer() -> None:
    answer = _answer(
        {
            "passage": 1,
            "entities": [
                {"name": "  Rust  ", "description": "A language."},
                {"name": "rust", "type": "tool"},
                {"name": ""},
                "not an object",
                {"type": "tool"},
            ],
            "relationships": [
                {"source": "Rust", "target": "Go", "description": "Unknown end."},
                {"source": "Rust", "target": "rust", "description": "Loop."},
            ],
        },
        {"passage": 7, "entities": [{"name": "Out of range"}]},
    )

    [extraction] = parse_extractions(answer, 1)

    assert extraction == Extraction((ExtractedEntity("Rust", "other", "A language."),), ())


@pytest.mark.parametrize(("given", "kept"), [(15, 10), (0, 1), (6.6, 7), ("high", 5), (True, 5)])
def test_relationship_strength_is_kept_within_one_to_ten(given: object, kept: int) -> None:
    answer = _answer(
        {
            "passage": 1,
            "entities": [{"name": "A"}, {"name": "B"}],
            "relationships": [{"source": "A", "target": "B", "strength": given}],
        }
    )

    [extraction] = parse_extractions(answer, 1)

    assert extraction is not None
    assert extraction.relationships[0].strength == kept


def test_a_passage_left_out_comes_back_as_none() -> None:
    assert parse_extractions(_answer({"passage": 2, "entities": []}), 2) == [None, Extraction()]


@pytest.mark.parametrize("answer", ["not json", "[1, 2]", '{"entities": []}'])
def test_an_unreadable_answer_raises(answer: str) -> None:
    with pytest.raises(ExtractionError):
        parse_extractions(answer, 1)


def test_the_request_numbers_every_passage_and_suggests_types() -> None:
    system, user = extraction_messages(["First text.", "Second text."])

    assert system.role == "system"
    assert all(kind in system.content for kind in SUGGESTED_TYPES)
    assert user.content == "### Passage 1\nFirst text.\n\n### Passage 2\nSecond text."


def test_the_cache_is_keyed_by_model_and_survives_a_round_trip(cache: ExtractionCache) -> None:
    extraction = Extraction(
        (ExtractedEntity("Zoë", "person", "Café owner."),),
        (ExtractedRelationship("Zoë", "Zoë Café", "Owns it.", 9),),
    )
    cache.put_many("model-a", {"hash": extraction})

    assert cache.get_many("model-a", ["hash", "other"]) == {"hash": extraction}
    assert cache.get_many("model-b", ["hash"]) == {}


# -- running extraction --------------------------------------------------------


def test_every_chunk_is_read_once_in_batches(store: GraphStore, cache: ExtractionCache) -> None:
    _load(
        store,
        _conversation("one", "Alice likes Postgres.", "Bob prefers SQLite.", "Carol uses Rust."),
        _conversation("two", "Dave wrote Python.", "Erin tried Go."),
    )
    llm = FakeLLM()

    first = sync_extractions(store, llm, cache, batch_size=2, workers=2)
    second = sync_extractions(store, llm, cache, batch_size=2, workers=2)

    assert (first.extracted, first.already_done, first.failed) == (5, 0, 0)
    assert (first.input_tokens, first.output_tokens) == (50, 100)
    assert sorted(len(request) for request in llm.requests) == [1, 2, 2]
    assert (second.extracted, second.already_done) == (0, 5)
    assert len(llm.requests) == 3


def test_identical_text_is_read_once(store: GraphStore, cache: ExtractionCache) -> None:
    _load(store, _conversation("one", "Thanks Alice."), _conversation("two", "Thanks Alice."))
    llm = FakeLLM()

    report = sync_extractions(store, llm, cache)

    assert report.extracted == 1
    assert llm.requests == [["Thanks Alice."]]
    assert pending_extractions(store, cache, llm.name) == []


def test_limit_reads_only_some_chunks(store: GraphStore, cache: ExtractionCache) -> None:
    _load(store, _conversation("one", "Alice here.", "Bob here.", "Carol here."))

    report = sync_extractions(store, FakeLLM(), cache, batch_size=1, limit=2)

    assert report.extracted == 2
    assert len(pending_extractions(store, cache, "fake-llm")) == 1


def test_an_unreadable_answer_is_asked_again_next_run(
    store: GraphStore, cache: ExtractionCache
) -> None:
    _load(store, _conversation("one", "Alice here.", "Bob here."))

    first = sync_extractions(store, FakeLLM(broken_requests=[1]), cache, batch_size=1, workers=1)
    retry = FakeLLM()
    second = sync_extractions(store, retry, cache, batch_size=1)

    assert (first.extracted, first.failed) == (1, 1)
    assert (second.extracted, second.already_done) == (1, 1)
    assert len(retry.requests) == 1


def test_an_unreachable_model_stops_the_run_and_keeps_what_was_read(
    store: GraphStore, cache: ExtractionCache
) -> None:
    _load(store, _conversation("one", "Alice here.", "Bob here.", "Carol here."))

    with pytest.raises(LLMError):
        sync_extractions(store, FakeLLM(fail_on_request=2), cache, batch_size=1, workers=1)

    assert len(pending_extractions(store, cache, "fake-llm")) == 2


def test_another_model_reads_everything_again(store: GraphStore, cache: ExtractionCache) -> None:
    _load(store, _conversation("one", "Alice here."))
    sync_extractions(store, FakeLLM("model-a"), cache)

    report = sync_extractions(store, FakeLLM("model-b"), cache)

    assert report.extracted == 1


# -- assembling the graph ------------------------------------------------------


def _entities(store: GraphStore) -> dict[str, dict[str, object]]:
    return {str(node.props["name"]): node.props for node in store.find_nodes(Label.ENTITY)}


def test_mentions_of_one_name_become_one_entity(store: GraphStore, cache: ExtractionCache) -> None:
    _load(store, _conversation("one", "Alice moved Postgres.", "Postgres is fast."))
    sync_extractions(store, FakeLLM(), cache)

    report = sync_entities(store, cache, "fake-llm")

    entities = _entities(store)
    assert sorted(entities) == ["Alice", "Postgres"]
    assert entities["Postgres"]["mentions"] == 2
    assert entities["Postgres"]["type"] == "concept"
    assert len(entities["Postgres"]["descriptions"]) == 2  # type: ignore[arg-type]
    assert (report.entities, report.relationships, report.mentions) == (2, 1, 3)

    postgres = next(n for n in store.find_nodes(Label.ENTITY) if n.props["name"] == "Postgres")
    alice = next(n for n in store.find_nodes(Label.ENTITY) if n.props["name"] == "Alice")
    mentioned_in = store.neighbors(postgres.id, [EdgeType.MENTIONS], direction="in")
    assert {node.label for _, node in mentioned_in} == {Label.CHUNK}
    [(edge, target)] = store.neighbors(alice.id, [EdgeType.RELATED_TO])
    assert target.id == postgres.id
    assert edge.props == {"descriptions": ["Alice with Postgres"], "weight": 6, "count": 1}


def test_entities_follow_the_text_after_a_re_import(
    store: GraphStore, cache: ExtractionCache
) -> None:
    _load(store, _conversation("one", "Alice moved Postgres."))
    sync_extractions(store, FakeLLM(), cache)
    sync_entities(store, cache, "fake-llm")

    _load(store, _conversation("one", "Bob moved SQLite."))
    sync_extractions(store, FakeLLM(), cache)
    sync_entities(store, cache, "fake-llm")

    assert sorted(_entities(store)) == ["Bob", "SQLite"]


def test_a_rebuilt_database_gets_its_entities_from_the_cache(cache: ExtractionCache) -> None:
    conversation = _conversation("one", "Alice moved Postgres.")
    with SQLiteStore(":memory:") as first:
        _load(first, conversation)
        sync_extractions(first, FakeLLM(), cache)
    llm = FakeLLM()

    with SQLiteStore(":memory:") as rebuilt:
        _load(rebuilt, conversation)
        report = sync_extractions(rebuilt, llm, cache)
        sync_entities(rebuilt, cache, "fake-llm")

        assert report.extracted == 0
        assert llm.requests == []
        assert sorted(_entities(rebuilt)) == ["Alice", "Postgres"]


def test_only_the_named_model_builds_the_graph(store: GraphStore, cache: ExtractionCache) -> None:
    _load(store, _conversation("one", "Alice here."))
    sync_extractions(store, FakeLLM("model-a"), cache)

    report = sync_entities(store, cache, "model-b")

    assert report.entities == 0


# -- cleaning up and summarising -------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Drive D:", "drive d"),
        ("Challenge fees", "Challenge fee"),
        ("Stocks", "stock"),
        ("claude-code", "Claude  Code"),
        ("snake_case", "Snake Case"),
        (" PostgreSQL.", "postgresql"),
    ],
)
def test_names_that_differ_only_in_form_are_one_entity(first: str, second: str) -> None:
    assert entity_key(first) == entity_key(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [("C++", "C"), ("C#", "C"), ("Llama", "Ollama"), ("Bus", "Bu"), ("Class", "Clas")],
)
def test_names_that_mean_different_things_stay_apart(first: str, second: str) -> None:
    assert entity_key(first) != entity_key(second)


def test_invented_types_that_mean_a_suggested_one_are_mapped() -> None:
    assert canonical_type("company") == "organization"
    assert canonical_type("city") == "place"
    assert canonical_type("tool") == "tool"
    assert canonical_type("law") == "law"


def test_a_name_of_only_punctuation_is_not_an_entity() -> None:
    [extraction] = parse_extractions(_answer({"passage": 1, "entities": [{"name": ":"}]}), 1)

    assert extraction == Extraction()


def _remember(store: GraphStore, cache: ExtractionCache, found: dict[str, Extraction]) -> None:
    """Load one conversation with a message per text and cache what the model 'found'."""
    _load(store, _conversation("one", *found))
    cache.put_many("model", {text_hash(text): extraction for text, extraction in found.items()})


def test_merged_names_share_one_entity_and_drop_links_to_themselves(
    store: GraphStore, cache: ExtractionCache
) -> None:
    _remember(
        store,
        cache,
        {
            "First.": Extraction(
                (
                    ExtractedEntity("Challenge fees", "company", "Fees for a challenge."),
                    ExtractedEntity("Challenge fee", "concept", "A single fee."),
                ),
                (ExtractedRelationship("Challenge fees", "Challenge fee", "Same thing.", 5),),
            ),
            "Second.": Extraction((ExtractedEntity("Challenge fee", "company", "Paid once."),)),
        },
    )

    report = sync_entities(store, cache, "model")

    [entity] = store.find_nodes(Label.ENTITY)
    assert entity.props["name"] == "Challenge fee"
    assert entity.props["type"] == "organization"
    assert entity.props["mentions"] == 2
    assert (report.entities, report.relationships) == (1, 0)


def test_the_request_lists_each_entity_with_its_descriptions() -> None:
    system, user = summary_messages(
        [
            EntityToSummarise("Rust", "tool", ("A language.", "Used for the CLI.")),
            EntityToSummarise("Lisbon", "place", ("A city.",)),
        ]
    )

    assert "summaries" in system.content
    assert user.content == (
        "### Entity 1: Rust (tool)\n- A language.\n- Used for the CLI.\n\n"
        "### Entity 2: Lisbon (place)\n- A city."
    )


def test_summaries_are_read_per_entity() -> None:
    answer = json.dumps(
        {
            "summaries": [
                {"entity": 2, "summary": "  A   city. "},
                {"entity": 1, "summary": ""},
                {"entity": 9, "summary": "Out of range."},
            ]
        }
    )

    assert parse_summaries(answer, 2) == [None, "A city."]
    with pytest.raises(ExtractionError):
        parse_summaries('{"passages": []}', 1)


def test_the_summary_key_ignores_the_order_descriptions_were_found_in() -> None:
    first = EntityToSummarise("Rust", "tool", ("A language.", "Fast."))

    assert first.key == EntityToSummarise("Rust", "concept", ("Fast.", "A language.")).key
    assert first.key != EntityToSummarise("Rust", "tool", ("A language.",)).key


def _summaries(store: GraphStore) -> dict[str, object]:
    return {
        str(node.props["name"]): node.props["summary"] for node in store.find_nodes(Label.ENTITY)
    }


def test_only_entities_described_more_than_once_are_summarised(
    store: GraphStore, cache: ExtractionCache
) -> None:
    _load(store, _conversation("one", "Alice moved Postgres.", "Postgres is fast."))
    llm = FakeLLM()
    sync_extractions(store, llm, cache)
    sync_entities(store, cache, llm.name)

    report = sync_summaries(store, llm, cache)

    summaries = _summaries(store)
    assert llm.summary_requests == [["Postgres"]]
    assert report.summarised == 1
    assert summaries["Alice"] == "Alice appears in: Alice moved Postgres."
    assert set(str(summaries["Postgres"]).split(" / ")) == {
        "Postgres appears in: Alice moved Postgres.",
        "Postgres appears in: Postgres is fast.",
    }
    assert pending_summaries(store) == []


def test_summaries_survive_a_rebuild_and_follow_new_descriptions(
    store: GraphStore, cache: ExtractionCache
) -> None:
    _load(store, _conversation("one", "Alice moved Postgres.", "Postgres is fast."))
    llm = FakeLLM()
    sync_extractions(store, llm, cache)
    sync_entities(store, cache, llm.name)
    sync_summaries(store, llm, cache)

    sync_entities(store, cache, llm.name)
    assert pending_summaries(store) == []

    _load(
        store, _conversation("one", "Alice moved Postgres.", "Postgres is fast.", "Postgres won.")
    )
    sync_extractions(store, llm, cache)
    sync_entities(store, cache, llm.name)
    sync_summaries(store, llm, cache)

    assert llm.summary_requests == [["Postgres"], ["Postgres"]]
    assert "Postgres won." in str(_summaries(store)["Postgres"])


def test_an_unreadable_summary_is_asked_again_next_run(
    store: GraphStore, cache: ExtractionCache
) -> None:
    _load(store, _conversation("one", "Alice moved Postgres.", "Postgres is fast."))
    sync_extractions(store, FakeLLM(), cache, batch_size=2)
    sync_entities(store, cache, "fake-llm")

    first = sync_summaries(store, FakeLLM(broken_requests=[1]), cache)
    second = sync_summaries(store, FakeLLM(), cache)

    assert (first.summarised, first.failed) == (0, 1)
    assert second.summarised == 1
    assert pending_summaries(store) == []
