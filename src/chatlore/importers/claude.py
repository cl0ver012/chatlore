"""Importer for the Claude.ai data export.

The export (Settings > Privacy > Export data) is a zip that contains
``conversations.json``: a list of conversations, each with ``chat_messages``.
A message has a plain ``text`` field and, in newer exports, a ``content`` list
of typed blocks. Newer exports also carry ``parent_message_uuid``, which
records edits and retries as branches; older ones are a simple sequence.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from chatlore.ids import conversation_id, message_id
from chatlore.importers.base import (
    ImporterError,
    IssueSink,
    as_str,
    from_iso,
    load_first_json,
    report,
)
from chatlore.models import (
    Attachment,
    ContentPart,
    Conversation,
    Message,
    PartType,
    Role,
    SourceKind,
)

_SENDERS = {"human": Role.USER, "assistant": Role.ASSISTANT}

# Claude uses this uuid as the parent of the first message in a conversation.
_ROOT_PARENT = "00000000-0000-4000-8000-000000000000"


class ClaudeImporter:
    """Parses ``conversations.json`` from a Claude export zip, folder, or file."""

    kind = SourceKind.CLAUDE

    def parse(self, path: Path, on_issue: IssueSink | None = None) -> Iterator[Conversation]:
        data = load_first_json(path, "conversations.json")
        if not isinstance(data, list):
            raise ImporterError("Claude conversations.json should contain a list")

        for index, raw in enumerate(data):
            label = f"conversation #{index}"
            try:
                if not isinstance(raw, dict) or not isinstance(raw.get("chat_messages"), list):
                    raise ValueError("not a Claude conversation (no chat_messages)")
                label = as_str(raw.get("name")) or label
                yield _conversation(raw)
            except (KeyError, TypeError, ValueError, ValidationError) as error:
                first_line = str(error).strip().splitlines()
                report(on_issue, label, first_line[0] if first_line else type(error).__name__)


def _conversation(raw: dict[str, Any]) -> Conversation:
    external_id = as_str(raw.get("uuid"))
    if external_id is None:
        raise ValueError("conversation has no uuid")
    conv_id = conversation_id(SourceKind.CLAUDE, external_id)

    kept: dict[str, Message] = {}
    parents: dict[str, str | None] = {}
    order: list[str] = []
    for item in raw["chat_messages"]:
        if not isinstance(item, dict):
            continue
        uuid = as_str(item.get("uuid"))
        if uuid is None or uuid in kept:
            continue
        message = _message(conv_id, uuid, item)
        parents[uuid] = as_str(item.get("parent_message_uuid"))
        if message is not None:
            kept[uuid] = message
            order.append(uuid)

    has_tree = any(parent is not None for parent in parents.values())
    previous: str | None = None
    for uuid in order:
        parent = _nearest_kept(parents.get(uuid), parents, kept) if has_tree else previous
        kept[uuid].parent_id = None if parent is None else kept[parent].id
        previous = uuid

    # The export does not say which branch was on screen, so take the newest message.
    leaf = max(order, key=lambda u: _sort_key(kept[u], order.index(u))) if has_tree else None

    summary = as_str(raw.get("summary"))
    return Conversation(
        id=conv_id,
        source=SourceKind.CLAUDE,
        external_id=external_id,
        title=as_str(raw.get("name")),
        created_at=from_iso(raw.get("created_at")),
        updated_at=from_iso(raw.get("updated_at")),
        messages=[kept[uuid] for uuid in order],
        current_leaf_id=None if leaf is None else kept[leaf].id,
        metadata={"summary": summary} if summary else {},
    )


def _message(conv_id: str, uuid: str, item: dict[str, Any]) -> Message | None:
    role = _SENDERS.get(str(item.get("sender")))
    if role is None:
        return None

    parts = _blocks(item.get("content"))
    if not parts:
        text = as_str(item.get("text"))
        parts = [ContentPart(text=text)] if text and text.strip() else []

    attachments: list[Attachment] = []
    for raw in _dicts(item.get("attachments")):
        name = as_str(raw.get("file_name")) or "attachment"
        attachments.append(Attachment(name=name, mime=as_str(raw.get("file_type"))))
        extracted = as_str(raw.get("extracted_content"))
        if extracted and extracted.strip():
            parts.append(
                ContentPart(type=PartType.OTHER, text=f"[attachment: {name}]\n{extracted}")
            )
    for raw in _dicts(item.get("files")):
        file_name = as_str(raw.get("file_name"))
        if file_name:
            attachments.append(Attachment(name=file_name))

    if not parts:
        return None
    return Message(
        id=message_id(conv_id, uuid),
        role=role,
        created_at=from_iso(item.get("created_at")),
        content=parts,
        attachments=attachments,
    )


def _blocks(content: Any) -> list[ContentPart]:
    parts: list[ContentPart] = []
    for block in _dicts(content):
        kind = str(block.get("type", ""))
        if kind == "text":
            text = as_str(block.get("text"))
            if text and text.strip():
                parts.append(ContentPart(text=text))
        elif kind == "tool_use":
            name = as_str(block.get("name")) or "tool"
            payload = json.dumps(block.get("input"), ensure_ascii=False, sort_keys=True)
            parts.append(ContentPart(type=PartType.OTHER, text=f"[tool call: {name}]\n{payload}"))
        elif kind == "tool_result":
            text = _tool_result_text(block.get("content"))
            if text:
                parts.append(ContentPart(type=PartType.OTHER, text=f"[tool result]\n{text}"))
        # "thinking" blocks are internal reasoning, not part of the visible conversation.
    return parts


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    texts = [as_str(block.get("text")) for block in _dicts(content)]
    return "\n".join(text for text in texts if text).strip()


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _nearest_kept(
    uuid: str | None, parents: dict[str, str | None], kept: dict[str, Message]
) -> str | None:
    seen: set[str] = set()
    while uuid is not None and uuid != _ROOT_PARENT and uuid not in seen:
        if uuid in kept:
            return uuid
        seen.add(uuid)
        uuid = parents.get(uuid)
    return None


def _sort_key(message: Message, position: int) -> tuple[float, int]:
    created = message.created_at.timestamp() if message.created_at else float("-inf")
    return (created, position)
