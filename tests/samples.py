"""Small documents of every kind ChatLore reads, written for tests."""

from __future__ import annotations

import zipfile
from pathlib import Path

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CORE = (
    '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/'
    'core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:dcterms="http://purl.org/dc/terms/"><dc:title>{title}</dc:title>'
    "<dcterms:created>2026-03-04T10:00:00Z</dcterms:created></cp:coreProperties>"
)


def _zip(path: Path, members: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return path


def docx(path: Path, title: str, paragraphs: list[str]) -> Path:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    return _zip(
        path,
        {
            "word/document.xml": f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>',
            "docProps/core.xml": CORE.format(title=title),
        },
    )


def pptx(path: Path, slides: list[list[str]]) -> Path:
    members = {}
    for number, lines in enumerate(slides, start=1):
        paragraphs = "".join(f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in lines)
        members[f"ppt/slides/slide{number}.xml"] = (
            f'<p:sld xmlns:p="x" xmlns:a="{A}"><p:txBody>{paragraphs}</p:txBody></p:sld>'
        )
    return _zip(path, members)


def xlsx(path: Path, sheet: str, rows: list[list[str]]) -> Path:
    shared: list[str] = []
    cells = []
    for number, row in enumerate(rows, start=1):
        values = []
        for value in row:
            shared.append(value)
            values.append(f'<c t="s"><v>{len(shared) - 1}</v></c>')
        cells.append(f'<row r="{number}">{"".join(values)}</row>')
    strings = "".join(f"<si><t>{value}</t></si>" for value in shared)
    return _zip(
        path,
        {
            "xl/workbook.xml": (
                f'<workbook xmlns="{S}" xmlns:r="{R}"><sheets>'
                f'<sheet name="{sheet}" sheetId="1" r:id="rId1"/></sheets></workbook>'
            ),
            "xl/_rels/workbook.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                'relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" '
                'Type="worksheet"/></Relationships>'
            ),
            "xl/sharedStrings.xml": f'<sst xmlns="{S}">{strings}</sst>',
            "xl/worksheets/sheet1.xml": (
                f'<worksheet xmlns="{S}"><sheetData>{"".join(cells)}</sheetData></worksheet>'
            ),
        },
    )


def odt(path: Path, title: str, paragraphs: list[str]) -> Path:
    body = "".join(f"<text:p>{text}</text:p>" for text in paragraphs)
    return _zip(
        path,
        {
            "content.xml": (
                '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:'
                'office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
                f"<office:body><office:text>{body}</office:text></office:body>"
                "</office:document-content>"
            ),
            "meta.xml": (
                '<office:document-meta xmlns:office="urn:oasis:names:tc:opendocument:xmlns:'
                'office:1.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><office:meta>'
                f"<dc:title>{title}</dc:title></office:meta></office:document-meta>"
            ),
        },
    )


def epub(path: Path, title: str, chapters: list[str]) -> Path:
    items = "".join(
        f'<item id="c{number}" href="c{number}.xhtml" media-type="application/xhtml+xml"/>'
        for number in range(len(chapters))
    )
    spine = "".join(f'<itemref idref="c{number}"/>' for number in range(len(chapters)))
    members = {
        "META-INF/container.xml": (
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>'
        ),
        "OEBPS/content.opf": (
            '<package xmlns="http://www.idpf.org/2007/opf" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<metadata><dc:title>{title}</dc:title></metadata>"
            f"<manifest>{items}</manifest><spine>{spine}</spine></package>"
        ),
    }
    for number, text in enumerate(chapters):
        members[f"OEBPS/c{number}.xhtml"] = f"<html><body><p>{text}</p></body></html>"
    return _zip(path, members)


def pdf(path: Path, title: str, lines: list[str]) -> Path:
    """A one-page PDF with text in Helvetica, its offsets worked out."""
    text = " ".join(
        f"BT /F1 12 Tf 72 {720 - 20 * number} Td ({line}) Tj ET"
        for number, line in enumerate(lines)
    )
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Title ({title}) >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 6 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    path.write_bytes(out)
    return path


def email(path: Path, subject: str, body: str, message_id: str = "<m1@example.com>") -> Path:
    path.write_text(
        f"From: Maya <maya@example.com>\nTo: Sam <sam@example.com>\nSubject: {subject}\n"
        f"Date: Tue, 03 Mar 2026 09:30:00 +0000\nMessage-ID: {message_id}\n"
        f"Content-Type: text/plain; charset=utf-8\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def mbox(path: Path, messages: list[tuple[str, str]]) -> Path:
    text = ""
    for number, (subject, body) in enumerate(messages):
        text += (
            "From maya@example.com Tue Mar  3 09:30:00 2026\n"
            f"From: maya@example.com\nSubject: {subject}\n"
            f"Message-ID: <m{number}@example.com>\n\n{body}\n\n"
        )
    path.write_text(text, encoding="utf-8")
    return path


CHATGPT = """[{"title": "Trip ideas", "create_time": 1767225600, "update_time": 1767225700,
"id": "c-1", "current_node": "b", "mapping": {
"a": {"id": "a", "parent": null, "children": ["b"], "message": {"id": "a",
"author": {"role": "user"}, "create_time": 1767225600,
"content": {"content_type": "text", "parts": ["Where should I go in spring?"]}}},
"b": {"id": "b", "parent": "a", "children": [], "message": {"id": "b",
"author": {"role": "assistant"}, "create_time": 1767225660,
"content": {"content_type": "text", "parts": ["Kyoto for the blossoms."]}}}}}]"""
