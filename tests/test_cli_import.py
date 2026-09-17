"""Tests for the import, note, and stats commands."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from chatlore.cli import app
from chatlore.library import Library
from chatlore.models import SourceKind

runner = CliRunner()


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "home"
    monkeypatch.setenv("CHATLORE_HOME", str(target))
    monkeypatch.setenv("COLUMNS", "120")
    return target


def test_import_detects_the_source_and_fills_the_library(home: Path, fixtures: Path) -> None:
    result = runner.invoke(app, ["import", str(fixtures / "chatgpt" / "conversations.json")])

    assert result.exit_code == 0
    assert "chatgpt import" in result.output
    assert "skipped" in result.output
    assert "No id: conversation has no id" in result.output
    assert {c.title for c in Library(home)} == {"Postgres indexing", "Trip ideas"}


def test_importing_twice_changes_nothing(home: Path, fixtures: Path) -> None:
    path = str(fixtures / "claude" / "conversations.json")
    runner.invoke(app, ["import", path])
    files = {p: p.read_text(encoding="utf-8") for p in home.rglob("*.json")}

    result = runner.invoke(app, ["import", path, "--source", "claude"])

    assert result.exit_code == 0
    assert {p: p.read_text(encoding="utf-8") for p in home.rglob("*.json")} == files
    assert Library(home).stats()["claude"].conversations == 2


def test_dry_run_writes_nothing(home: Path, fixtures: Path) -> None:
    result = runner.invoke(app, ["import", str(fixtures / "gemini"), "--dry-run"])

    assert result.exit_code == 0
    assert "dry run" in result.output
    assert not home.exists()


def test_import_reports_failures_with_exit_code_one(home: Path, tmp_path: Path) -> None:
    missing = runner.invoke(app, ["import", str(tmp_path / "missing.zip")])
    unknown = runner.invoke(app, ["import", str(tmp_path), "--source", "slack"])

    assert missing.exit_code == 1
    assert "Import failed" in missing.output
    assert unknown.exit_code == 1
    assert "unknown source" in unknown.output


def test_note_and_stats(home: Path, fixtures: Path) -> None:
    empty = runner.invoke(app, ["stats"])
    runner.invoke(app, ["import", str(fixtures / "markdown" / "vault")])
    saved = runner.invoke(app, ["note", "Ship milestone two", "--title", "Reminder"])
    rejected = runner.invoke(app, ["note", "   "])
    stats = runner.invoke(app, ["stats"])

    assert "library is empty" in empty.output
    assert saved.exit_code == 0 and "Reminder" in saved.output
    assert rejected.exit_code == 1
    totals = Library(home).stats()
    assert totals[SourceKind.MARKDOWN.value].conversations == 4
    assert totals[SourceKind.NOTE.value].conversations == 1
    assert "markdown" in stats.output and "total" in stats.output
