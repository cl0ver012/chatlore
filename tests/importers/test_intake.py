"""Tests for importing whatever is dropped: files, folders, and archives."""

from __future__ import annotations

import gzip
import json
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

from chatlore.importers.base import ImporterError
from chatlore.importers.intake import MAX_FILES, MAX_UNPACKED, Intake, bsdtar, describe
from tests import samples
from tests.conftest import make_zip


def take(
    tmp_path: Path, *paths: Path, max_unpacked: int = MAX_UNPACKED, max_files: int = MAX_FILES
) -> tuple[Intake, dict[str, str]]:
    """Scan ``paths`` and read everything: the intake and each conversation's title by source."""
    work = Path(tempfile.mkdtemp(prefix="work", dir=tmp_path))
    intake = Intake(work, max_unpacked=max_unpacked, max_files=max_files)
    intake.scan(list(paths))
    titles = {
        f"{c.source.value}: {c.title}": c.metadata.get("path", "") for c in intake.conversations()
    }
    return intake, titles


def export_folder(root: Path) -> Path:
    """A ChatGPT export as it unzips, with its companions and a picture."""
    root.mkdir(parents=True)
    (root / "conversations.json").write_text(samples.CHATGPT, encoding="utf-8")
    (root / "chat.html").write_text("<html>the same chats again</html>", encoding="utf-8")
    (root / "user.json").write_text('{"id": "user-1"}', encoding="utf-8")
    (root / "file-abc.png").write_bytes(b"\x89PNG")
    return root


def test_a_folder_of_everything_is_sorted_out(tmp_path: Path) -> None:
    drop = tmp_path / "drop"
    export_folder(drop / "ChatGPT")
    (drop / "notes").mkdir()
    (drop / "notes" / "idea.md").write_text("# Idea\n\nA graph of chats.", encoding="utf-8")
    samples.pdf(drop / "report.pdf", "Tide report", ["High tide at 14:05"])
    samples.email(drop / "trip.eml", "Kayak trip", "Meet at 8.")
    (drop / ".git").mkdir()
    (drop / ".git" / "config").write_text("secret", encoding="utf-8")
    (drop / ".DS_Store").write_bytes(b"\x00")

    intake, titles = take(tmp_path, drop)

    assert titles == {
        "chatgpt: Trip ideas": "",
        "markdown: Idea": "notes/idea.md",
        "document: Tide report": "report.pdf",
        "email: Kayak trip": "trip.eml",
    }
    assert dict(intake.sources) == {"chatgpt": 1, "markdown": 1, "document": 1, "email": 1}
    assert {(item.path, item.reason) for item in intake.skipped} == {
        ("ChatGPT/chat.html", "part of the ChatGPT export"),
        ("ChatGPT/user.json", "part of the ChatGPT export"),
        ("ChatGPT/file-abc.png", "images, audio, video, and programs are not read"),
    }


def test_archives_inside_archives_are_unpacked(tmp_path: Path) -> None:
    inner = make_zip(tmp_path / "inner.zip", {"deep/plan.txt": "Plant tomatoes in May."})
    tarball = tmp_path / "notes.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(inner, arcname="inner.zip")
    outer = make_zip(
        tmp_path / "outer.zip",
        {"ChatGPT/conversations.json": samples.CHATGPT, "notes.tar.gz": tarball},
    )
    compressed = tmp_path / "log.txt.gz"
    compressed.write_bytes(gzip.compress(b"Deploy went fine."))

    intake, titles = take(tmp_path, outer, compressed)

    assert titles == {
        "chatgpt: Trip ideas": "",
        "markdown: plan": "outer.zip/notes.tar.gz/inner.zip/deep/plan.txt",
        "markdown: log": "log.txt",
    }
    assert intake.skipped == []


def test_google_takeout_brings_gemini_and_skips_other_activity(
    tmp_path: Path, fixtures: Path
) -> None:
    gemini = (fixtures / "gemini" / "MyActivity.json").read_text(encoding="utf-8")
    search = json.dumps(
        [{"header": "Search", "title": "Searched for tides", "time": "2026-01-01T00:00:00Z"}]
    )
    takeout = make_zip(
        tmp_path / "takeout-001.zip",
        {
            "Takeout/My Activity/Gemini Apps/MyActivity.json": gemini,
            "Takeout/My Activity/Search/MyActivity.json": search,
            "Takeout/archive_browser.html": "<html>index</html>",
            "Takeout/Drive/Trip.docx": samples.docx(
                tmp_path / "Trip.docx", "Trip", ["Porto in May."]
            ),
        },
    )

    intake, titles = take(tmp_path, takeout)

    assert intake.sources["gemini"] > 0
    assert "document: Trip" in titles
    assert [(item.path, item.reason) for item in intake.skipped] == [
        (
            "takeout-001.zip/Takeout/My Activity/Search/MyActivity.json",
            "activity for another Google product",
        )
    ]


def test_chatlore_archives_are_set_aside_for_the_caller(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "library.zip",
        {
            "manifest.json": json.dumps({"format": "chatlore-archive", "version": 1}),
            "conversations.jsonl": "",
        },
    )
    folder = tmp_path / "backups"
    folder.mkdir()
    (folder / "library.zip").write_bytes(archive.read_bytes())

    intake, titles = take(tmp_path, folder)

    assert [shown for _, shown in intake.archives] == ["library.zip"]
    assert titles == {}
    assert intake.found_anything()


def test_unreadable_files_are_listed_and_nothing_found_is_said(tmp_path: Path) -> None:
    (tmp_path / "photos").mkdir()
    for name in ("a.jpg", "b.jpg", "c.mp4"):
        (tmp_path / "photos" / name).write_bytes(b"\x00\x01")
    (tmp_path / "photos" / "blob.bin2").write_bytes(b"\x00binary")

    intake, titles = take(tmp_path, tmp_path / "photos")

    assert titles == {} and not intake.found_anything()
    assert describe(intake.skipped) == [
        "3 files: images, audio, video, and programs are not read",
        "blob.bin2: not a kind of file ChatLore reads",
    ]


def test_unpacking_is_limited_and_stays_inside_its_folder(tmp_path: Path) -> None:
    sneaky = tmp_path / "sneaky.zip"
    with zipfile.ZipFile(sneaky, "w") as archive:
        archive.writestr("../../escaped.txt", "outside")
        archive.writestr("/etc/absolute.txt", "outside")
    big = make_zip(tmp_path / "big.zip", {"a.txt": "x" * 5000})

    intake, titles = take(tmp_path, sneaky)
    with pytest.raises(ImporterError, match="unpack to more than"):
        take(tmp_path, big, max_unpacked=1000)
    with pytest.raises(ImporterError, match="more than 2 files"):
        take(
            tmp_path,
            make_zip(tmp_path / "many.zip", {"1.txt": "a", "2.txt": "b", "3.txt": "c"}),
            max_files=2,
        )

    assert not (tmp_path / "escaped.txt").exists() and not Path("/etc/absolute.txt").exists()
    assert sorted(titles) == ["markdown: absolute", "markdown: escaped"]
    assert all(intake.workdir in path.parents for path, _ in intake.documents)


def test_a_missing_path_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ImporterError, match="does not exist"):
        take(tmp_path, tmp_path / "nowhere")


@pytest.mark.skipif(bsdtar() is None, reason="needs bsdtar")
def test_7z_archives_are_unpacked_with_bsdtar(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "plan.md").write_text("# Plan\n\nKayak on Sunday.", encoding="utf-8")
    tool = bsdtar()
    assert tool is not None
    subprocess.run(
        [tool, "--format", "7zip", "-cf", str(tmp_path / "notes.7z"), "-C", str(source), "plan.md"],
        check=True,
    )

    _, titles = take(tmp_path, tmp_path / "notes.7z")

    assert titles == {"markdown: Plan": "notes.7z/plan.md"}
