"""Deterministic identifiers and content hashes.

Identifiers are derived from where a record came from, so importing the same
export twice yields the same ids. Content hashes are derived from what a record
contains, so a changed record can be detected without comparing every field.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_ID_HEX_LENGTH = 24
_SEPARATOR = "\x1f"


def canonical_json(value: Any) -> str:
    """Serialise ``value`` to JSON with a stable key order and no extra whitespace."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def content_hash(value: Any) -> str:
    """Return the SHA-256 hex digest of the canonical JSON form of ``value``."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _short_digest(*parts: str) -> str:
    joined = _SEPARATOR.join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:_ID_HEX_LENGTH]


def conversation_id(source: str, external_id: str) -> str:
    """Return the stable id of a conversation from its source and the id it had there."""
    return f"conv_{_short_digest(source, external_id)}"


def message_id(conversation: str, external_id: str) -> str:
    """Return the stable id of a message within ``conversation``."""
    return f"msg_{_short_digest(conversation, external_id)}"


def chunk_id(message: str, order: int, text: str) -> str:
    """Return the stable id of a chunk.

    The text is part of the identity, so a chunk whose text changed gets a new
    id and can never keep an embedding computed for the old text.
    """
    return f"chunk_{_short_digest(message, str(order), text)}"


def entity_id(key: str) -> str:
    """Return the stable id of an entity from its normalised name.

    Mentions whose names differ only in case or spacing share one id, so the
    same entity found in two chunks becomes one node.
    """
    return f"ent_{_short_digest(key)}"
