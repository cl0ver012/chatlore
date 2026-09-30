"""Tests for reading documents of every kind into notes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from chatlore.importers.documents import DocumentError, read_document, readable
from chatlore.models import PartType, SourceKind
from tests import samples


def one(path: Path) -> tuple[str | None, str, SourceKind]:
    [conversation] = read_document(path, path.name)
    return conversation.title, conversation.messages[0].text, conversation.source


def test_office_files_become_notes_with_their_titles(tmp_path: Path) -> None:
    docx = samples.docx(
        tmp_path / "plan.docx", "Garden plan", ["Plant tomatoes in May.", "Water daily."]
    )
    [note] = read_document(docx, "plans/plan.docx")

    assert note.source is SourceKind.DOCUMENT
    assert note.title == "Garden plan"
    assert note.created_at == datetime(2026, 3, 4, 10, tzinfo=UTC)
    assert note.messages[0].text == "Plant tomatoes in May.\nWater daily."
    assert note.metadata == {"kind": "document", "path": "plans/plan.docx", "type": "docx"}
    assert one(samples.pptx(tmp_path / "talk.pptx", [["Intro", "Why"], ["Demo"]]))[1] == (
        "Slide 1\nIntro\nWhy\n\nSlide 2\nDemo"
    )
    assert one(samples.xlsx(tmp_path / "b.xlsx", "Costs", [["Item", "Price"], ["Kayak", "900"]]))[
        1
    ] == ("Costs\nItem: Kayak | Price: 900")
    assert one(samples.odt(tmp_path / "l.odt", "Letter", ["Dear Sam,", "See you."])) == (
        "Letter",
        "Dear Sam,\nSee you.",
        SourceKind.DOCUMENT,
    )


def test_books_and_pdfs_are_read(tmp_path: Path) -> None:
    book = one(samples.epub(tmp_path / "b.epub", "Tide Tales", ["Chapter one.", "Chapter two."]))
    report = one(
        samples.pdf(tmp_path / "r.pdf", "Tide report", ["High tide at 14:05", "Low at 20:10"])
    )

    assert book[:2] == ("Tide Tales", "Chapter one.\n\nChapter two.")
    assert report[:2] == ("Tide report", "High tide at 14:05\nLow at 20:10")


def test_email_becomes_conversations_one_per_message(tmp_path: Path) -> None:
    [mail] = read_document(
        samples.email(tmp_path / "trip.eml", "Kayak trip", "Meet at 8."), "trip.eml"
    )
    box = read_document(
        samples.mbox(tmp_path / "box.mbox", [("First", "Hello one"), ("Second", "Hello two")]),
        "box.mbox",
    )

    assert mail.source is SourceKind.EMAIL
    assert mail.title == "Kayak trip"
    assert mail.created_at == datetime(2026, 3, 3, 9, 30, tzinfo=UTC)
    assert "From: Maya <maya@example.com>" in mail.messages[0].text
    assert "Meet at 8." in mail.messages[0].text
    assert [conversation.title for conversation in box] == ["First", "Second"]
    assert len({conversation.id for conversation in box}) == 2


def test_text_and_data_files_are_read(tmp_path: Path) -> None:
    (tmp_path / "people.csv").write_text("name,city\nMaya,Lisbon\nSam,,\n", encoding="utf-8")
    (tmp_path / "settings.json").write_text(
        '{"trip": {"to": "Porto", "days": [1, 2]}}', encoding="utf-8"
    )
    (tmp_path / "page.html").write_text(
        "<html><head><title>Tides</title><script>var x = 1;</script></head>"
        "<body><h1>High tide</h1><p>At 14:05</p></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "notes.rtf").write_text(r"{\rtf1\ansi {\b Bold} plain\par next}", encoding="utf-8")
    (tmp_path / "old.txt").write_bytes("Caf\xe9 in Porto".encode("cp1252"))

    assert one(tmp_path / "people.csv")[1] == "name: Maya | city: Lisbon\nname: Sam"
    assert one(tmp_path / "settings.json")[1] == "trip.to: Porto\ntrip.days[0]: 1\ntrip.days[1]: 2"
    assert one(tmp_path / "page.html")[:2] == ("Tides", "High tide\nAt 14:05")
    assert one(tmp_path / "notes.rtf")[1] == "Bold plain\nnext"
    assert one(tmp_path / "old.txt")[1] == "Café in Porto"


def test_code_keeps_its_language_and_markdown_stays_a_note(tmp_path: Path) -> None:
    (tmp_path / "tide.py").write_text("def high():\n    return 14\n", encoding="utf-8")
    (tmp_path / "idea.md").write_text("# Idea\n\nA graph of chats.", encoding="utf-8")

    [code] = read_document(tmp_path / "tide.py", "tide.py")
    [note] = read_document(tmp_path / "idea.md", "idea.md")

    assert code.messages[0].content[0].type is PartType.CODE
    assert code.messages[0].content[0].language == "python"
    assert note.source is SourceKind.MARKDOWN and note.title == "Idea"


def test_files_are_recognised_by_name_or_by_looking(tmp_path: Path) -> None:
    (tmp_path / "server.conf").write_text("port = 80\n", encoding="utf-8")
    (tmp_path / "blob.dat").write_bytes(b"\x00\x01\x02binary")
    (tmp_path / "photo.jpg").write_bytes(b"not really a photo")

    assert readable(tmp_path / "server.conf")
    assert not readable(tmp_path / "blob.dat")
    assert not readable(tmp_path / "photo.jpg")
    assert readable(tmp_path / "missing.pdf")


def test_documents_without_text_are_refused_with_a_reason(tmp_path: Path) -> None:
    (tmp_path / "empty.md").write_text("   \n", encoding="utf-8")
    samples.pdf(tmp_path / "scan.pdf", "Scan", [])
    (tmp_path / "broken.docx").write_bytes(b"not a zip")

    with pytest.raises(DocumentError, match="empty"):
        read_document(tmp_path / "empty.md", "empty.md")
    with pytest.raises(DocumentError, match="no text"):
        read_document(tmp_path / "scan.pdf", "scan.pdf")
    with pytest.raises(DocumentError, match="could not be read"):
        read_document(tmp_path / "broken.docx", "broken.docx")


def test_a_documents_identity_is_its_path(tmp_path: Path) -> None:
    (tmp_path / "a.csv").write_text("x\n1\n", encoding="utf-8")

    first = read_document(tmp_path / "a.csv", "data/a.csv")[0]
    again = read_document(tmp_path / "a.csv", "data/a.csv")[0]
    elsewhere = read_document(tmp_path / "a.csv", "other/a.csv")[0]

    assert first.id == again.id != elsewhere.id
