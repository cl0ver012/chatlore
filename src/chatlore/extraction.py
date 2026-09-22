"""Entity and relationship extraction with a language model.

The model reads chunks and names the entities each one mentions and how they
relate. It chooses entity types itself; a suggested list keeps the common kinds
consistent, so one thing is not filed under five different labels.

Answers are cached on disk by model, prompt version, and text hash. The graph
is assembled from that cache instead of being edited in place, so text removed
by a re-import takes its entities with it, a rebuilt database costs no model
calls, and the merge rules can change without extracting again.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Self

from chatlore.ids import content_hash
from chatlore.llm import ChatMessage

PROMPT_VERSION = "1"
"""Bump when the prompt changes in a way that should re-read every chunk."""

SUGGESTED_TYPES = ("person", "organization", "project", "tool", "concept", "place", "event")
OTHER_TYPE = "other"

TYPE_ALIASES = {
    "company": "organization",
    "organisation": "organization",
    "institution": "organization",
    "agency": "organization",
    "business": "organization",
    "software": "tool",
    "technology": "tool",
    "language": "tool",
    "framework": "tool",
    "library": "tool",
    "platform": "tool",
    "service": "tool",
    "app": "tool",
    "application": "tool",
    "protocol": "tool",
    "city": "place",
    "country": "place",
    "location": "place",
    "region": "place",
    "idea": "concept",
    "topic": "concept",
    "method": "concept",
    "technique": "concept",
    "conference": "event",
    "meeting": "event",
}
"""Types the model sometimes invents that clearly mean one of the suggested ones."""

SUMMARY_PROMPT_VERSION = "1"
"""Bump when the summary prompt changes in a way that should rewrite every summary."""

MAX_DESCRIPTIONS = 20
"""Descriptions sent per entity when asking for its summary."""

_MIN_STRENGTH, _DEFAULT_STRENGTH, _MAX_STRENGTH = 1, 5, 10

SYSTEM_PROMPT = f"""\
You build a knowledge graph from one person's conversations with AI assistants.
For each numbered passage, list the entities it mentions and how they relate.

Entities are specific things worth remembering: people, organizations, projects,
tools and technologies, concepts, places, and events. Skip generic words such as
"user", "assistant", "code", or "question", and labels that only make sense
inside the passage, such as "Solution A", "Option 2", or "Step 3". Use the
fullest common name, for example "PostgreSQL" rather than "the database".
- type: one of {", ".join(SUGGESTED_TYPES)}, or another single lowercase word if
  none fits.
- description: one sentence about the entity, based only on the passage.

Relationships connect two entities from the same passage's list.
- description: one sentence on how they relate.
- strength: 1 to 10, how strongly the passage links them.

Reply with only a JSON object in this shape, including every passage and using
empty lists when nothing qualifies:
{{"passages": [{{"passage": 1,
  "entities": [{{"name": "...", "type": "...", "description": "..."}}],
  "relationships": [{{"source": "...", "target": "...", "description": "...",
                     "strength": 7}}]}}]}}"""


class ExtractionError(Exception):
    """The model's answer could not be read as an extraction."""


@dataclass(frozen=True, slots=True)
class ExtractedEntity:
    name: str
    type: str
    description: str


@dataclass(frozen=True, slots=True)
class ExtractedRelationship:
    source: str
    target: str
    description: str
    strength: int


@dataclass(frozen=True, slots=True)
class Extraction:
    """What the model found in one chunk."""

    entities: tuple[ExtractedEntity, ...] = ()
    relationships: tuple[ExtractedRelationship, ...] = ()

    def to_json(self) -> str:
        return json.dumps(
            {
                "entities": [[e.name, e.type, e.description] for e in self.entities],
                "relationships": [
                    [r.source, r.target, r.description, r.strength] for r in self.relationships
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> Extraction:
        data = json.loads(value)
        return cls(
            entities=tuple(ExtractedEntity(*item) for item in data["entities"]),
            relationships=tuple(ExtractedRelationship(*item) for item in data["relationships"]),
        )


_SEPARATORS = re.compile(r"[-_]+")
_TRAILING_PUNCTUATION = ".:;,!?"


def entity_key(name: str) -> str:
    """The form of a name that decides whether two mentions are the same entity.

    Case, spacing, hyphens and underscores between words, trailing punctuation,
    and a plural "s" on the last word are ignored, so "Drive D:" and "drive d",
    or "Challenge fees" and "challenge fee", are one entity. Punctuation inside a
    name is kept, so "C++", "C#", and "C" stay apart.
    """
    words = _SEPARATORS.sub(" ", name).casefold().split()
    key = " ".join(words).rstrip(_TRAILING_PUNCTUATION).rstrip()
    head, _, last = key.rpartition(" ")
    if len(last) > 3 and last.endswith("s") and not last.endswith(("ss", "us", "is")):
        key = f"{head} {last[:-1]}" if head else last[:-1]
    return key


def canonical_type(kind: str) -> str:
    """Map an invented type onto a suggested one when it clearly means the same."""
    return TYPE_ALIASES.get(kind, kind)


def extraction_messages(texts: Sequence[str]) -> list[ChatMessage]:
    """The request that asks the model to read ``texts`` as numbered passages."""
    passages = "\n\n".join(
        f"### Passage {number}\n{text}" for number, text in enumerate(texts, start=1)
    )
    return [ChatMessage("system", SYSTEM_PROMPT), ChatMessage("user", passages)]


def parse_extractions(answer: str, count: int) -> list[Extraction | None]:
    """Read the model's answer for ``count`` passages.

    Malformed items are dropped rather than failing the whole answer, and a
    relationship is kept only when both ends are entities of its passage. A
    passage the model left out comes back as ``None`` so it is asked again.
    """
    try:
        data = json.loads(answer)
    except json.JSONDecodeError as error:
        raise ExtractionError(f"the answer is not JSON: {error}") from error
    passages = data.get("passages") if isinstance(data, dict) else None
    if not isinstance(passages, list):
        raise ExtractionError('the answer has no "passages" list')

    found: list[Extraction | None] = [None] * count
    for passage in passages:
        if not isinstance(passage, dict):
            continue
        number = passage.get("passage")
        if not isinstance(number, int) or not 1 <= number <= count:
            continue
        found[number - 1] = _passage(passage)
    return found


def _passage(passage: Mapping[str, Any]) -> Extraction:
    entities: dict[str, ExtractedEntity] = {}
    for item in _list(passage.get("entities")):
        name = _text(item.get("name"))
        key = entity_key(name)
        if key and key not in entities:
            kind = _text(item.get("type")).lower() or OTHER_TYPE
            entities[key] = ExtractedEntity(name, kind, _text(item.get("description")))

    relationships: list[ExtractedRelationship] = []
    for item in _list(passage.get("relationships")):
        source, target = (
            entity_key(_text(item.get("source"))),
            entity_key(_text(item.get("target"))),
        )
        if source in entities and target in entities and source != target:
            relationships.append(
                ExtractedRelationship(
                    entities[source].name,
                    entities[target].name,
                    _text(item.get("description")),
                    _strength(item.get("strength")),
                )
            )
    return Extraction(tuple(entities.values()), tuple(relationships))


def _list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _text(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _strength(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _DEFAULT_STRENGTH
    return max(_MIN_STRENGTH, min(_MAX_STRENGTH, round(value)))


SUMMARY_SYSTEM_PROMPT = """\
You maintain a knowledge graph built from one person's conversations with AI
assistants. Each numbered entity below was described several times, each time
from a different passage. Write one summary per entity: one or two sentences, at
most 50 words, saying what the entity is and why it matters in these
conversations. Keep the most important specific facts and leave out the rest.
Do not add anything the descriptions do not say.

Reply with only a JSON object in this shape, including every entity:
{"summaries": [{"entity": 1, "summary": "..."}]}"""


@dataclass(frozen=True, slots=True)
class EntityToSummarise:
    name: str
    type: str
    descriptions: tuple[str, ...]

    @property
    def key(self) -> str:
        """Identifies this set of descriptions, whatever order they were found in."""
        return content_hash([self.name, sorted(self.descriptions)])


def summary_messages(entities: Sequence[EntityToSummarise]) -> list[ChatMessage]:
    """The request that asks the model to summarise ``entities`` as numbered items.

    At most ``MAX_DESCRIPTIONS`` descriptions are sent per entity; a summary this
    short does not need more, and it keeps requests for common entities small.
    """
    blocks = "\n\n".join(
        f"### Entity {number}: {entity.name} ({entity.type})\n"
        + "\n".join(f"- {description}" for description in entity.descriptions[:MAX_DESCRIPTIONS])
        for number, entity in enumerate(entities, start=1)
    )
    return [ChatMessage("system", SUMMARY_SYSTEM_PROMPT), ChatMessage("user", blocks)]


def parse_summaries(answer: str, count: int) -> list[str | None]:
    """Read the model's summaries of ``count`` entities; a missing one is ``None``."""
    try:
        data = json.loads(answer)
    except json.JSONDecodeError as error:
        raise ExtractionError(f"the answer is not JSON: {error}") from error
    items = data.get("summaries") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ExtractionError('the answer has no "summaries" list')

    found: list[str | None] = [None] * count
    for item in _list(items):
        number, summary = item.get("entity"), _text(item.get("summary"))
        if isinstance(number, int) and 1 <= number <= count and summary:
            found[number - 1] = summary
    return found


SAME_PROMPT_VERSION = "1"
"""Bump when the duplicate prompt changes in a way that should ask about every pair again."""

SAME_SYSTEM_PROMPT = """\
You maintain a knowledge graph built from one person's conversations with AI
assistants. Each numbered pair below names two entities with their types and
summaries. Decide whether both names refer to the same real thing, such as one
tool, person, organization, or place under two names. Different versions,
different products of one company, and a part and its whole are not the same.
Say true only when you are confident.

Reply with only a JSON object in this shape, including every pair:
{"pairs": [{"pair": 1, "same": false}]}"""

_SIMILAR_NAMES = 0.85
_MIN_PREFIX = 4


@dataclass(frozen=True, slots=True)
class EntityCard:
    """How an entity is shown to the model when comparing it with another."""

    name: str
    type: str
    summary: str


def pair_key(first: str, second: str) -> str:
    """Identifies a pair of entity keys, whichever order they come in."""
    return content_hash(sorted([first, second]))


def duplicate_candidates(keys: Iterable[str]) -> list[tuple[str, str]]:
    """Pairs of entity keys that might name the same thing, for the model to decide.

    Generous on purpose, because the model rejects what is not the same:
    - one name starts with the other, or they are spelled almost alike, when both
      start with the same four letters ("postgre" and "postgresql");
    - one name is the other with words added at the end ("postgre" and
      "postgre database", "claude code" and "claude code cli"), or at the start
      when the shorter has two words or more;
    - one short name is the initials of the other ("kyc" and "know your customer").
    Names are only compared within those groups, so large graphs stay fast.
    """
    ordered = sorted(set(keys))
    by_start: dict[str, list[str]] = {}
    by_first_word: dict[str, list[str]] = {}
    by_last_word: dict[str, list[str]] = {}
    by_initials: dict[str, list[str]] = {}
    for key in ordered:
        words = key.split()
        if len(key) >= _MIN_PREFIX:
            by_start.setdefault(key[:_MIN_PREFIX], []).append(key)
        if len(words) > 1:
            by_first_word.setdefault(words[0], []).append(key)
            by_last_word.setdefault(words[-1], []).append(key)
            by_initials.setdefault("".join(word[0] for word in words), []).append(key)

    pairs: set[tuple[str, str]] = set()

    def add(first: str, second: str) -> None:
        if first != second:
            pairs.add((min(first, second), max(first, second)))

    for key in ordered:
        words = key.split()
        for other in by_first_word.get(words[0], ()):
            if len(other.split()) > len(words) and other.split()[: len(words)] == words:
                add(key, other)
        if len(words) > 1:
            for other in by_last_word.get(words[-1], ()):
                if len(other.split()) > len(words) and other.split()[-len(words) :] == words:
                    add(key, other)
        if len(words) == 1 and 2 <= len(key) <= 6:
            for other in by_initials.get(key, ()):
                add(key, other)
    for group in by_start.values():
        for index, first in enumerate(group):
            for second in group[index + 1 :]:
                if (
                    first.startswith(second)
                    or second.startswith(first)
                    or SequenceMatcher(None, first, second).ratio() >= _SIMILAR_NAMES
                ):
                    add(first, second)
    return sorted(pairs)


def duplicate_messages(pairs: Sequence[tuple[EntityCard, EntityCard]]) -> list[ChatMessage]:
    """The request that asks the model whether each numbered pair is one thing."""
    blocks = "\n\n".join(
        f"### Pair {number}\n"
        + "\n".join(f"- {card.name} ({card.type}): {card.summary}" for card in pair)
        for number, pair in enumerate(pairs, start=1)
    )
    return [ChatMessage("system", SAME_SYSTEM_PROMPT), ChatMessage("user", blocks)]


def parse_duplicates(answer: str, count: int) -> list[bool | None]:
    """Read the model's verdicts on ``count`` pairs; a missing one is ``None``."""
    try:
        data = json.loads(answer)
    except json.JSONDecodeError as error:
        raise ExtractionError(f"the answer is not JSON: {error}") from error
    items = data.get("pairs") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ExtractionError('the answer has no "pairs" list')

    found: list[bool | None] = [None] * count
    for item in _list(items):
        number, same = item.get("pair"), item.get("same")
        if isinstance(number, int) and 1 <= number <= count and isinstance(same, bool):
            found[number - 1] = same
    return found


class ExtractionCache:
    """What the model answered, on disk, so no question is asked twice.

    Extractions are keyed by model, prompt version, and text hash; summaries by
    the entity's name and set of descriptions; duplicate verdicts by the pair of
    entity names; topic reports by the set of entities in the topic.
    """

    def __init__(self, path: Path | str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path))
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS extractions ("
            "model TEXT NOT NULL, prompt_version TEXT NOT NULL, text_hash TEXT NOT NULL, "
            "extraction TEXT NOT NULL, PRIMARY KEY (model, prompt_version, text_hash))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS summaries ("
            "model TEXT NOT NULL, prompt_version TEXT NOT NULL, entity_key TEXT NOT NULL, "
            "summary TEXT NOT NULL, PRIMARY KEY (model, prompt_version, entity_key))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS verdicts ("
            "model TEXT NOT NULL, prompt_version TEXT NOT NULL, pair_key TEXT NOT NULL, "
            "same INTEGER NOT NULL, PRIMARY KEY (model, prompt_version, pair_key))"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS reports ("
            "model TEXT NOT NULL, prompt_version TEXT NOT NULL, topic_key TEXT NOT NULL, "
            "report TEXT NOT NULL, PRIMARY KEY (model, prompt_version, topic_key))"
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def get_many(self, model: str, hashes: Sequence[str]) -> dict[str, Extraction]:
        found: dict[str, Extraction] = {}
        for digest in hashes:
            row = self._connection.execute(
                "SELECT extraction FROM extractions "
                "WHERE model = ? AND prompt_version = ? AND text_hash = ?",
                (model, PROMPT_VERSION, digest),
            ).fetchone()
            if row is not None:
                found[digest] = Extraction.from_json(row[0])
        return found

    def put_many(self, model: str, extractions: Mapping[str, Extraction]) -> None:
        self._connection.executemany(
            "INSERT OR REPLACE INTO extractions (model, prompt_version, text_hash, extraction) "
            "VALUES (?, ?, ?, ?)",
            [
                (model, PROMPT_VERSION, digest, extraction.to_json())
                for digest, extraction in extractions.items()
            ],
        )
        self._connection.commit()

    def get_summaries(self, model: str, keys: Sequence[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for key in keys:
            row = self._connection.execute(
                "SELECT summary FROM summaries "
                "WHERE model = ? AND prompt_version = ? AND entity_key = ?",
                (model, SUMMARY_PROMPT_VERSION, key),
            ).fetchone()
            if row is not None:
                found[key] = str(row[0])
        return found

    def put_summaries(self, model: str, summaries: Mapping[str, str]) -> None:
        self._connection.executemany(
            "INSERT OR REPLACE INTO summaries (model, prompt_version, entity_key, summary) "
            "VALUES (?, ?, ?, ?)",
            [(model, SUMMARY_PROMPT_VERSION, key, summary) for key, summary in summaries.items()],
        )
        self._connection.commit()

    def get_verdicts(self, model: str, keys: Sequence[str]) -> dict[str, bool]:
        found: dict[str, bool] = {}
        for key in keys:
            row = self._connection.execute(
                "SELECT same FROM verdicts WHERE model = ? AND prompt_version = ? AND pair_key = ?",
                (model, SAME_PROMPT_VERSION, key),
            ).fetchone()
            if row is not None:
                found[key] = bool(row[0])
        return found

    def put_verdicts(self, model: str, verdicts: Mapping[str, bool]) -> None:
        self._connection.executemany(
            "INSERT OR REPLACE INTO verdicts (model, prompt_version, pair_key, same) "
            "VALUES (?, ?, ?, ?)",
            [(model, SAME_PROMPT_VERSION, key, int(same)) for key, same in verdicts.items()],
        )
        self._connection.commit()

    def get_reports(self, model: str, version: str, keys: Sequence[str]) -> dict[str, str]:
        """Topic reports as the JSON they were stored as; the topics module reads them."""
        found: dict[str, str] = {}
        for key in keys:
            row = self._connection.execute(
                "SELECT report FROM reports WHERE model = ? AND prompt_version = ? "
                "AND topic_key = ?",
                (model, version, key),
            ).fetchone()
            if row is not None:
                found[key] = str(row[0])
        return found

    def put_reports(self, model: str, version: str, reports: Mapping[str, str]) -> None:
        self._connection.executemany(
            "INSERT OR REPLACE INTO reports (model, prompt_version, topic_key, report) "
            "VALUES (?, ?, ?, ?)",
            [(model, version, key, report) for key, report in reports.items()],
        )
        self._connection.commit()
