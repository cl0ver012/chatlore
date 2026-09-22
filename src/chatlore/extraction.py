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
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from chatlore.llm import ChatMessage

PROMPT_VERSION = "1"
"""Bump when the prompt changes in a way that should re-read every chunk."""

SUGGESTED_TYPES = ("person", "organization", "project", "tool", "concept", "place", "event")
OTHER_TYPE = "other"

_MIN_STRENGTH, _DEFAULT_STRENGTH, _MAX_STRENGTH = 1, 5, 10

SYSTEM_PROMPT = f"""\
You build a knowledge graph from one person's conversations with AI assistants.
For each numbered passage, list the entities it mentions and how they relate.

Entities are specific things worth remembering: people, organizations, projects,
tools and technologies, concepts, places, and events. Skip generic words such as
"user", "assistant", "code", or "question". Use the fullest common name, for
example "PostgreSQL" rather than "the database".
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


def entity_key(name: str) -> str:
    """The form of a name that decides whether two mentions are the same entity."""
    return " ".join(name.split()).casefold()


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
        if name and entity_key(name) not in entities:
            kind = _text(item.get("type")).lower() or OTHER_TYPE
            entities[entity_key(name)] = ExtractedEntity(name, kind, _text(item.get("description")))

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


class ExtractionCache:
    """Extractions on disk, keyed by model, prompt version, and text hash."""

    def __init__(self, path: Path | str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(str(path))
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS extractions ("
            "model TEXT NOT NULL, prompt_version TEXT NOT NULL, text_hash TEXT NOT NULL, "
            "extraction TEXT NOT NULL, PRIMARY KEY (model, prompt_version, text_hash))"
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
