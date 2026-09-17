"""Tests for the Gemini Takeout importer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chatlore.importers import ImporterError, ImportIssue
from chatlore.importers.gemini import GeminiImporter, html_to_text
from chatlore.models import Conversation, Role, SourceKind
from tests.conftest import make_zip


def _parse(path: Path) -> tuple[list[Conversation], list[ImportIssue]]:
    issues: list[ImportIssue] = []
    return list(GeminiImporter().parse(path, issues.append)), issues


@pytest.fixture
def export(fixtures: Path) -> Path:
    return fixtures / "gemini" / "MyActivity.json"


def test_html_to_text_keeps_structure_and_drops_scripts() -> None:
    html = (
        "<p>Here is a simple recipe:</p><ul><li>500 g flour</li><li>325 g water</li></ul>"
        "<script>ignored()</script><p>Mix &amp; rest.</p>"
    )

    assert html_to_text(html) == (
        "Here is a simple recipe:\n\n- 500 g flour\n- 325 g water\n\nMix & rest."
    )


def test_prompts_are_grouped_into_sessions_in_time_order(export: Path) -> None:
    conversations, _ = _parse(export)

    assert [c.title for c in conversations] == [
        "Give me a recipe for Neapolitan pizza dough",
        "What is the capital of Australia?",
        "Eingegebener Prompt: Wie spät ist es in Tokio?",
    ]
    assert [len(c.messages) for c in conversations] == [4, 3, 2]


def test_session_messages_alternate_and_chain(export: Path) -> None:
    conversation = _parse(export)[0][0]
    messages = conversation.linear_messages()

    assert conversation.source is SourceKind.GEMINI
    assert conversation.created_at == datetime(2026, 3, 10, 18, 0, tzinfo=UTC)
    assert conversation.updated_at == datetime(2026, 3, 10, 18, 12, tzinfo=UTC)
    assert [m.role for m in messages] == [Role.USER, Role.ASSISTANT, Role.USER, Role.ASSISTANT]
    assert messages[0].parent_id is None
    assert [m.parent_id for m in messages[1:]] == [m.id for m in messages[:-1]]
    assert messages[3].text == "At least 24 hours in the fridge."
    assert conversation.metadata == {"grouping": "session", "session_gap_minutes": 30}


def test_prompt_without_a_stored_response_is_kept(export: Path) -> None:
    conversation = _parse(export)[0][1]

    assert [m.role for m in conversation.messages] == [Role.USER, Role.ASSISTANT, Role.USER]
    assert conversation.messages[2].text == "A question that got no stored response"


def test_only_unusable_prompts_are_reported(export: Path) -> None:
    _, issues = _parse(export)

    assert [(i.record, i.reason) for i in issues] == [("activity #6", "entry has no usable time")]


def test_picks_the_gemini_activity_out_of_a_takeout_zip(export: Path, tmp_path: Path) -> None:
    search = json.dumps(
        [{"header": "Search", "title": "Searched for x", "time": "2026-01-01T00:00:00Z"}]
    )
    archive = make_zip(
        tmp_path / "takeout.zip",
        {
            "Takeout/My Activity/A Search/MyActivity.json": search,
            "Takeout/My Activity/Gemini Apps/MyActivity.json": export,
        },
    )

    assert len(_parse(archive)[0]) == 3


def test_rejects_unusable_exports(tmp_path: Path) -> None:
    html = tmp_path / "MyActivity.html"
    html.write_text("<html></html>", encoding="utf-8")
    other = tmp_path / "other" / "MyActivity.json"
    other.parent.mkdir()
    other.write_text(
        '[{"header": "Search", "title": "x", "time": "2026-01-01T00:00:00Z"}]', "utf-8"
    )
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(ImporterError, match="choose JSON"):
        _parse(html)
    with pytest.raises(ImporterError, match="no Gemini Apps activity"):
        _parse(other.parent)
    with pytest.raises(ImporterError, match=r"no MyActivity\.json"):
        _parse(empty)
