"""Tests for the index and search commands and the store side of import."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chatlore.cli import app
from chatlore.store import open_store
from tests.fakes import FakeEmbedder

runner = CliRunner()


@pytest.fixture(autouse=True)
def embedder(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    """Replace the real model everywhere in the CLI so tests never download anything."""
    fake = FakeEmbedder()
    monkeypatch.setattr("chatlore.cli.make_embedder", lambda home: fake)
    return fake


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


def _row(output: str, name: str) -> int:
    match = re.search(rf"{name}\s*\D\s*(\d+)", output)
    assert match is not None, output
    return int(match.group(1))


def test_process_chunks_the_library_and_is_idempotent(home: Path, fixtures: Path) -> None:
    runner.invoke(app, ["import", str(fixtures / "chatgpt" / "conversations.json")])

    first = runner.invoke(app, ["process"])
    second = runner.invoke(app, ["process"])

    assert first.exit_code == 0 and second.exit_code == 0
    with open_store(home) as store:
        chunks = store.count_nodes("Chunk")
    assert chunks > 0
    assert _row(first.output, "chunks added") == chunks
    assert _row(second.output, "chunks added") == 0
    assert _row(second.output, "chunks unchanged") == chunks


def test_search_does_not_return_a_message_twice_once_it_is_chunked(
    home: Path, fixtures: Path
) -> None:
    runner.invoke(app, ["import", str(fixtures / "chatgpt" / "conversations.json")])
    before = runner.invoke(app, ["search", "composite index"]).output

    runner.invoke(app, ["process"])

    assert runner.invoke(app, ["search", "composite index"]).output == before


def test_process_embeds_every_chunk_once(
    home: Path, fixtures: Path, embedder: FakeEmbedder
) -> None:
    runner.invoke(app, ["import", str(fixtures / "chatgpt" / "conversations.json")])

    first = runner.invoke(app, ["process"])
    second = runner.invoke(app, ["process"])

    assert first.exit_code == 0 and second.exit_code == 0
    with open_store(home) as store:
        chunks = store.count_nodes("Chunk")
        assert store.count_embeddings() == chunks
    assert _row(first.output, "embedded now") == chunks
    assert _row(second.output, "embedded now") == 0
    assert len(embedder.embedded) == chunks
    assert (home / "cache" / "embeddings.db").exists()


def test_no_embed_skips_the_model(home: Path, fixtures: Path, embedder: FakeEmbedder) -> None:
    runner.invoke(app, ["import", str(fixtures / "claude")])

    result = runner.invoke(app, ["process", "--no-embed"])

    assert result.exit_code == 0
    assert "embedded now" not in result.output
    assert embedder.embedded == []


def test_a_rebuilt_database_is_embedded_from_the_cache(
    home: Path, fixtures: Path, embedder: FakeEmbedder
) -> None:
    runner.invoke(app, ["import", str(fixtures / "claude")])
    runner.invoke(app, ["process"])
    calls = len(embedder.embedded)

    runner.invoke(app, ["index", "--rebuild"])
    result = runner.invoke(app, ["process"])

    assert _row(result.output, "embedded now") == 0
    assert _row(result.output, "embeddings from cache") == calls
    assert len(embedder.embedded) == calls


def test_changing_the_model_needs_reembed(
    home: Path, fixtures: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["import", str(fixtures / "claude")])
    runner.invoke(app, ["process"])
    other = FakeEmbedder("another-model")
    monkeypatch.setattr("chatlore.cli.make_embedder", lambda home: other)

    refused = runner.invoke(app, ["process"])
    forced = runner.invoke(app, ["process", "--reembed"])

    assert refused.exit_code == 1
    assert "--reembed" in refused.output
    assert forced.exit_code == 0
    with open_store(home) as store:
        assert store.get_meta("embedding_model") == "another-model"
        assert store.count_embeddings() == len(other.embedded) > 0


def test_semantic_search_finds_by_shared_meaning(home: Path, fixtures: Path) -> None:
    runner.invoke(app, ["import", str(fixtures / "chatgpt" / "conversations.json")])
    before = runner.invoke(app, ["search", "weekend trip", "--semantic"])
    runner.invoke(app, ["process"])

    found = runner.invoke(app, ["search", "suggest a weekend trip", "--semantic", "--limit", "1"])
    other_source = runner.invoke(app, ["search", "weekend trip", "--semantic", "-s", "claude"])

    assert "Run `chatlore process` first" in before.output
    assert found.exit_code == 0
    assert "Trip ideas" in found.output
    assert "similarity" in found.output
    assert "No matches" in other_source.output
