"""Split messages into chunks sized for embedding and retrieval.

A chunk is a run of whole paragraphs and whole fenced code blocks that fits a
size budget. Only what the user and the assistant actually wrote is chunked:
tool output, attachment dumps, and other machine text stay reachable through
full-text search on the message but are not embedded, because they would bury
the conversation itself under scraped pages and JSON.

Sizes are measured in characters and reported as estimated tokens at four
characters per token. That is close enough to size chunks, and it avoids
shipping a tokenizer for every embedding model a user might pick.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from chatlore.ids import chunk_id
from chatlore.models import Conversation, Message, PartType, Role

CHUNKER_VERSION = 1
CHARS_PER_TOKEN = 4
DEFAULT_MAX_TOKENS = 400
DEFAULT_OVERLAP_TOKENS = 40

_CHUNKED_ROLES = frozenset({Role.USER, Role.ASSISTANT})
_FENCE = re.compile(r"^(```|~~~)", re.MULTILINE)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable piece of a message."""

    id: str
    conversation_id: str
    message_id: str
    order: int
    text: str

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.text) // CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class _Unit:
    text: str
    is_code: bool


def chunk_conversation(
    conversation: Conversation,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Chunk every user and assistant message, on every branch."""
    chunks: list[Chunk] = []
    for message in conversation.messages:
        chunks.extend(chunk_message(message, conversation.id, max_tokens, overlap_tokens))
    return chunks


def chunk_message(
    message: Message,
    conversation_id: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Split one message into chunks. Returns nothing for tool and system messages."""
    if message.role not in _CHUNKED_ROLES:
        return []
    if max_tokens <= 0 or overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("need max_tokens > overlap_tokens >= 0")

    max_chars = max_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN

    units: list[_Unit] = []
    for part in message.content:
        if part.type is PartType.TEXT:
            units.extend(_split_markdown(part.text))
        elif part.type is PartType.CODE and part.text.strip():
            units.append(_Unit(part.render(), is_code=True))
    units = [piece for unit in units for piece in _fit(unit, max_chars)]

    texts = _pack(units, max_chars, overlap_chars)
    return [
        Chunk(
            id=chunk_id(message.id, order, text),
            conversation_id=conversation_id,
            message_id=message.id,
            order=order,
            text=text,
        )
        for order, text in enumerate(texts)
    ]


def _split_markdown(text: str) -> list[_Unit]:
    """Split prose into paragraphs while keeping fenced code blocks whole."""
    units: list[_Unit] = []
    position = 0
    fences = list(_FENCE.finditer(text))
    index = 0
    while index + 1 < len(fences):
        opening, closing = fences[index], fences[index + 1]
        if closing.group(1) != opening.group(1):
            index += 1
            continue
        end_of_block = text.find("\n", closing.end())
        end_of_block = len(text) if end_of_block == -1 else end_of_block
        units.extend(_paragraphs(text[position : opening.start()]))
        units.append(_Unit(text[opening.start() : end_of_block].strip(), is_code=True))
        position = end_of_block
        index += 2
    units.extend(_paragraphs(text[position:]))
    return [unit for unit in units if unit.text]


def _paragraphs(text: str) -> list[_Unit]:
    return [
        _Unit(paragraph.strip(), is_code=False)
        for paragraph in re.split(r"\n\s*\n", text)
        if paragraph.strip()
    ]


def _fit(unit: _Unit, max_chars: int) -> list[_Unit]:
    """Break a unit that is larger than a whole chunk into smaller units."""
    if len(unit.text) <= max_chars:
        return [unit]
    pieces = unit.text.split("\n") if unit.is_code else _SENTENCE_END.split(unit.text)
    joiner = "\n" if unit.is_code else " "

    fitted: list[_Unit] = []
    current = ""
    for piece in pieces:
        while len(piece) > max_chars:  # a single line or sentence longer than a chunk
            if current:
                fitted.append(_Unit(current, unit.is_code))
                current = ""
            fitted.append(_Unit(piece[:max_chars], unit.is_code))
            piece = piece[max_chars:]
        candidate = f"{current}{joiner}{piece}" if current else piece
        if len(candidate) > max_chars:
            fitted.append(_Unit(current, unit.is_code))
            current = piece
        else:
            current = candidate
    if current.strip():
        fitted.append(_Unit(current, unit.is_code))
    return fitted


def _pack(units: list[_Unit], max_chars: int, overlap_chars: int) -> list[str]:
    """Greedily pack units into chunks, repeating a short prose tail as overlap."""
    chunks: list[str] = []
    current: list[_Unit] = []
    size = 0
    for unit in units:
        added = len(unit.text) + (2 if current else 0)
        if current and size + added > max_chars:
            chunks.append("\n\n".join(u.text for u in current))
            tail = current[-1]
            carry = (
                not tail.is_code
                and len(tail.text) <= overlap_chars
                and len(tail.text) + 2 + len(unit.text) <= max_chars
            )
            current = [tail] if carry else []
            size = len(tail.text) if carry else 0
            added = len(unit.text) + (2 if current else 0)
        current.append(unit)
        size += added
    if current:
        chunks.append("\n\n".join(u.text for u in current))
    return chunks
