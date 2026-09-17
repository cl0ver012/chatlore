"""Tests for importer lookup and source detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from chatlore.importers import IMPORTERS, ImporterError, detect_source, get_importer
from chatlore.models import SourceKind
from tests.conftest import make_zip


def test_every_importer_is_registered_under_its_own_kind() -> None:
    assert {kind: importer.kind for kind, importer in IMPORTERS.items()} == {
        SourceKind.CHATGPT: SourceKind.CHATGPT,
        SourceKind.CLAUDE: SourceKind.CLAUDE,
        SourceKind.GEMINI: SourceKind.GEMINI,
        SourceKind.MARKDOWN: SourceKind.MARKDOWN,
    }


def test_get_importer_rejects_unknown_sources() -> None:
    assert get_importer("claude").kind is SourceKind.CLAUDE
    with pytest.raises(ImporterError, match="unknown source 'slack'"):
        get_importer("slack")
    with pytest.raises(ImporterError, match="unknown source 'note'"):
        get_importer("note")


def test_detects_each_fixture(fixtures: Path) -> None:
    assert detect_source(fixtures / "chatgpt" / "conversations.json") is SourceKind.CHATGPT
    assert detect_source(fixtures / "chatgpt") is SourceKind.CHATGPT
    assert detect_source(fixtures / "claude" / "conversations.json") is SourceKind.CLAUDE
    assert detect_source(fixtures / "gemini") is SourceKind.GEMINI
    assert detect_source(fixtures / "markdown" / "vault") is SourceKind.MARKDOWN
    assert detect_source(fixtures / "markdown" / "vault" / "ideas.md") is SourceKind.MARKDOWN


def test_detects_inside_zips_and_renamed_files(fixtures: Path, tmp_path: Path) -> None:
    claude_zip = make_zip(
        tmp_path / "claude.zip",
        {"conversations.json": fixtures / "claude" / "conversations.json", "users.json": "[]"},
    )
    renamed = tmp_path / "my-gemini-history.json"
    renamed.write_bytes((fixtures / "gemini" / "MyActivity.json").read_bytes())

    assert detect_source(claude_zip) is SourceKind.CLAUDE
    assert detect_source(renamed) is SourceKind.GEMINI


def test_detection_fails_clearly(tmp_path: Path) -> None:
    unknown = tmp_path / "data.json"
    unknown.write_text('[{"something": "else"}]', encoding="utf-8")
    binary = tmp_path / "photo.png"
    binary.write_bytes(b"\x89PNG")

    with pytest.raises(ImporterError, match="does not exist"):
        detect_source(tmp_path / "missing")
    with pytest.raises(ImporterError, match="could not recognise"):
        detect_source(unknown)
    with pytest.raises(ImporterError, match="could not recognise"):
        detect_source(binary)
