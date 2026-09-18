"""Tests for the index and search commands and the store side of import."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from chatlore.cli import app
from chatlore.store import open_store

runner = CliRunner()


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "home"
    monkeypatch.setenv("CHATLORE_HOME", str(target))
    monkeypatch.setenv("COLUMNS", "120")
    return target


def test_import_fills_the_store_and_search_finds_it(home: Path, fixtures: Path) -> None:
    imported = runner.invoke(app, ["import", str(fixtures / "chatgpt" / "conversations.json")])
    found = runner.invoke(app, ["search", "composite index"])
    filtered = runner.invoke(app, ["search", "composite index", "--source", "claude"])

    assert imported.exit_code == 0
    assert found.exit_code == 0
    assert "Postgres indexing" in found.output
    assert "[composite]" in found.output
    assert "No matches" in filtered.output
    with open_store(home) as store:
        assert store.count_nodes("Conversation") == 2


def test_dry_run_does_not_create_the_database(home: Path, fixtures: Path) -> None:
    runner.invoke(app, ["import", str(fixtures / "claude"), "--dry-run"])

    assert not (home / "chatlore.db").exists()


def test_index_rebuilds_from_the_library(home: Path, fixtures: Path) -> None:
    runner.invoke(app, ["import", str(fixtures / "claude")])
    runner.invoke(app, ["import", str(fixtures / "gemini")])
    (home / "chatlore.db").unlink()

    result = runner.invoke(app, ["index", "--rebuild"])

    assert result.exit_code == 0
    assert "Indexed 6 conversations" in result.output
    assert "[cat]" in runner.invoke(app, ["search", "cat name"]).output


def test_note_is_searchable_immediately(home: Path) -> None:
    runner.invoke(app, ["note", "Ask Sam about the staging database"])

    result = runner.invoke(app, ["search", "staging"])

    assert "Ask Sam" in result.output


def test_search_on_an_empty_home(home: Path) -> None:
    result = runner.invoke(app, ["search", "anything"])

    assert result.exit_code == 0
    assert "No matches" in result.output
