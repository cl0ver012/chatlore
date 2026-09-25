"""Tests for archives, Markdown export, and the demo library."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chatlore.archive import (
    ArchiveError,
    conversation_markdown,
    export_archive,
    export_markdown,
    import_archive,
    is_archive,
    read_manifest,
    restore_graph,
)
from chatlore.cli import DEMO_ARCHIVE, display_path
from chatlore.cli import app as cli
from chatlore.library import Library
from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind
from chatlore.store import Edge, EdgeType, Label, Node, open_store
from tests.conftest import make_zip
from tests.fakes import FakeEmbedder, FakeLLM

runner = CliRunner()

NOTES = {
    "Mac tools": "Parsec keeps dropping frames on the Mac. Try Moonlight with Sunshine instead.",
    "Weekend": "Lisbon and Sintra make a good weekend. Take the train from Rossio to Sintra.",
}


@pytest.fixture
def embedder(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    shared = FakeEmbedder()
    monkeypatch.setattr("chatlore.cli.make_embedder", lambda home: shared)
    monkeypatch.setattr("chatlore.cli.make_llm", FakeLLM)
    monkeypatch.setenv("COLUMNS", "120")
    return shared


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, embedder: FakeEmbedder) -> Path:
    """A library with conversations, chunks, embeddings, entities, and topics."""
    target = tmp_path / "home"
    monkeypatch.setenv("CHATLORE_HOME", str(target))
    for title, text in NOTES.items():
        runner.invoke(cli, ["note", text, "--title", title])
    runner.invoke(cli, ["process"])
    runner.invoke(cli, ["extract"])
    return target


def counts(home: Path) -> dict[str, int]:
    with open_store(home) as graph:
        found: dict[str, int] = {label: graph.count_nodes(label) for label in Label}
        found["embeddings"] = graph.count_embeddings()
    return found


def test_an_archive_restores_the_library_and_its_graph(
    home: Path, tmp_path: Path, embedder: FakeEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = counts(home)
    report = export_archive(home, tmp_path / "library.zip")
    other = tmp_path / "other"
    monkeypatch.setenv("CHATLORE_HOME", str(other))
    embedder.embedded.clear()

    imported = runner.invoke(cli, ["import", str(tmp_path / "library.zip")])
    after_import = counts(other)
    processed = runner.invoke(cli, ["process"])

    assert report.conversations == 2
    assert report.caches == ("embeddings.db", "extractions.db")
    assert imported.exit_code == 0
    assert "archive import" in imported.output
    assert {c.title for c in Library(other)} == set(NOTES)
    assert after_import[Label.ENTITY] == before[Label.ENTITY] > 0
    assert after_import[Label.TOPIC] == before[Label.TOPIC] > 0
    assert after_import[Label.CHUNK] == before[Label.CHUNK]
    assert processed.exit_code == 0
    assert counts(other) == before
    assert embedder.embedded == []  # every vector came from the archive's cache


def test_the_restored_graph_answers_like_the_original(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = runner.invoke(cli, ["entity", "parsec"]).output
    export_archive(home, tmp_path / "library.zip")
    monkeypatch.setenv("CHATLORE_HOME", str(tmp_path / "other"))
    runner.invoke(cli, ["import", str(tmp_path / "library.zip")])

    assert runner.invoke(cli, ["entity", "parsec"]).output == original
    assert "Parsec" in original


def test_importing_an_archive_twice_changes_nothing(home: Path, tmp_path: Path) -> None:
    export_archive(home, tmp_path / "library.zip")
    before = counts(home)

    report = import_archive(tmp_path / "library.zip", home)

    assert dict(report.outcomes) == {"unchanged": 2}
    assert counts(home) == before


def test_an_archive_joins_an_existing_library_and_its_caches(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_archive(home, tmp_path / "library.zip")
    other = tmp_path / "other"
    monkeypatch.setenv("CHATLORE_HOME", str(other))
    runner.invoke(cli, ["note", "Grafana dashboards for the NAS", "--title", "NAS"])
    runner.invoke(cli, ["process"])
    with closing(sqlite3.connect(other / "cache" / "embeddings.db")) as db:
        own = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]

    import_archive(tmp_path / "library.zip", other)

    assert {c.title for c in Library(other)} == {*NOTES, "NAS"}
    with closing(sqlite3.connect(other / "cache" / "embeddings.db")) as db:
        merged = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    with closing(sqlite3.connect(home / "cache" / "embeddings.db")) as db:
        archived = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    assert merged == own + archived


def test_a_dry_run_only_counts(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    export_archive(home, tmp_path / "library.zip")
    other = tmp_path / "other"
    monkeypatch.setenv("CHATLORE_HOME", str(other))

    result = runner.invoke(cli, ["import", str(tmp_path / "library.zip"), "--dry-run"])

    assert result.exit_code == 0
    assert "conversations parsed" in result.output
    assert not other.exists()


def test_edges_to_nodes_missing_everywhere_are_left_out(tmp_path: Path) -> None:
    with open_store(tmp_path) as graph:
        written = restore_graph(
            graph,
            [
                Node("ent_a", Label.ENTITY, {"name": "A"}),
                Node("ent_b", Label.ENTITY, {"name": "B"}),
            ],
            [
                Edge("ent_a", EdgeType.RELATED_TO, "ent_b"),
                Edge("msg_gone", EdgeType.HAS_CHUNK, "ent_a"),
            ],
        )
        assert written == (2, 1)
        assert graph.count_nodes(Label.ENTITY) == 2


def test_only_chatlore_archives_are_recognised(fixtures: Path, tmp_path: Path) -> None:
    export = make_zip(
        tmp_path / "claude.zip",
        {"conversations.json": fixtures / "claude" / "conversations.json"},
    )
    newer = make_zip(
        tmp_path / "newer.zip",
        {
            "manifest.json": json.dumps({"format": "chatlore-archive", "version": 99}),
            "conversations.jsonl": "",
        },
    )

    assert not is_archive(export)
    assert not is_archive(fixtures / "claude" / "conversations.json")
    assert not is_archive(tmp_path / "missing.zip")
    with pytest.raises(ArchiveError, match="newer ChatLore"):
        read_manifest(newer)


def test_importing_something_else_as_an_archive_fails_clearly(
    fixtures: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHATLORE_HOME", str(tmp_path / "home"))
    path = fixtures / "claude" / "conversations.json"

    result = runner.invoke(cli, ["import", str(path), "--source", "chatlore"])

    assert result.exit_code == 1
    assert "is not a ChatLore archive" in " ".join(result.output.split())


def test_export_needs_a_library_and_a_file(tmp_path: Path, home: Path) -> None:
    folder = runner.invoke(cli, ["export", str(tmp_path)])
    written = runner.invoke(cli, ["export", str(tmp_path / "out.zip")])

    assert folder.exit_code == 1
    assert "is a folder" in folder.output
    assert written.exit_code == 0
    assert "Wrote 2 conversations" in written.output
    assert read_manifest(tmp_path / "out.zip")["conversations"] == 2
    assert not (tmp_path / "out.zip.partial").exists()


def test_export_of_an_empty_library_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHATLORE_HOME", str(tmp_path / "empty"))

    result = runner.invoke(cli, ["export", str(tmp_path / "out.zip")])

    assert result.exit_code == 1
    assert "library is empty" in result.output
    assert not (tmp_path / "out.zip").exists()


def conversation(title: str | None, day: int = 2, number: int = 1) -> Conversation:
    when = datetime(2026, 3, day, 9, 30, tzinfo=UTC)
    return Conversation(
        id=f"conv_0000000000000000000000{day}{number}",
        source=SourceKind.CLAUDE,
        external_id=f"{day}{number}",
        title=title,
        created_at=when,
        model="claude-sonnet",
        messages=[
            Message(id="m1", role=Role.USER, created_at=when, content=[ContentPart(text="Hi?")]),
            Message(
                id="m2",
                parent_id="m1",
                role=Role.ASSISTANT,
                content=[ContentPart(text="print(1)", type="code", language="python")],
            ),
            Message(id="m3", parent_id="m1", role=Role.ASSISTANT, content=[]),
        ],
        current_leaf_id="m2",
    )


def test_a_conversation_as_markdown() -> None:
    markdown = conversation_markdown(conversation("Setting up: CI/CD?"))

    assert markdown.startswith("---\ntitle: 'Setting up: CI/CD?'\nsource: claude\n")
    assert "created: 2026-03-02 09:30 UTC\n" in markdown
    assert "model: claude-sonnet\n---\n" in markdown
    assert "\n# Setting up: CI/CD?\n" in markdown
    assert "## You · 2026-03-02 09:30 UTC\n\nHi?\n" in markdown
    assert "## Assistant\n\n```python\nprint(1)\n```\n" in markdown
    assert markdown.count("## Assistant") == 1  # the branch it ended on, without empty ones


def test_markdown_files_are_named_by_date_and_title(tmp_path: Path) -> None:
    written = export_markdown(
        [
            conversation("Setting up: CI/CD?"),
            conversation("Setting up: CI/CD?", number=2),
            conversation(None, day=5),
        ],
        tmp_path,
    )

    names = sorted(path.name for path in (tmp_path / "claude").iterdir())
    assert written == 3
    assert names == [
        "2026-03-02 Setting up- CI-CD- 00000022.md",
        "2026-03-02 Setting up- CI-CD-.md",
        "2026-03-05 untitled.md",
    ]


def test_the_export_command_writes_markdown(home: Path, tmp_path: Path) -> None:
    result = runner.invoke(cli, ["export", str(tmp_path / "md"), "--markdown"])

    files = sorted((tmp_path / "md" / "note").glob("*.md"))
    assert result.exit_code == 0
    assert "Wrote 2 conversations as Markdown" in result.output
    assert [path.name[11:] for path in files] == ["Mac tools.md", "Weekend.md"]
    assert "Take the train from Rossio to Sintra." in files[1].read_text(encoding="utf-8")


def test_the_home_option_picks_the_library(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHATLORE_HOME", str(tmp_path / "elsewhere"))

    result = runner.invoke(cli, ["--home", str(home), "stats"])

    assert result.exit_code == 0
    assert "note" in result.output
    assert "2" in result.output


def test_the_demo_loads_a_made_up_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, embedder: FakeEmbedder
) -> None:
    demo = tmp_path / "demo"
    monkeypatch.setattr("chatlore.cli.demo_home", lambda: demo)
    monkeypatch.setenv("CHATLORE_HOME", str(tmp_path / "mine"))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    first = runner.invoke(cli, ["demo", "--no-serve"])
    again = runner.invoke(cli, ["demo", "--no-serve"])

    assert first.exit_code == 0, first.output
    assert "Demo library ready" in first.output
    assert "Asking questions needs a model key" in first.output
    assert f'chatlore --home {display_path(demo)} search "litestream"\n' in first.output
    assert again.output == first.output
    found = counts(demo)
    assert found[Label.CONVERSATION] == 32
    assert found[Label.ENTITY] > 100 and found[Label.TOPIC] > 10
    assert found["embeddings"] == found[Label.CHUNK]
    assert not (tmp_path / "mine").exists()


def test_the_bundled_demo_is_a_complete_archive() -> None:
    manifest = read_manifest(DEMO_ARCHIVE)
    with zipfile.ZipFile(DEMO_ARCHIVE) as demo:
        names = set(demo.namelist())

    assert manifest["conversations"] == 32
    assert {"cache/embeddings.db", "cache/extractions.db", "graph.jsonl"} <= names
