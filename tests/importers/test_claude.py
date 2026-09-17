"""Tests for the Claude importer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from chatlore.importers import ImporterError, ImportIssue
from chatlore.importers.claude import ClaudeImporter
from chatlore.models import Conversation, PartType, Role, SourceKind


def _parse(path: Path) -> tuple[list[Conversation], list[ImportIssue]]:
    issues: list[ImportIssue] = []
    return list(ClaudeImporter().parse(path, issues.append)), issues


@pytest.fixture
def export(fixtures: Path) -> Path:
    return fixtures / "claude" / "conversations.json"


def test_parses_good_records_and_reports_bad_ones(export: Path) -> None:
    conversations, issues = _parse(export)

    assert [c.title for c in conversations] == ["Rust lifetimes", "Old style export"]
    assert [(i.record, i.reason) for i in issues] == [
        ("conversation #2", "not a Claude conversation (no chat_messages)")
    ]


def test_conversation_fields(export: Path) -> None:
    conversation = _parse(export)[0][0]

    assert conversation.source is SourceKind.CLAUDE
    assert conversation.external_id == "aaaaaaaa-1111-4222-8333-000000000001"
    assert conversation.created_at == datetime(2026, 2, 1, 9, 0, tzinfo=UTC)
    assert conversation.metadata == {"summary": "Explaining a borrow checker error."}


def test_thinking_blocks_are_not_imported(export: Path) -> None:
    conversation = _parse(export)[0][0]

    assert all("internal reasoning" not in m.text for m in conversation.messages)


def test_retries_become_branches_and_the_newest_is_active(export: Path) -> None:
    conversation = _parse(export)[0][0]
    branch = conversation.linear_messages()

    assert len(conversation.messages) == 4
    assert [m.role for m in branch] == [Role.USER, Role.ASSISTANT, Role.USER]
    assert "Retried answer" in branch[1].text
    assert branch[0].parent_id is None

    first_answer = next(m for m in conversation.messages if "long enough" in m.text)
    assert first_answer.parent_id == branch[0].id


def test_attachments_and_tool_blocks_are_kept_as_text(export: Path) -> None:
    branch = _parse(export)[0][0].linear_messages()
    question, retried = branch[0], branch[1]

    assert [a.name for a in question.attachments] == ["main.rs", "screenshot.png"]
    assert question.attachments[0].mime == "text/x-rust"
    assert "[attachment: main.rs]\nfn main()" in question.text

    kinds = [p.type for p in retried.content]
    assert kinds == [PartType.TEXT, PartType.OTHER, PartType.OTHER]
    assert '[tool call: repl]\n{"code": "cargo check"}' in retried.text
    assert "[tool result]\nFinished dev profile" in retried.text


def test_old_exports_without_parents_are_linear(export: Path) -> None:
    conversation = _parse(export)[0][1]

    assert conversation.current_leaf_id is None
    assert [m.text for m in conversation.linear_messages()] == [
        "Give me a name for a cat.",
        "Biscuit.",
    ]
    assert conversation.messages[1].parent_id == conversation.messages[0].id


def test_rejects_unusable_exports(tmp_path: Path) -> None:
    path = tmp_path / "conversations.json"
    path.write_text('{"oops": true}', encoding="utf-8")

    with pytest.raises(ImporterError, match="should contain a list"):
        _parse(path)
