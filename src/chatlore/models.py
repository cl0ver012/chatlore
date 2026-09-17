"""Canonical conversation model.

Every importer converts its source format into these models, and everything
downstream (storage, chunking, extraction, search) reads only these models.
Keeping one shape in the middle is what lets a new source be added without
touching the rest of the system.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from chatlore.ids import content_hash


class SourceKind(StrEnum):
    """Where a conversation came from."""

    CHATGPT = "chatgpt"
    CLAUDE = "claude"
    GEMINI = "gemini"
    MARKDOWN = "markdown"
    NOTE = "note"
    CHATLORE = "chatlore"
    CLAUDE_CODE = "claude_code"
    CODEX = "codex"


class Role(StrEnum):
    """Who wrote a message."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class PartType(StrEnum):
    """The kind of content held by one part of a message."""

    TEXT = "text"
    CODE = "code"
    OTHER = "other"


def _to_utc(value: datetime | None) -> datetime | None:
    """Normalise a timestamp to timezone-aware UTC. Naive values are taken as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ContentPart(_Model):
    """One piece of a message: prose, a code block, or something else kept as text."""

    type: PartType = PartType.TEXT
    text: str
    language: str | None = None

    def render(self) -> str:
        """Return the part as plain text, with code wrapped in a fenced block."""
        if self.type is PartType.CODE:
            return f"```{self.language or ''}\n{self.text}\n```"
        return self.text


class Attachment(_Model):
    """A file referenced by a message. The bytes themselves are not stored here."""

    name: str
    mime: str | None = None
    sha256: str | None = None


class Message(_Model):
    """A single message. ``parent_id`` links messages into a tree of branches."""

    id: str = Field(min_length=1)
    parent_id: str | None = None
    role: Role
    created_at: datetime | None = None
    content: list[ContentPart] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _normalise_created_at = field_validator("created_at")(_to_utc)

    @property
    def text(self) -> str:
        """The full message as plain text, parts separated by blank lines."""
        return "\n\n".join(part.render() for part in self.content if part.text)


class Conversation(_Model):
    """A conversation in canonical form.

    Messages form a tree through ``parent_id`` because some sources keep
    regenerated or edited branches. ``current_leaf_id`` marks the end of the
    branch the user last saw; ``linear_messages`` follows it back to the root.
    """

    id: str = Field(min_length=1)
    source: SourceKind
    external_id: str = Field(min_length=1)
    title: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    model: str | None = None
    messages: list[Message] = Field(default_factory=list)
    current_leaf_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    _normalise_timestamps = field_validator("created_at", "updated_at")(_to_utc)

    @model_validator(mode="after")
    def _check_message_tree(self) -> Self:
        by_id: dict[str, Message] = {}
        for message in self.messages:
            if message.id in by_id:
                raise ValueError(f"duplicate message id: {message.id}")
            by_id[message.id] = message

        for message in self.messages:
            if message.parent_id is not None and message.parent_id not in by_id:
                raise ValueError(f"message {message.id} has unknown parent_id {message.parent_id}")

        if self.current_leaf_id is not None and self.current_leaf_id not in by_id:
            raise ValueError(f"current_leaf_id {self.current_leaf_id} is not a message id")

        # Every parent chain must end at a root. Chains already proven sound are
        # remembered, so the whole check stays linear in the number of messages.
        sound: set[str] = set()
        for message in self.messages:
            path: list[str] = []
            seen: set[str] = set()
            cursor: str | None = message.id
            while cursor is not None and cursor not in sound:
                if cursor in seen:
                    raise ValueError(f"cycle in message parents at {cursor}")
                seen.add(cursor)
                path.append(cursor)
                cursor = by_id[cursor].parent_id
            sound.update(path)
        return self

    def linear_messages(self) -> list[Message]:
        """Return the active branch from root to leaf.

        Without ``current_leaf_id`` the messages are returned in stored order,
        which is correct for sources that have no branching.
        """
        if self.current_leaf_id is None:
            return list(self.messages)

        by_id = {message.id: message for message in self.messages}
        branch: list[Message] = []
        cursor: str | None = self.current_leaf_id
        while cursor is not None:
            message = by_id[cursor]
            branch.append(message)
            cursor = message.parent_id
        branch.reverse()
        return branch

    def content_hash(self) -> str:
        """Hash of everything that came from the source, ignoring importer metadata."""
        return content_hash(self.model_dump(mode="json", exclude={"metadata"}))
