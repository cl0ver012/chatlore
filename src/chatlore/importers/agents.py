"""Sessions of coding agents that keep them on disk: Claude Code and the Codex CLI.

Both write one JSON Lines file per session: Claude Code under
``~/.claude/projects/<project>/``, the Codex CLI under ``~/.codex/sessions/``.
Each session becomes one conversation. What was said is kept as text, so it is
chunked, embedded, and read into the graph like any chat. Tool calls and their
results are kept too, shortened, as parts that are searchable by words but not
chunked, since they are mostly machine output. Hidden reasoning, subagent
side chains, and the context the tools inject are left out.

The files are recognised by their records (``session_kind``), wherever they
sit, so a folder of sessions or an archive of one imports like any export.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from chatlore.ids import conversation_id, message_id
from chatlore.importers.base import (
    ImporterError,
    IssueSink,
    as_str,
    from_iso,
    report,
)
from chatlore.models import ContentPart, Conversation, Message, PartType, Role, SourceKind

TOOL_TEXT_LIMIT = 2_000
"""Characters kept of each tool call and result; the rest is mostly file contents and logs."""
TITLE_LENGTH = 80
_SNIFFED_LINES = 30

_CLAUDE_CODE_TYPES = frozenset({"user", "assistant", "summary", "system", "custom-title"})
_CODEX_TYPES = frozenset({"session_meta", "response_item", "event_msg", "turn_context"})
_CODEX_ITEMS = frozenset(
    {"message", "function_call", "function_call_output", "custom_tool_call",
     "custom_tool_call_output", "local_shell_call", "reasoning"}
)  # fmt: skip
_COMMAND_OUTPUT = ("<command-", "<local-command-")
"""How Claude Code records slash commands and their output in the user's turn."""
_INJECTED = ("<environment_context>", "<user_instructions>", "# AGENTS.md instructions")
"""Context the Codex CLI sends as the user's turn, which the user never typed."""


def session_kind(path: Path) -> SourceKind | None:
    """Which agent wrote the JSON Lines file at ``path``, or None if neither did."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            lines = [line for _, line in zip(range(_SNIFFED_LINES), handle, strict=False)]
    except OSError:
        return None
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        if kind in _CLAUDE_CODE_TYPES and ("sessionId" in record or "leafUuid" in record):
            return SourceKind.CLAUDE_CODE
        if kind in _CODEX_TYPES and isinstance(record.get("payload"), dict):
            return SourceKind.CODEX
        if index == 0 and kind is None and "id" in record and "instructions" in record:
            return SourceKind.CODEX  # the first Codex CLI versions wrote bare records
    return None


def _session_files(path: Path, kind: SourceKind) -> list[Path]:
    if not path.exists():
        raise ImporterError(f"{path} does not exist")
    if path.is_file():
        return [path]
    return [file for file in sorted(path.rglob("*.jsonl")) if session_kind(file) is kind]


def _records(path: Path, on_issue: IssueSink | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                report(on_issue, f"{path.name}:{number}", "not valid JSON")
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def _shortened(text: str) -> str:
    text = text.strip()
    if len(text) <= TOOL_TEXT_LIMIT:
        return text
    return f"{text[:TOOL_TEXT_LIMIT].rstrip()}\n[{len(text) - TOOL_TEXT_LIMIT:,} more characters]"


def _tool_call(name: str, arguments: Any) -> ContentPart:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
    return ContentPart(type=PartType.OTHER, text=f"[tool call: {name}]\n{_shortened(arguments)}")


def _tool_result(text: str, error: bool = False) -> ContentPart:
    label = "tool error" if error else "tool result"
    return ContentPart(type=PartType.OTHER, text=f"[{label}]\n{_shortened(text)}")


def _texts(content: Any) -> list[str]:
    """The text of a block list, or of a plain string."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        text for block in content if isinstance(block, dict) and (text := as_str(block.get("text")))
    ]


@dataclass(slots=True)
class _Draft:
    role: Role
    parts: list[ContentPart]
    parent: str | None
    created_at: datetime | None
    model: str | None = None
    api_id: str | None = None


@dataclass(slots=True)
class _Session:
    """What one session file holds, before it becomes a conversation."""

    drafts: dict[str, _Draft] = field(default_factory=dict)
    leaf: str | None = None
    title: str | None = None

    def conversation(
        self, source: SourceKind, external_id: str, metadata: dict[str, Any]
    ) -> Conversation | None:
        if not self.drafts:
            return None
        conv_id = conversation_id(source, external_id)
        messages = [
            Message(
                id=message_id(conv_id, key),
                parent_id=message_id(conv_id, draft.parent) if draft.parent else None,
                role=draft.role,
                created_at=draft.created_at,
                content=draft.parts,
                model=draft.model,
            )
            for key, draft in self.drafts.items()
        ]
        stamps = [message.created_at for message in messages if message.created_at]
        first = next(
            (part.text for d in self.drafts.values() if d.role is Role.USER for part in d.parts),
            None,
        )
        title = self.title or (first.strip().splitlines()[0][:TITLE_LENGTH] if first else None)
        return Conversation(
            id=conv_id,
            source=source,
            external_id=external_id,
            title=title,
            created_at=min(stamps) if stamps else None,
            updated_at=max(stamps) if stamps else None,
            model=next((m.model for m in reversed(messages) if m.model), None),
            messages=messages,
            current_leaf_id=message_id(conv_id, self.leaf) if self.leaf else None,
            metadata={key: value for key, value in metadata.items() if value},
        )


class _SessionImporter:
    kind: SourceKind

    def parse(self, path: Path, on_issue: IssueSink | None = None) -> Iterator[Conversation]:
        for file in _session_files(path, self.kind):
            records = _records(file, on_issue)
            try:
                conversation = self._conversation(file, records)
            except ValueError as error:  # an inconsistent file, such as a broken message tree
                report(on_issue, file.name, f"could not be read: {error}")
                continue
            if conversation is None:
                report(on_issue, file.name, "no messages")
                continue
            yield conversation

    def _conversation(self, path: Path, records: list[dict[str, Any]]) -> Conversation | None:
        raise NotImplementedError


class ClaudeCodeImporter(_SessionImporter):
    """Claude Code sessions: the JSON Lines files under ``~/.claude/projects``."""

    kind = SourceKind.CLAUDE_CODE

    def _conversation(self, path: Path, records: list[dict[str, Any]]) -> Conversation | None:
        session = _Session()
        parents: dict[str, str | None] = {}
        alias: dict[str, str] = {}
        summary: str | None = None
        external_id = path.stem
        metadata: dict[str, Any] = {}

        def kept_ancestor(uuid: str | None) -> str | None:
            seen: set[str] = set()
            while uuid is not None and uuid not in seen:
                if uuid in alias:
                    return alias[uuid]
                seen.add(uuid)
                uuid = parents.get(uuid)
            return None

        for record in records:
            kind = record.get("type")
            external_id = as_str(record.get("sessionId")) or external_id
            if kind == "custom-title":
                session.title = as_str(record.get("customTitle")) or session.title
                continue
            if kind == "summary":
                summary = summary or as_str(record.get("summary"))
                continue
            uuid = as_str(record.get("uuid"))
            if uuid is None:
                continue
            # After a compaction the chain restarts; its logical parent keeps it whole.
            parents[uuid] = as_str(record.get("parentUuid")) or as_str(
                record.get("logicalParentUuid")
            )
            message = record.get("message")
            if (
                kind not in {"user", "assistant"}
                or record.get("isSidechain")
                or record.get("isMeta")
                or record.get("isCompactSummary")
                or not isinstance(message, dict)
            ):
                continue
            metadata.setdefault("project", as_str(record.get("cwd")))
            metadata.setdefault("git_branch", as_str(record.get("gitBranch")))
            role, parts = _claude_code_parts(kind, message.get("content"))
            if not parts:
                continue
            parent = kept_ancestor(parents[uuid])
            api_id = as_str(message.get("id"))
            previous = session.drafts.get(parent) if parent is not None else None
            if (
                parent is not None
                and previous is not None
                and role is Role.ASSISTANT
                and previous.role is Role.ASSISTANT
                and api_id is not None
                and previous.api_id == api_id
            ):
                # One answer is written as one record per block; they make one message.
                previous.parts.extend(parts)
                alias[uuid] = parent
            else:
                session.drafts[uuid] = _Draft(
                    role,
                    parts,
                    parent,
                    from_iso(record.get("timestamp")),
                    as_str(message.get("model")),
                    api_id,
                )
                alias[uuid] = uuid
            session.leaf = alias[uuid]
        session.title = session.title or summary
        return session.conversation(self.kind, external_id, metadata)


def _claude_code_parts(kind: str, content: Any) -> tuple[Role, list[ContentPart]]:
    if kind == "assistant":
        parts: list[ContentPart] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and (text := as_str(block.get("text"))):
                if text.strip():
                    parts.append(ContentPart(text=text))
            elif block.get("type") == "tool_use":
                name = as_str(block.get("name")) or "tool"
                parts.append(_tool_call(name, block.get("input")))
            # Skipped on purpose: "thinking" is internal reasoning.
        if isinstance(content, str) and content.strip():
            parts.append(ContentPart(text=content))
        return Role.ASSISTANT, parts

    if isinstance(content, str):
        if content.lstrip().startswith(_COMMAND_OUTPUT) or not content.strip():
            return Role.USER, []
        return Role.USER, [ContentPart(text=content)]
    blocks = (
        [item for item in content if isinstance(item, dict)] if isinstance(content, list) else []
    )
    if any(block.get("type") == "tool_result" for block in blocks):
        return Role.TOOL, [
            _tool_result(text, bool(block.get("is_error")))
            for block in blocks
            if block.get("type") == "tool_result"
            and (text := "\n".join(_texts(block.get("content"))).strip())
        ]
    parts = [
        ContentPart(text=text)
        for block in blocks
        if block.get("type") == "text"
        and (text := as_str(block.get("text")))
        and text.strip()
        and not text.lstrip().startswith(_COMMAND_OUTPUT)
    ]
    if any(block.get("type") == "image" for block in blocks):
        parts.append(ContentPart(type=PartType.OTHER, text="[image]"))
    return Role.USER, parts


class CodexImporter(_SessionImporter):
    """Codex CLI sessions: the JSON Lines files under ``~/.codex/sessions``."""

    kind = SourceKind.CODEX

    def _conversation(self, path: Path, records: list[dict[str, Any]]) -> Conversation | None:
        session = _Session()
        external_id = path.stem
        metadata: dict[str, Any] = {}
        for index, record in enumerate(records):
            kind = record.get("type")
            payload = record.get("payload")
            if kind == "session_meta" and isinstance(payload, dict):
                external_id = as_str(payload.get("id")) or external_id
                metadata["project"] = as_str(payload.get("cwd"))
                continue
            if index == 0 and kind is None and "instructions" in record:
                external_id = as_str(record.get("id")) or external_id
                continue
            if kind == "response_item" and isinstance(payload, dict):
                item = payload
            elif kind in _CODEX_ITEMS:
                item = record  # the first Codex CLI versions wrote bare records
            else:
                continue
            found = _codex_parts(item)
            if found is None:
                continue
            role, parts = found
            stamp = from_iso(record.get("timestamp"))
            last = session.drafts.get(session.leaf) if session.leaf else None
            if last is not None and last.role is role:
                last.parts.extend(parts)
                continue
            key = str(len(session.drafts))
            session.drafts[key] = _Draft(role, parts, session.leaf, stamp)
            session.leaf = key
        return session.conversation(self.kind, external_id, metadata)


def _codex_parts(item: dict[str, Any]) -> tuple[Role, list[ContentPart]] | None:
    kind = item.get("type")
    if kind == "message":
        role = {"user": Role.USER, "assistant": Role.ASSISTANT}.get(str(item.get("role")))
        if role is None:
            return None  # system and developer turns are instructions, not conversation
        texts = [
            text
            for text in _texts(item.get("content"))
            if text.strip() and not text.lstrip().startswith(_INJECTED)
        ]
        return (role, [ContentPart(text=text) for text in texts]) if texts else None
    if kind in {"function_call", "custom_tool_call"}:
        name = as_str(item.get("name")) or "tool"
        return Role.ASSISTANT, [_tool_call(name, item.get("arguments", item.get("input", "")))]
    if kind == "local_shell_call":
        return Role.ASSISTANT, [_tool_call("shell", item.get("action"))]
    if kind in {"function_call_output", "custom_tool_call_output"}:
        text = _codex_output(item.get("output"))
        return (Role.TOOL, [_tool_result(text)]) if text.strip() else None
    return None  # reasoning is hidden, and the rest is bookkeeping


def _codex_output(output: Any) -> str:
    """A tool's output: a string, a string of JSON holding an ``output`` field, or items."""
    if isinstance(output, str):
        try:
            decoded = json.loads(output)
        except json.JSONDecodeError:
            return output
        if isinstance(decoded, dict) and isinstance(decoded.get("output"), str):
            return str(decoded["output"])
        return output
    if isinstance(output, dict):
        return _codex_output(output.get("content") or output.get("output") or "")
    return "\n".join(_texts(output))
