"""Importer for Gemini activity from Google Takeout.

Takeout (takeout.google.com > My Activity > Gemini Apps, JSON format) produces
``MyActivity.json``: a flat list with one entry per prompt. An entry has the
prompt in ``title`` (prefixed with "Prompted " in English exports), a ``time``,
and usually the response as HTML in ``safeHtmlItem``.

The export has no conversation ids, so conversations cannot be reconstructed
exactly. Entries are grouped into sessions instead: consecutive prompts less
than ``SESSION_GAP`` apart become one conversation. This is a heuristic and is
documented as such.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from chatlore.ids import conversation_id, message_id
from chatlore.importers.base import (
    ImporterError,
    IssueSink,
    as_str,
    from_iso,
    load_json,
    read_candidates,
    report,
)
from chatlore.models import ContentPart, Conversation, Message, Role, SourceKind

SESSION_GAP = timedelta(minutes=30)
_PROMPT_PREFIX = "Prompted "
_TITLE_LENGTH = 80


class GeminiImporter:
    """Parses ``MyActivity.json`` for Gemini Apps from a Takeout zip, folder, or file."""

    kind = SourceKind.GEMINI

    def parse(self, path: Path, on_issue: IssueSink | None = None) -> Iterator[Conversation]:
        entries = _load_entries(path)
        turns: list[_Turn] = []
        for index, raw in enumerate(entries):
            turn = _turn(raw)
            if isinstance(turn, str):
                report(on_issue, f"activity #{index}", turn)
            elif turn is not None:
                turns.append(turn)

        turns.sort(key=lambda t: t.time)
        for session in _sessions(turns):
            try:
                yield _conversation(session)
            except ValidationError as error:
                report(on_issue, session[0].prompt[:_TITLE_LENGTH], str(error).splitlines()[0])


class _Turn:
    __slots__ = ("prompt", "response", "time")

    def __init__(self, time: datetime, prompt: str, response: str) -> None:
        self.time = time
        self.prompt = prompt
        self.response = response


def _load_entries(path: Path) -> list[Any]:
    if path.suffix.lower() in {".html", ".htm"}:
        raise ImporterError(
            "The HTML activity format is not supported. "
            "In Google Takeout choose JSON as the My Activity format and export again."
        )

    found_any = False
    for display_name, content in read_candidates(path, "MyActivity.json"):
        found_any = True
        data = load_json(display_name, content)
        if isinstance(data, list) and any(_is_gemini(entry) for entry in data):
            return data
    if found_any:
        raise ImporterError(f"no Gemini Apps activity found in {path}")
    raise ImporterError(f"no MyActivity.json found in {path}")


def _is_gemini(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    header = str(entry.get("header", ""))
    products = entry.get("products")
    names = [str(p) for p in products] if isinstance(products, list) else []
    return "Gemini" in header or any("Gemini" in name for name in names)


def _turn(raw: Any) -> _Turn | str | None:
    """Return a turn, a reason it was skipped, or ``None`` for non-Gemini entries."""
    if not _is_gemini(raw):
        return None
    time = from_iso(raw.get("time"))
    if time is None:
        return "entry has no usable time"
    if time.tzinfo is None:
        time = time.astimezone()

    title = as_str(raw.get("title")) or ""
    response = _response_text(raw.get("safeHtmlItem"))
    if title.startswith(_PROMPT_PREFIX):
        prompt = title[len(_PROMPT_PREFIX) :]
    elif response:
        # Non-English exports localise the prefix; with a response present the
        # whole title is still the best available prompt text.
        prompt = title
    else:
        return None  # feedback, settings changes, and other non-prompt activity
    if not prompt.strip():
        return "prompt is empty"
    return _Turn(time=time, prompt=prompt.strip(), response=response)


def _response_text(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    texts = [
        html_to_text(html)
        for item in items
        if isinstance(item, dict) and (html := as_str(item.get("html")))
    ]
    return "\n\n".join(text for text in texts if text)


def _sessions(turns: list[_Turn]) -> Iterator[list[_Turn]]:
    session: list[_Turn] = []
    for turn in turns:
        if session and turn.time - session[-1].time > SESSION_GAP:
            yield session
            session = []
        session.append(turn)
    if session:
        yield session


def _conversation(session: list[_Turn]) -> Conversation:
    external_id = session[0].time.isoformat()
    conv_id = conversation_id(SourceKind.GEMINI, external_id)

    messages: list[Message] = []
    previous: str | None = None
    for turn in session:
        stamp = turn.time.isoformat()
        user = Message(
            id=message_id(conv_id, f"{stamp}:user"),
            parent_id=previous,
            role=Role.USER,
            created_at=turn.time,
            content=[ContentPart(text=turn.prompt)],
        )
        messages.append(user)
        previous = user.id
        if turn.response:
            reply = Message(
                id=message_id(conv_id, f"{stamp}:assistant"),
                parent_id=previous,
                role=Role.ASSISTANT,
                created_at=turn.time,
                content=[ContentPart(text=turn.response)],
            )
            messages.append(reply)
            previous = reply.id

    first_line = session[0].prompt.splitlines()[0]
    title = (
        first_line if len(first_line) <= _TITLE_LENGTH else first_line[: _TITLE_LENGTH - 1] + "…"
    )
    return Conversation(
        id=conv_id,
        source=SourceKind.GEMINI,
        external_id=external_id,
        title=title,
        created_at=session[0].time,
        updated_at=session[-1].time,
        messages=messages,
        metadata={"grouping": "session", "session_gap_minutes": SESSION_GAP.seconds // 60},
    )


_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "tr", "table", "pre", "blockquote", "hr"}
    | {f"h{level}" for level in range(1, 7)}
)
_SKIPPED_TAGS = frozenset({"script", "style"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skipping += 1
        elif tag == "li":
            self.chunks.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _BLOCK_TAGS and tag != "li":
            self.chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.chunks.append(data)


def html_to_text(html: str) -> str:
    """Reduce response HTML to readable plain text, one block per line."""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = [" ".join(line.split()) for line in "".join(extractor.chunks).splitlines()]
    collapsed: list[str] = []
    for line in lines:
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    return "\n".join(collapsed).strip()
