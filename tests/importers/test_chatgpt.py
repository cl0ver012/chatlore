"""Tests for the ChatGPT importer."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from chatlore.importers import ImporterError, ImportIssue
from chatlore.importers.chatgpt import ChatGPTImporter
from chatlore.models import Conversation, PartType, Role, SourceKind
from tests.conftest import make_zip


def _parse(path: Path) -> tuple[list[Conversation], list[ImportIssue]]:
    issues: list[ImportIssue] = []
    return list(ChatGPTImporter().parse(path, issues.append)), issues


@pytest.fixture
def export(fixtures: Path) -> Path:
    return fixtures / "chatgpt" / "conversations.json"


def test_parses_good_records_and_reports_bad_ones(export: Path) -> None:
    conversations, issues = _parse(export)

    assert [c.title for c in conversations] == ["Postgres indexing", "Trip ideas"]
    assert [(i.record, i.reason) for i in issues] == [
        ("conversation #2", "not a ChatGPT conversation (no mapping)"),
        ("No id", "conversation has no id"),
    ]


def test_conversation_fields(export: Path) -> None:
    conversation = _parse(export)[0][0]

    assert conversation.source is SourceKind.CHATGPT
    assert conversation.external_id == "11111111-aaaa-4bbb-8ccc-000000000001"
    assert conversation.model == "gpt-5"
    assert conversation.created_at == datetime.fromtimestamp(1767261600.0, tz=UTC)
    assert conversation.metadata == {"is_archived": False}


def test_hidden_and_internal_nodes_are_dropped(export: Path) -> None:
    conversation = _parse(export)[0][0]
    texts = [m.text for m in conversation.messages]

    assert len(conversation.messages) == 7
    assert not any("internal" in text or text == "" for text in texts)


def test_active_branch_skips_the_regenerated_answer(export: Path) -> None:
    conversation = _parse(export)[0][0]
    branch = conversation.linear_messages()

    assert [m.role for m in branch] == [
        Role.USER,
        Role.ASSISTANT,
        Role.USER,
        Role.ASSISTANT,
        Role.TOOL,
        Role.ASSISTANT,
    ]
    assert branch[0].parent_id is None
    assert "composite index" in branch[1].text
    assert all("regenerated" not in m.text for m in branch)
    assert any("regenerated" in m.text for m in conversation.messages)


def test_children_of_dropped_nodes_are_reattached(export: Path) -> None:
    branch = _parse(export)[0][0].linear_messages()
    user_with_image, code = branch[2], branch[3]

    assert code.parent_id == user_with_image.id


def test_content_types_map_to_parts(export: Path) -> None:
    branch = _parse(export)[0][0].linear_messages()
    first_user, user_with_image, code, tool = branch[0], branch[2], branch[3], branch[4]

    assert [a.name for a in first_user.attachments] == ["explain.txt"]
    assert [(p.type, p.text) for p in user_with_image.content] == [
        (PartType.OTHER, "[image]"),
        (PartType.TEXT, "Here is the plan output. Can you write the statement?"),
    ]
    assert code.content[0].type is PartType.CODE
    assert code.content[0].language == "sql"
    assert tool.metadata["author_name"] == "python"
    assert tool.content[0].type is PartType.OTHER


def test_id_field_and_model_fallback(export: Path) -> None:
    conversation = _parse(export)[0][1]

    assert conversation.external_id == "22222222-aaaa-4bbb-8ccc-000000000002"
    assert conversation.model == "gpt-5-mini"
    assert len(conversation.linear_messages()) == 2


def test_parsing_is_deterministic(export: Path) -> None:
    first, _ = _parse(export)
    second, _ = _parse(export)

    assert [c.id for c in first] == [c.id for c in second]
    assert [c.content_hash() for c in first] == [c.content_hash() for c in second]


def test_reads_from_a_zip_and_a_folder(export: Path, tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "export.zip", {"chatgpt-export/conversations.json": export})
    folder = tmp_path / "extracted" / "nested"
    folder.mkdir(parents=True)
    (folder / "conversations.json").write_bytes(export.read_bytes())

    assert len(_parse(archive)[0]) == 2
    assert len(_parse(tmp_path / "extracted")[0]) == 2


def test_rejects_unusable_exports(tmp_path: Path) -> None:
    not_a_list = tmp_path / "conversations.json"
    not_a_list.write_text('{"oops": true}', encoding="utf-8")
    not_json = tmp_path / "bad.json"
    not_json.write_text("{nope", encoding="utf-8")

    with pytest.raises(ImporterError, match="should contain a list"):
        _parse(not_a_list)
    with pytest.raises(ImporterError, match="not valid JSON"):
        _parse(not_json)
    with pytest.raises(ImporterError, match="does not exist"):
        _parse(tmp_path / "missing.zip")
    with pytest.raises(ImporterError, match=r"no conversations\.json"):
        _parse(make_zip(tmp_path / "empty.zip", {"readme.txt": "hello"}))
