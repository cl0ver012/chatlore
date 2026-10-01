"""Tests for uploading, exporting, and visitors' own libraries."""

from __future__ import annotations

import io
import threading
import time
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from chatlore.api import create_app
from chatlore.archive import export_archive, read_manifest
from chatlore.cli import app as cli
from chatlore.llm import LLMError
from chatlore.spaces import COOKIE, Spaces
from tests.conftest import make_zip
from tests.fakes import FakeEmbedder, FakeLLM

runner = CliRunner()
CHANGE = {"X-ChatLore": "1"}


@pytest.fixture
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A small library to serve, with its graph built."""
    target = tmp_path / "home"
    monkeypatch.setenv("CHATLORE_HOME", str(target))
    monkeypatch.setenv("COLUMNS", "120")
    for module in ("cli", "api", "mcp_server"):
        monkeypatch.setattr(f"chatlore.{module}.make_embedder", lambda home: FakeEmbedder())
    fake = FakeLLM()
    monkeypatch.setattr("chatlore.cli.make_llm", lambda: fake)
    monkeypatch.setattr("chatlore.api.make_llm", lambda: FakeLLM())
    runner.invoke(cli, ["note", "Lisbon and Sintra make a good weekend.", "--title", "Weekend"])
    runner.invoke(cli, ["process"])
    runner.invoke(cli, ["extract"])
    return target


@pytest.fixture
def private(home: Path) -> Iterator[TestClient]:
    with TestClient(create_app(home)) as client:
        yield client


@pytest.fixture
def spaces(tmp_path: Path) -> Spaces:
    return Spaces(tmp_path / "spaces")


@pytest.fixture
def public(home: Path, spaces: Spaces) -> Iterator[TestClient]:
    with TestClient(create_app(home, public=True, spaces=spaces)) as client:
        yield client


def upload(client: TestClient, path: Path, name: str | None = None) -> Any:
    return client.post(
        "/library/import",
        content=path.read_bytes(),
        headers={**CHANGE, "X-Filename": name or path.name},
    )


def finished(client: TestClient) -> dict[str, Any]:
    """The import's status once it stops."""
    for _ in range(400):
        status = client.get("/library").json()["import"]
        if status is not None and not status["running"]:
            return dict(status)
        time.sleep(0.025)
    raise AssertionError("the import did not finish")


def test_an_upload_is_imported_embedded_and_read_into_the_graph(
    private: TestClient, home: Path, fixtures: Path
) -> None:
    before = private.get("/stats").json()

    started = upload(private, fixtures / "claude" / "conversations.json")
    status = finished(private)
    after = private.get("/stats").json()

    assert started.status_code == 202
    assert started.json()["file"] == "conversations.json"
    assert status["stage"] == "done", status
    assert status["conversations"] == 3
    assert after["conversations"] == before["conversations"] + 3
    assert after["chunks"] == after["embeddings"] > before["chunks"]
    assert after["entities"] > before["entities"]
    assert list((home / "uploads").iterdir()) == []  # the upload is not kept


def test_the_server_says_whether_it_takes_uploads(private: TestClient, home: Path) -> None:
    info = private.get("/library").json()
    with TestClient(create_app(home, public=True)) as demo:
        closed = demo.get("/library").json()
        refused = demo.post("/library/import", content=b"x", headers=CHANGE)

    assert info["own"] is True and info["uploads"] is True and info["import"] is None
    assert private.get("/health").json()["uploads"] is True
    assert closed["uploads"] is False and closed["own"] is False
    assert refused.status_code == 403


def test_changes_need_the_interface_header(private: TestClient, fixtures: Path) -> None:
    path = fixtures / "claude" / "conversations.json"

    without = private.post("/library/import", content=path.read_bytes())
    delete = private.delete("/library")

    assert without.status_code == 403
    assert "X-ChatLore" in without.json()["detail"]
    assert delete.status_code == 403


def test_empty_and_oversized_uploads_are_refused(home: Path, fixtures: Path) -> None:
    with TestClient(create_app(home, max_upload=100)) as client:
        empty = client.post("/library/import", content=b"", headers=CHANGE)
        large = upload(client, fixtures / "claude" / "conversations.json")

    assert empty.status_code == 400
    assert large.status_code == 413
    assert list((home / "uploads").iterdir()) == []


def test_an_unreadable_upload_fails_with_a_reason(private: TestClient, tmp_path: Path) -> None:
    path = tmp_path / "photo.zip"
    make_zip(path, {"photo.jpg": "not an export"})

    upload(private, path)
    status = finished(private)

    assert status["stage"] == "failed"
    assert status["error"] == "nothing ChatLore can read was found in the upload"
    assert status["skipped_shown"] == [
        ["photo.zip/photo.jpg", "images, audio, video, and programs are not read"]
    ]


def test_a_zip_of_markdown_notes_and_an_archive_are_imported(
    private: TestClient, home: Path, fixtures: Path, tmp_path: Path
) -> None:
    vault = fixtures / "markdown" / "vault"
    notes = make_zip(
        tmp_path / "vault.zip",
        {str(path.relative_to(vault)): path for path in vault.rglob("*") if path.is_file()},
    )
    upload(private, notes)
    from_notes = finished(private)

    other = tmp_path / "other"
    export_archive(home, tmp_path / "library.zip")
    with TestClient(create_app(other)) as client:
        upload(client, tmp_path / "library.zip")
        from_archive = finished(client)
        stats = client.get("/stats").json()

    assert from_notes["stage"] == "done" and from_notes["conversations"] > 0
    assert from_archive["stage"] == "done", from_archive
    assert stats["conversations"] == from_notes["conversations"] + 1
    assert stats["entities"] > 0


def test_without_a_model_key_everything_but_the_graph_is_built(
    home: Path, fixtures: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_key() -> FakeLLM:
        raise LLMError("no API key for OpenRouter")

    monkeypatch.setattr("chatlore.api.make_llm", no_key)
    with TestClient(create_app(home)) as client:
        upload(client, fixtures / "claude" / "conversations.json")
        status = finished(client)
        stats = client.get("/stats").json()

    assert status["stage"] == "done"
    assert status["notes"] == ["The knowledge graph was not built: no API key for OpenRouter"]
    assert stats["chunks"] == stats["embeddings"]


def test_the_model_reads_only_as_many_passages_as_allowed(home: Path, fixtures: Path) -> None:
    with TestClient(create_app(home, extract_limit=1)) as client:
        upload(client, fixtures / "claude" / "conversations.json")
        status = finished(client)

    assert status["stage"] == "done"
    assert status["notes"][0].startswith("The model read 1 of ")


def test_the_library_downloads_as_an_archive_or_markdown(private: TestClient) -> None:
    archive = private.get("/library/export")
    markdown = private.get("/library/export", params={"format": "markdown"})

    assert archive.status_code == 200
    assert "chatlore-library.zip" in archive.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
        assert {"manifest.json", "conversations.jsonl", "graph.jsonl"} <= set(zipped.namelist())
    with zipfile.ZipFile(io.BytesIO(markdown.content)) as zipped:
        names = zipped.namelist()
    assert any(name.endswith("Weekend.md") for name in names)
    assert private.get("/library/export", params={"format": "pdf"}).status_code == 422


def test_an_empty_library_has_nothing_to_download(tmp_path: Path) -> None:
    with TestClient(create_app(tmp_path / "empty")) as client:
        assert client.get("/library/export").status_code == 404


def test_each_visitor_gets_a_private_library(
    public: TestClient, home: Path, spaces: Spaces, fixtures: Path
) -> None:
    demo = public.get("/stats").json()

    started = upload(public, fixtures / "claude" / "conversations.json")
    status = finished(public)
    own = public.get("/stats").json()
    info = public.get("/library").json()
    stranger = TestClient(public.app)  # another browser, on the app already running
    theirs = stranger.get("/stats").json()
    stranger_info = stranger.get("/library").json()

    cookie = started.headers["set-cookie"]
    assert status["stage"] == "done"
    assert f"{COOKIE}=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    assert own["conversations"] == 3  # only the visitor's upload, not the demo
    assert theirs == demo
    assert info["own"] is True and info["expires_at"] is not None and info["keep_hours"] == 24
    assert stranger_info["own"] is False and stranger_info["import"] is None
    homes = [path for path in spaces.root.iterdir()]
    assert len(homes) == 1
    token = public.cookies[COOKIE]
    assert token not in homes[0].name  # the folder does not give the token away


def test_a_visitor_can_delete_their_library(
    public: TestClient, spaces: Spaces, fixtures: Path
) -> None:
    upload(public, fixtures / "claude" / "conversations.json")
    finished(public)

    deleted = public.delete("/library", headers=CHANGE)
    again = public.delete("/library", headers=CHANGE)

    assert deleted.status_code == 200
    assert list(spaces.root.iterdir()) == []
    assert again.status_code == 404
    assert public.get("/library").json()["own"] is False


def test_over_https_the_library_also_works_in_another_sites_frame(
    home: Path, spaces: Spaces, fixtures: Path
) -> None:
    app = create_app(home, public=True, spaces=spaces)
    with TestClient(app, base_url="https://demo.example") as client:
        started = upload(client, fixtures / "claude" / "conversations.json")
        finished(client)
        own = client.get("/stats").json()
        deleted = client.delete("/library", headers=CHANGE)
        after = client.get("/library").json()

    given = started.headers["set-cookie"]
    removed = deleted.headers["set-cookie"]
    assert all(part in given for part in ("SameSite=None", "Secure", "Partitioned", "HttpOnly"))
    assert own["conversations"] == 3
    assert "Max-Age=0" in removed and "Partitioned" in removed
    assert after["own"] is False


def test_the_servers_own_library_cannot_be_deleted(private: TestClient) -> None:
    assert private.delete("/library", headers=CHANGE).status_code == 403


def test_deleting_stops_an_import_first(
    home: Path, spaces: Spaces, fixtures: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    class Slow(FakeEmbedder):
        def embed_passages(self, texts: Any) -> list[list[float]]:
            release.wait(5)
            return super().embed_passages(texts)

    monkeypatch.setattr("chatlore.api.make_embedder", lambda home: Slow())
    with TestClient(create_app(home, public=True, spaces=spaces)) as client:
        upload(client, fixtures / "claude" / "conversations.json")
        for _ in range(200):
            if client.get("/library").json()["import"]["stage"] == "embedding":
                break
            time.sleep(0.02)
        busy = upload(client, fixtures / "claude" / "conversations.json")
        client.delete("/library", headers=CHANGE)
        release.set()
        for _ in range(200):
            if not list(spaces.root.iterdir()):
                break
            time.sleep(0.02)

    assert busy.status_code == 409
    assert list(spaces.root.iterdir()) == []


def test_libraries_expire(tmp_path: Path) -> None:
    now = [datetime(2026, 9, 1, tzinfo=UTC)]
    spaces = Spaces(tmp_path / "spaces", keep=timedelta(hours=24), clock=lambda: now[0])

    token, space = spaces.create()
    found = spaces.find(token)
    now[0] += timedelta(hours=25)

    assert found == space
    assert spaces.find(token) is None
    assert spaces.find("not a token") is None
    assert spaces.find(None) is None
    assert spaces.expired() == [space]
    (spaces.root / "leftover").mkdir()
    assert [expired.id for expired in spaces.expired()] == [space.id, "leftover"]
    spaces.delete(space)
    assert not space.home.exists()


def test_expired_libraries_are_swept_away(home: Path, tmp_path: Path) -> None:
    now = [datetime.now(UTC) - timedelta(hours=30)]
    spaces = Spaces(tmp_path / "spaces", clock=lambda: now[0])
    _, old = spaces.create()
    now[0] = datetime.now(UTC)

    with TestClient(create_app(home, public=True, spaces=spaces)):
        for _ in range(200):
            if not old.home.exists():
                break
            time.sleep(0.02)

    assert not old.home.exists()


def test_serve_takes_uploads_on_request(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[Any, dict[str, Any]]] = []
    monkeypatch.setattr("uvicorn.run", lambda app, **options: started.append((app, options)))

    result = runner.invoke(
        cli,
        [
            "serve",
            "--public",
            "--uploads",
            "--keep-hours",
            "2",
            "--spaces-dir",
            str(tmp_path / "v"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "each gets a private library" in " ".join(result.output.split())
    with TestClient(started[0][0]) as client:
        info = client.get("/library").json()
    assert info["uploads"] is True and info["keep_hours"] == 2


def test_the_archive_downloaded_from_a_visitor_library_imports_anywhere(
    public: TestClient, fixtures: Path, tmp_path: Path
) -> None:
    upload(public, fixtures / "claude" / "conversations.json")
    finished(public)

    download = public.get("/library/export")
    (tmp_path / "mine.zip").write_bytes(download.content)

    assert read_manifest(tmp_path / "mine.zip")["conversations"] == 3


def send(client: TestClient, batch: str, name: str, content: bytes) -> Any:
    return client.post(
        "/library/files",
        params={"batch": batch},
        content=content,
        headers={**CHANGE, "X-Filename": name},
    )


def test_many_files_with_their_folders_import_together(
    private: TestClient, home: Path, fixtures: Path, tmp_path: Path
) -> None:
    from tests import samples

    batch = "0123456789abcdef0123456789abcdef"
    report = samples.pdf(tmp_path / "report.pdf", "Tide report", ["High tide at 14:05"])
    sent = [
        send(
            private,
            batch,
            "Claude%20export/conversations.json",
            (fixtures / "claude" / "conversations.json").read_bytes(),
        ),
        send(private, batch, "Docs/report.pdf", report.read_bytes()),
        send(private, batch, "../../outside.md", b"# Outside\n\nStill inside."),
        send(private, batch, "Docs/photo.jpg", b"\x00"),
    ]

    started = private.post("/library/import", params={"batch": batch}, headers=CHANGE)
    status = finished(private)

    assert [response.status_code for response in sent] == [200, 200, 200, 200]
    assert sent[-1].json()["files"] == 4
    assert started.status_code == 202 and started.json()["file"] == "4 files"
    assert status["stage"] == "done", status
    assert status["sources"] == {"claude": 3, "document": 1, "markdown": 1}
    assert status["skipped_files"] == 1
    assert status["skipped_shown"] == [
        ["Docs/photo.jpg", "images, audio, video, and programs are not read"]
    ]
    assert not (home / "outside.md").exists()
    assert list((home / "uploads").iterdir()) == []


def test_batches_are_checked(home: Path) -> None:
    with TestClient(create_app(home, max_upload=10)) as client:
        bad = send(client, "not-hex", "a.txt", b"x")
        empty = client.post("/library/import", params={"batch": "f" * 32}, headers=CHANGE)
        first = send(client, "a" * 32, "a.txt", b"12345")
        over = send(client, "a" * 32, "b.txt", b"1234567")
        unmarked = client.post("/library/files", params={"batch": "a" * 32}, content=b"x")

    assert bad.status_code == 422
    assert empty.status_code == 400
    assert first.status_code == 200
    assert over.status_code == 413
    assert unmarked.status_code == 403
