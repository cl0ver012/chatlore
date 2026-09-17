"""Tests for the Markdown importer and manual notes."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chatlore.importers import ImporterError, ImportIssue, make_note
from chatlore.importers.markdown import MarkdownImporter
from chatlore.models import Conversation, SourceKind


def _parse(path: Path) -> tuple[list[Conversation], list[ImportIssue]]:
    issues: list[ImportIssue] = []
    return list(MarkdownImporter().parse(path, issues.append)), issues


@pytest.fixture
def vault(fixtures: Path) -> Path:
    return fixtures / "markdown" / "vault"


def test_imports_every_text_file_and_skips_editor_folders(vault: Path) -> None:
    conversations, issues = _parse(vault)

    assert sorted(c.external_id for c in conversations) == [
        "broken-frontmatter.md",
        "ideas.md",
        "plain.txt",
        "projects/chatlore.md",
    ]
    assert issues == []


def test_frontmatter_drives_title_dates_and_metadata(vault: Path) -> None:
    note = next(c for c in _parse(vault)[0] if c.external_id == "projects/chatlore.md")

    assert note.source is SourceKind.MARKDOWN
    assert note.title == "ChatLore notes"
    assert note.created_at == datetime(2026, 1, 15, tzinfo=UTC)
    assert note.updated_at == datetime(2026, 2, 1, 10, 30, tzinfo=UTC)
    assert note.metadata["frontmatter"]["tags"] == ["project", "knowledge-graph"]
    assert note.metadata["wikilinks"] == ["Graph stores", "ideas"]
    assert note.messages[0].text.startswith("# A heading that is not the title")
    assert "title: ChatLore notes" not in note.messages[0].text


def test_title_falls_back_to_heading_then_file_name(vault: Path) -> None:
    titles = {c.external_id: c.title for c in _parse(vault)[0]}

    assert titles["ideas.md"] == "Ideas for later"
    assert titles["plain.txt"] == "plain"
    assert titles["broken-frontmatter.md"] == "broken-frontmatter"


def test_invalid_frontmatter_is_kept_as_body(vault: Path) -> None:
    note = next(c for c in _parse(vault)[0] if c.external_id == "broken-frontmatter.md")

    assert "title: [unclosed" in note.messages[0].text
    assert "frontmatter" not in note.metadata


def test_ids_survive_moving_the_vault(vault: Path, tmp_path: Path) -> None:
    copy = tmp_path / "elsewhere"
    shutil.copytree(vault, copy)

    original = {c.id: c.content_hash() for c in _parse(vault)[0]}
    moved = {c.id: c.content_hash() for c in _parse(copy)[0]}

    assert original == moved


def test_single_file_and_empty_file(tmp_path: Path) -> None:
    (tmp_path / "one.md").write_text("# One\n\nbody", encoding="utf-8")
    (tmp_path / "empty.md").write_text("   \n", encoding="utf-8")

    single, _ = _parse(tmp_path / "one.md")
    _, issues = _parse(tmp_path)

    assert [c.external_id for c in single] == ["one.md"]
    assert [(i.record, i.reason) for i in issues] == [("empty.md", "file is empty")]


def test_rejects_missing_or_empty_folders(tmp_path: Path) -> None:
    with pytest.raises(ImporterError, match="does not exist"):
        _parse(tmp_path / "missing")
    with pytest.raises(ImporterError, match="no Markdown or text files"):
        _parse(tmp_path)


def test_make_note() -> None:
    created = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    note = make_note("  Remember the milk.\nAnd the bread.  ", created_at=created)
    same = make_note("Remember the milk.\nAnd the bread.", created_at=created)
    long = make_note("x" * 200)

    assert note.source is SourceKind.NOTE
    assert note.title == "Remember the milk."
    assert note.messages[0].text == "Remember the milk.\nAnd the bread."
    assert note.id == same.id
    assert long.title is not None and len(long.title) == 80
    assert make_note("body", title="Custom").title == "Custom"
    with pytest.raises(ValueError, match="cannot be empty"):
        make_note("   ")
