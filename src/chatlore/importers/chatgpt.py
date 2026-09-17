"""Importer for the ChatGPT data export.

The export (Settings > Data controls > Export data) is a zip that contains
``conversations.json``: a list of conversations, each holding a ``mapping`` of
nodes that form a tree. Regenerating or editing a message adds a sibling
branch, and ``current_node`` points at the end of the branch shown in the app.

Nodes without a visible message (the synthetic root, hidden system prompts,
internal reasoning records) are dropped, and their children are re-attached to
the nearest kept ancestor so the tree stays connected.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from chatlore.ids import conversation_id, message_id
from chatlore.importers.base import (
    ImporterError,
    IssueSink,
    as_str,
    from_epoch,
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

_ROLES = {
    "user": Role.USER,
    "assistant": Role.ASSISTANT,
    "system": Role.SYSTEM,
    "tool": Role.TOOL,
}

# Internal records that never appear in the conversation as the user saw it.
_HIDDEN_CONTENT_TYPES = frozenset(
    {
        "thoughts",
        "reasoning_recap",
        "user_editable_context",
        "model_editable_context",
        "app_pairing_content",
    }
)


class ChatGPTImporter:
    """Parses ``conversations.json`` from a ChatGPT export zip, folder, or file."""

    kind = SourceKind.CHATGPT

    def parse(self, path: Path, on_issue: IssueSink | None = None) -> Iterator[Conversation]:
        data = load_first_json(path, "conversations.json")
        if not isinstance(data, list):
            raise ImporterError("ChatGPT conversations.json should contain a list")

        for index, raw in enumerate(data):
            label = f"conversation #{index}"
            try:
                if not isinstance(raw, dict) or not isinstance(raw.get("mapping"), dict):
                    raise ValueError("not a ChatGPT conversation (no mapping)")
                label = as_str(raw.get("title")) or label
                yield _conversation(raw)
            except (KeyError, TypeError, ValueError, ValidationError) as error:
                report(on_issue, label, _short(error))


def _conversation(raw: dict[str, Any]) -> Conversation:
    external_id = as_str(raw.get("conversation_id")) or as_str(raw.get("id"))
    if external_id is None:
        raise ValueError("conversation has no id")
    conv_id = conversation_id(SourceKind.CHATGPT, external_id)
    mapping: dict[str, Any] = raw["mapping"]

    kept: dict[str, Message] = {}
    for node_id, node in mapping.items():
        message = _message(conv_id, node_id, node)
        if message is not None:
            kept[node_id] = message

    for node_id, message in kept.items():
        parent = _nearest_kept(_parent_of(mapping, node_id), mapping, kept)
        message.parent_id = None if parent is None else kept[parent].id

    leaf = _nearest_kept(as_str(raw.get("current_node")), mapping, kept)
    messages = sorted(kept.values(), key=_sort_key)
    models = [m.model for m in messages if m.model]

    return Conversation(
        id=conv_id,
        source=SourceKind.CHATGPT,
        external_id=external_id,
        title=as_str(raw.get("title")),
        created_at=from_epoch(raw.get("create_time")),
        updated_at=from_epoch(raw.get("update_time")),
        model=as_str(raw.get("default_model_slug")) or (models[-1] if models else None),
        messages=messages,
        current_leaf_id=None if leaf is None else kept[leaf].id,
        metadata={"is_archived": bool(raw.get("is_archived", False))},
    )


def _message(conv_id: str, node_id: str, node: Any) -> Message | None:
    if not isinstance(node, dict):
        return None
    raw = node.get("message")
    if not isinstance(raw, dict):
        return None

    metadata = _dict(raw.get("metadata"))
    if metadata.get("is_visually_hidden_from_conversation"):
        return None

    author = _dict(raw.get("author"))
    role = _ROLES.get(str(author.get("role")))
    if role is None:
        return None

    content = _dict(raw.get("content"))
    parts = _parts(content)
    if not any(part.text.strip() for part in parts):
        return None

    extra: dict[str, Any] = {}
    if as_str(author.get("name")):
        extra["author_name"] = author["name"]
    if as_str(content.get("content_type")):
        extra["content_type"] = content["content_type"]

    return Message(
        id=message_id(conv_id, node_id),
        role=role,
        created_at=from_epoch(raw.get("create_time")),
        content=parts,
        attachments=_attachments(metadata),
        model=as_str(metadata.get("model_slug")),
        metadata=extra,
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parts(content: dict[str, Any]) -> list[ContentPart]:
    content_type = str(content.get("content_type", "text"))
    if content_type in _HIDDEN_CONTENT_TYPES:
        return []

    if content_type == "code":
        language = as_str(content.get("language"))
        if language == "unknown":
            language = None
        return _one(PartType.CODE, content.get("text"), language)

    if content_type in {"execution_output", "system_error"}:
        return _one(PartType.OTHER, content.get("text"))

    if content_type == "tether_quote":
        lines = [as_str(content.get(key)) for key in ("title", "url", "text")]
        return _one(PartType.OTHER, "\n".join(line for line in lines if line))

    if content_type == "tether_browsing_display":
        return _one(PartType.OTHER, content.get("result") or content.get("summary"))

    raw_parts = content.get("parts")
    if isinstance(raw_parts, list):
        return [part for raw in raw_parts if (part := _part(raw)) is not None]
    return _one(PartType.OTHER, content.get("text"))


def _part(raw: Any) -> ContentPart | None:
    if isinstance(raw, str):
        return ContentPart(text=raw) if raw.strip() else None
    if not isinstance(raw, dict):
        return None
    kind = str(raw.get("content_type", ""))
    if kind == "audio_transcription":
        text = as_str(raw.get("text"))
        return ContentPart(text=text) if text else None
    if kind == "image_asset_pointer":
        return ContentPart(type=PartType.OTHER, text="[image]")
    return ContentPart(type=PartType.OTHER, text=f"[{kind}]") if kind else None


def _one(kind: PartType, text: Any, language: str | None = None) -> list[ContentPart]:
    if not isinstance(text, str) or not text.strip():
        return []
    return [ContentPart(type=kind, text=text, language=language)]


def _attachments(metadata: dict[str, Any]) -> list[Attachment]:
    raw = metadata.get("attachments")
    if not isinstance(raw, list):
        return []
    return [
        Attachment(name=name, mime=as_str(item.get("mime_type")))
        for item in raw
        if isinstance(item, dict) and (name := as_str(item.get("name")))
    ]


def _parent_of(mapping: dict[str, Any], node_id: str) -> str | None:
    node = mapping.get(node_id)
    return as_str(node.get("parent")) if isinstance(node, dict) else None


def _nearest_kept(
    node_id: str | None, mapping: dict[str, Any], kept: dict[str, Message]
) -> str | None:
    """Walk up from ``node_id`` (inclusive) to the first node that was kept."""
    seen: set[str] = set()
    while node_id is not None and node_id not in seen:
        if node_id in kept:
            return node_id
        seen.add(node_id)
        node_id = _parent_of(mapping, node_id)
    return None


def _sort_key(message: Message) -> tuple[float, str]:
    created = message.created_at.timestamp() if message.created_at else float("inf")
    return (created, message.id)


def _short(error: Exception) -> str:
    text = str(error).strip().splitlines()
    return text[0] if text else type(error).__name__
