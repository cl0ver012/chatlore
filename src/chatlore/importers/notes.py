"""Manual notes, created from the CLI and later from the web UI and MCP server."""

from __future__ import annotations

from datetime import UTC, datetime

from chatlore.ids import content_hash, conversation_id, message_id
from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind

_TITLE_LENGTH = 80


def make_note(
    text: str, title: str | None = None, created_at: datetime | None = None
) -> Conversation:
    """Wrap ``text`` in a single-message conversation with source ``note``."""
    body = text.strip()
    if not body:
        raise ValueError("a note cannot be empty")

    created = created_at or datetime.now(UTC)
    external_id = content_hash({"text": body, "created_at": created.isoformat()})[:32]
    conv_id = conversation_id(SourceKind.NOTE, external_id)
    first_line = body.splitlines()[0]
    fallback = (
        first_line if len(first_line) <= _TITLE_LENGTH else first_line[: _TITLE_LENGTH - 1] + "…"
    )

    return Conversation(
        id=conv_id,
        source=SourceKind.NOTE,
        external_id=external_id,
        title=title or fallback,
        created_at=created,
        updated_at=created,
        messages=[
            Message(
                id=message_id(conv_id, "body"),
                role=Role.USER,
                created_at=created,
                content=[ContentPart(text=body)],
            )
        ],
        metadata={"kind": "note"},
    )
