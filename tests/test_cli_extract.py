"""Tests for the extract command."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chatlore.cli import app
from chatlore.store import Label, open_store
from tests.fakes import FakeEmbedder, FakeLLM

runner = CliRunner()


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixtures: Path) -> Path:
    target = tmp_path / "home"
    monkeypatch.setenv("CHATLORE_HOME", str(target))
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr("chatlore.cli.make_embedder", lambda home: FakeEmbedder())
    runner.invoke(app, ["import", str(fixtures / "chatgpt" / "conversations.json")])
    return target


def _use(monkeypatch: pytest.MonkeyPatch, llm: FakeLLM) -> FakeLLM:
    monkeypatch.setattr("chatlore.cli.make_llm", lambda: llm)
    return llm


def _row(output: str, name: str) -> int:
    match = re.search(rf"{name}\s*\D\s*(\d+)", output)
    assert match is not None, output
    return int(match.group(1))


def test_extract_needs_chunks_first(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _use(monkeypatch, FakeLLM())

    result = runner.invoke(app, ["extract"])

    assert result.exit_code == 0
    assert "Run `chatlore process` first" in result.output
    assert llm.requests == []


def test_extract_explains_a_missing_key(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CHATLORE_LLM_BASE_URL", "CHATLORE_LLM_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    runner.invoke(app, ["process", "--no-embed"])

    result = runner.invoke(app, ["extract"])

    assert result.exit_code == 1
    assert "OPENROUTER_API_KEY" in result.output


def test_extract_builds_the_graph_and_is_idempotent(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["note", "Postgres needs an index on orders."])
    runner.invoke(app, ["note", "Postgres was slow before the index."])
    runner.invoke(app, ["process", "--no-embed"])
    llm = _use(monkeypatch, FakeLLM())

    first = runner.invoke(app, ["extract", "--batch-size", "2"])
    requests = (len(llm.requests), len(llm.summary_requests))
    second = runner.invoke(app, ["extract"])

    assert first.exit_code == 0 and second.exit_code == 0
    with open_store(home) as store:
        chunks = store.count_nodes(Label.CHUNK)
        entities = store.count_nodes(Label.ENTITY)
        summaries = [node.props["summary"] for node in store.find_nodes(Label.ENTITY)]
    assert None not in summaries
    assert _row(first.output, "entities summarised now") == len(llm.summary_requests[0]) > 0
    assert _row(second.output, "entities summarised now") == 0
    assert _row(first.output, "chunks read now") == chunks
    assert _row(second.output, "chunks read now") == 0
    assert _row(second.output, "chunks read before") == chunks
    assert _row(second.output, "entities") == entities > 0
    assert (len(llm.requests), len(llm.summary_requests)) == requests
    assert (home / "cache" / "extractions.db").exists()


def test_extract_limit_reads_a_few_chunks(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner.invoke(app, ["process", "--no-embed"])
    _use(monkeypatch, FakeLLM())

    result = runner.invoke(app, ["extract", "--limit", "1"])

    assert result.exit_code == 0
    assert _row(result.output, "chunks read now") == 1


def test_an_unreachable_model_keeps_what_was_read(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner.invoke(app, ["process", "--no-embed"])
    _use(monkeypatch, FakeLLM(fail_on_request=2))

    failed = runner.invoke(app, ["extract", "--batch-size", "1", "--workers", "1"])
    _use(monkeypatch, FakeLLM())
    resumed = runner.invoke(app, ["extract"])

    assert failed.exit_code == 1
    assert "fake model is unreachable" in failed.output
    assert "run `chatlore extract` again" in failed.output
    assert _row(failed.output, "entities") > 0
    assert resumed.exit_code == 0
    assert _row(resumed.output, "chunks read before") == 1
