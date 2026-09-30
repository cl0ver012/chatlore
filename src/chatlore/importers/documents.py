"""Reading documents of many kinds into notes: text, data, office files, PDF, and email.

Each document becomes a conversation holding one message with its text, like a
Markdown note, so it is chunked, searched, and read into the knowledge graph
the same way. An email becomes a conversation of its own, and a mailbox one per
email. The file's path within what was imported is its identity, so importing
the same files again changes nothing.

Office files (Word, PowerPoint, Excel, OpenDocument) and EPUB books are zipped
XML or HTML and are read with the standard library; only PDF needs a library.
A document with no text, such as a scanned PDF without a text layer, is
skipped with that reason.
"""

from __future__ import annotations

import csv
import email
import email.policy
import io
import json
import mailbox
import re
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

from pydantic import ValidationError

from chatlore.ids import conversation_id, message_id
from chatlore.importers.markdown import _conversation as markdown_note
from chatlore.models import ContentPart, Conversation, Message, PartType, Role, SourceKind

MAX_DOCUMENT_BYTES = 200_000_000
"""Larger files are skipped rather than read."""

NOTES = frozenset({".md", ".markdown", ".txt"})
"""Read as Markdown notes, as ``chatlore import`` always has."""
TEXT = frozenset({".text", ".rst", ".org", ".log", ".adoc", ".tex", ".srt", ".vtt"})
CODE = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".java": "java", ".kt": "kotlin", ".go": "go",
    ".rs": "rust", ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".scala": "scala", ".sh": "bash",
    ".ps1": "powershell", ".sql": "sql", ".r": "r", ".lua": "lua", ".dart": "dart",
    ".css": "css", ".scss": "scss", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".ini": "ini", ".cfg": "ini", ".ipynb": "json",
}  # fmt: skip
DATA = frozenset({".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".xml"})
MARKUP = frozenset({".html", ".htm", ".xhtml"})
OFFICE = frozenset({".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods", ".epub", ".rtf", ".pdf"})
MAIL = frozenset({".eml", ".mbox"})
MEDIA = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".bmp", ".tif", ".tiff", ".svg",
     ".ico", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".opus", ".mp4", ".mov",
     ".avi", ".mkv", ".webm", ".wmv", ".ttf", ".otf", ".woff", ".woff2", ".exe", ".dll",
     ".so", ".dylib", ".bin", ".iso", ".dmg", ".pyc", ".class", ".jar", ".db", ".sqlite"}
)  # fmt: skip
READABLE = NOTES | TEXT | frozenset(CODE) | DATA | MARKUP | OFFICE | MAIL

_SNIFF_BYTES = 8192
_TITLE_LENGTH = 120


class DocumentError(Exception):
    """A document cannot be read; the message says why."""


def readable(path: Path) -> bool:
    """Whether the file is a kind ChatLore reads, by its name or, failing that, by looking."""
    suffix = path.suffix.lower()
    if suffix in READABLE:
        return True
    if suffix in MEDIA:
        return False
    return _looks_like_text(path)


def read_document(path: Path, relative: str) -> list[Conversation]:
    """The conversations in one document: usually one, one per email in a mailbox.

    Raises ``DocumentError`` when it cannot be read or holds no text.
    """
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise DocumentError("too large to read")
    suffix = path.suffix.lower()
    try:
        if suffix in NOTES:
            try:
                note = markdown_note(path, relative)
            except UnicodeDecodeError:
                note = None  # not UTF-8: read below as text in whatever encoding it has
            else:
                if note is None:
                    raise DocumentError("it is empty")
                return [note]
        if suffix in MAIL:
            return _emails(path, relative)
        reader = _READERS.get(suffix)
        if reader is not None:
            title, text, created = reader(path)
            parts = [ContentPart(text=text)]
        elif suffix in CODE:
            title, created = None, None
            parts = [ContentPart(type=PartType.CODE, language=CODE[suffix], text=_text(path))]
        else:
            title, text, created = None, _text(path), None
            parts = [ContentPart(text=text)]
    except DocumentError:
        raise
    except (OSError, ValueError, KeyError, IndexError, zipfile.BadZipFile) as error:
        raise DocumentError(f"it could not be read: {error}") from error
    except ElementTree.ParseError as error:
        raise DocumentError(f"its XML could not be read: {error}") from error
    except Exception as error:  # a library failing on one odd file must not stop an import
        raise DocumentError(f"it could not be read: {error}") from error
    if not any(part.text.strip() for part in parts):
        raise DocumentError("it holds no text" + (" (a scan?)" if suffix == ".pdf" else ""))
    return [_note(relative, title or PurePosixPath(relative).stem, parts, created, suffix)]


# -- building notes --------------------------------------------------------------


def _note(
    relative: str,
    title: str,
    parts: list[ContentPart],
    created: datetime | None,
    suffix: str,
    source: SourceKind = SourceKind.DOCUMENT,
    key: str | None = None,
) -> Conversation:
    external = key or relative
    conv_id = conversation_id(source, external)
    try:
        return Conversation(
            id=conv_id,
            source=source,
            external_id=external,
            title=" ".join(title.split())[:_TITLE_LENGTH] or PurePosixPath(relative).stem,
            created_at=created,
            updated_at=created,
            messages=[
                Message(
                    id=message_id(conv_id, "body"),
                    role=Role.USER,
                    created_at=created,
                    content=[part for part in parts if part.text.strip()],
                )
            ],
            metadata={"kind": "document", "path": relative, "type": suffix.lstrip(".")},
        )
    except ValidationError as error:
        raise DocumentError(str(error).splitlines()[0]) from error


def _emails(path: Path, relative: str) -> list[Conversation]:
    if path.suffix.lower() == ".eml":
        messages = [email.message_from_bytes(path.read_bytes(), policy=email.policy.default)]
    else:
        box = mailbox.mbox(path, factory=None, create=False)
        try:
            messages = [
                email.message_from_bytes(bytes(item), policy=email.policy.default) for item in box
            ]
        finally:
            box.close()
    found: list[Conversation] = []
    for number, message in enumerate(messages):
        body = _email_body(message)
        if not body.strip():
            continue
        subject = str(message.get("subject") or "") or "(no subject)"
        sender = str(message.get("from") or "")
        header = "\n".join(
            f"{name}: {value}"
            for name, value in (("From", sender), ("To", str(message.get("to") or "")))
            if value
        )
        key = str(message.get("message-id") or "") or f"{relative}#{number}"
        found.append(
            _note(
                relative,
                subject,
                [ContentPart(text=f"{header}\n\n{body}".strip())],
                _email_date(message),
                path.suffix.lower(),
                source=SourceKind.EMAIL,
                key=key if len(messages) > 1 or key != f"{relative}#0" else relative,
            )
        )
    if not found:
        raise DocumentError("it holds no email with text")
    return found


def _email_body(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    text = content if isinstance(content, str) else ""
    return _html_text(text)[1] if part.get_content_subtype() == "html" else text


def _email_date(message: EmailMessage) -> datetime | None:
    try:
        return parsedate_to_datetime(str(message.get("date")))
    except (TypeError, ValueError, IndexError):
        return None


# -- plain text and data ---------------------------------------------------------


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _text(path: Path) -> str:
    return _decode(path.read_bytes())


def _looks_like_text(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(_SNIFF_BYTES)
    except OSError:
        return False
    if not head or b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as error:
        return error.start > len(head) - 4  # a character cut off at the end
    return True


def _table(path: Path) -> tuple[str | None, str, datetime | None]:
    text = _text(path)
    dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    rows = [
        row for row in csv.reader(io.StringIO(text), dialect) if any(cell.strip() for cell in row)
    ]
    if not rows:
        return None, "", None
    header, *body = rows
    if not body:
        return None, " | ".join(header), None
    lines = [
        " | ".join(f"{name}: {value}" for name, value in zip(header, row, strict=False) if value)
        for row in body
    ]
    return None, "\n".join(lines), None


def _json(path: Path) -> tuple[str | None, str, datetime | None]:
    text = _text(path)
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        values = [json.loads(line) for line in text.splitlines() if line.strip()]
        return None, "\n\n".join("\n".join(_flatten(value)) for value in values), None
    return None, "\n".join(_flatten(json.loads(text))), None


def _flatten(value: Any, prefix: str = "") -> Iterator[str]:
    """``key.path: value`` lines, so any JSON reads as text."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from _flatten(inner, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _flatten(inner, f"{prefix}[{index}]")
    elif value is not None and str(value).strip():
        yield f"{prefix}: {value}" if prefix else str(value)


def _xml(path: Path) -> tuple[str | None, str, datetime | None]:
    root = ElementTree.fromstring(path.read_bytes())
    return None, _joined(root.itertext()), None


def _joined(pieces: Iterator[str] | list[str]) -> str:
    lines = (" ".join(piece.split()) for piece in pieces)
    return "\n".join(line for line in lines if line)


# -- HTML and markup ---------------------------------------------------------------


class _TextOfHtml(HTMLParser):
    """The visible text of a page, a paragraph per block, and its title."""

    _BLOCKS = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
         "article", "blockquote", "pre", "td", "th", "dt", "dd", "header", "footer"}
    )  # fmt: skip
    _HIDDEN = frozenset({"script", "style", "noscript", "template", "svg", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.pieces: list[str] = []
        self.title = ""
        self._hidden = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in self._HIDDEN:
            self._hidden += 1
        elif tag in self._BLOCKS:
            self.pieces.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in self._HIDDEN and self._hidden:
            self._hidden -= 1
        elif tag in self._BLOCKS:
            self.pieces.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._hidden:
            self.pieces.append(data)


def _html_text(html: str) -> tuple[str, str]:
    parser = _TextOfHtml()
    parser.feed(html)
    parser.close()
    return " ".join(parser.title.split()), _joined("".join(parser.pieces).split("\n"))


def _html(path: Path) -> tuple[str | None, str, datetime | None]:
    title, text = _html_text(_text(path))
    return title or None, text, None


def _rtf(path: Path) -> tuple[str | None, str, datetime | None]:
    text = _text(path)
    text = re.sub(r"\\par[d]?\b", "\n", text)
    text = re.sub(r"\{\\\*[^{}]*\}", "", text)
    text = re.sub(r"\\'[0-9a-fA-F]{2}", "", text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    text = text.replace("{", "").replace("}", "")
    return None, _joined(text.split("\n")), None


# -- office files, EPUB, and PDF ---------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_TERMS = "{http://purl.org/dc/terms/}"
_TEXT_NS = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_TABLE_NS = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


def _core(archive: zipfile.ZipFile) -> tuple[str | None, datetime | None]:
    """Title and creation date from an Office file's core properties."""
    if "docProps/core.xml" not in archive.namelist():
        return None, None
    root = ElementTree.fromstring(archive.read("docProps/core.xml"))
    title = (root.findtext(f"{_DC}title") or "").strip() or None
    created = _iso((root.findtext(f"{_TERMS}created") or "").strip())
    return title, created


def _iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _docx(path: Path) -> tuple[str | None, str, datetime | None]:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
        title, created = _core(archive)
    paragraphs = [
        "".join(node.text or "" for node in p.iter(f"{_W}t")) for p in root.iter(f"{_W}p")
    ]
    return title, _joined(paragraphs), created


def _pptx(path: Path) -> tuple[str | None, str, datetime | None]:
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            (
                name
                for name in archive.namelist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
            ),
            key=lambda name: int(re.findall(r"\d+", name)[-1]),
        )
        pages: list[str] = []
        for number, name in enumerate(slides, start=1):
            root = ElementTree.fromstring(archive.read(name))
            lines = [
                "".join(node.text or "" for node in p.iter(f"{_A}t")) for p in root.iter(f"{_A}p")
            ]
            body = _joined(lines)
            if body:
                pages.append(f"Slide {number}\n{body}")
        title, created = _core(archive)
    return title, "\n\n".join(pages), created


def _xlsx(path: Path) -> tuple[str | None, str, datetime | None]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [
                "".join(t.text or "" for t in item.iter(f"{_S}t")) for item in root.iter(f"{_S}si")
            ]
        workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        relations = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            rel.get("Id"): rel.get("Target", "")
            for rel in relations
            if rel.get("Target", "").startswith(("worksheets/", "/xl/worksheets/"))
        }
        sheets: list[str] = []
        for sheet in workbook.iter(f"{_S}sheet"):
            target = targets.get(sheet.get(f"{_REL}id"))
            if not target:
                continue
            member = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            if member not in names:
                continue
            rows = _sheet_rows(ElementTree.fromstring(archive.read(member)), shared)
            if rows:
                sheets.append(f"{sheet.get('name', 'Sheet')}\n" + "\n".join(rows))
        title, created = _core(archive)
    return title, "\n\n".join(sheets), created


def _sheet_rows(root: ElementTree.Element, shared: list[str]) -> list[str]:
    rows: list[list[str]] = []
    for row in root.iter(f"{_S}row"):
        cells: list[str] = []
        for cell in row.iter(f"{_S}c"):
            kind = cell.get("t")
            if kind == "inlineStr":
                value = "".join(t.text or "" for t in cell.iter(f"{_S}t"))
            else:
                value = cell.findtext(f"{_S}v") or ""
                if kind == "s" and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
            cells.append(value.strip())
        if any(cells):
            rows.append(cells)
    if not rows:
        return []
    header, *body = rows
    if not body:
        return [" | ".join(cell for cell in header if cell)]
    return [
        " | ".join(
            f"{name}: {value}" if name else value
            for name, value in zip(header, row, strict=False)
            if value
        )
        for row in body
    ]


def _opendocument(path: Path) -> tuple[str | None, str, datetime | None]:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("content.xml"))
        title = None
        if "meta.xml" in archive.namelist():
            meta = ElementTree.fromstring(archive.read("meta.xml"))
            node = next(meta.iter(f"{_DC}title"), None)
            title = (node.text or "").strip() or None if node is not None else None
    lines: list[str] = []
    for node in root.iter():
        if node.tag in (f"{_TEXT_NS}p", f"{_TEXT_NS}h"):
            lines.append("".join(node.itertext()))
        elif node.tag == f"{_TABLE_NS}table-row":
            lines.append(
                " | ".join(
                    "".join(cell.itertext()) for cell in node if "".join(cell.itertext()).strip()
                )
            )
    # Table cells hold paragraphs too; keep each line once, in order.
    return title, _joined(list(dict.fromkeys(line for line in lines if line.strip()))), None


def _epub(path: Path) -> tuple[str | None, str, datetime | None]:
    with zipfile.ZipFile(path) as archive:
        container = ElementTree.fromstring(archive.read("META-INF/container.xml"))
        rootfile = next(node for node in container.iter() if node.tag.endswith("rootfile"))
        package_path = rootfile.get("full-path", "")
        package = ElementTree.fromstring(archive.read(package_path))
        base = str(PurePosixPath(package_path).parent)
        title = next((node.text for node in package.iter() if node.tag == f"{_DC}title"), None)
        items = {
            node.get("id"): node.get("href", "")
            for node in package.iter()
            if node.tag.endswith("item")
        }
        chapters: list[str] = []
        for node in package.iter():
            if not node.tag.endswith("itemref"):
                continue
            href = items.get(node.get("idref"))
            if not href:
                continue
            member = str(PurePosixPath(base) / href) if base not in ("", ".") else href
            if member in archive.namelist():
                chapters.append(_html_text(_decode(archive.read(member)))[1])
    return title, "\n\n".join(chapter for chapter in chapters if chapter), None


def _pdf(path: Path) -> tuple[str | None, str, datetime | None]:
    from pypdf import PdfReader  # loaded only when a PDF is imported
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentError("it is protected by a password")
        pages = [page.extract_text() or "" for page in reader.pages]
    except PdfReadError as error:
        raise DocumentError(f"it is not a readable PDF: {error}") from error
    meta = reader.metadata
    title = (meta.title or "").strip() if meta is not None else ""
    created = meta.creation_date if meta is not None else None
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    text = "\n\n".join(page.strip() for page in pages if page.strip())
    return title or None, text, created


_READERS: dict[str, Callable[[Path], tuple[str | None, str, datetime | None]]] = {
    ".csv": _table,
    ".tsv": _table,
    ".json": _json,
    ".jsonl": _json,
    ".ndjson": _json,
    ".xml": _xml,
    ".html": _html,
    ".htm": _html,
    ".xhtml": _html,
    ".rtf": _rtf,
    ".docx": _docx,
    ".pptx": _pptx,
    ".xlsx": _xlsx,
    ".odt": _opendocument,
    ".odp": _opendocument,
    ".ods": _opendocument,
    ".epub": _epub,
    ".pdf": _pdf,
}
