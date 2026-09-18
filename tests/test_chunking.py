"""Tests for the chunker."""

from __future__ import annotations

import pytest

from chatlore.chunking import CHARS_PER_TOKEN, chunk_conversation, chunk_message
from chatlore.models import ContentPart, Conversation, Message, PartType, Role, SourceKind


def _message(*parts: ContentPart, role: Role = Role.ASSISTANT, id_: str = "m1") -> Message:
    return Message(id=id_, role=role, content=list(parts))


def _text(text: str) -> ContentPart:
    return ContentPart(text=text)


def test_short_message_is_one_chunk() -> None:
    chunks = chunk_message(_message(_text("Add an index on customer_id.")), "conv")

    assert [c.text for c in chunks] == ["Add an index on customer_id."]
    assert (chunks[0].message_id, chunks[0].conversation_id, chunks[0].order) == ("m1", "conv", 0)
    assert chunks[0].token_estimate == len("Add an index on customer_id.") // CHARS_PER_TOKEN


def test_paragraphs_are_packed_up_to_the_budget_and_never_split() -> None:
    paragraphs = [f"Paragraph {i} " + "word " * 30 for i in range(6)]
    message = _message(_text("\n\n".join(paragraphs)))

    chunks = chunk_message(message, "conv", max_tokens=100, overlap_tokens=0)

    assert len(chunks) > 1
    assert all(len(c.text) <= 100 * CHARS_PER_TOKEN for c in chunks)
    for paragraph in paragraphs:
        assert sum(paragraph.strip() in c.text for c in chunks) == 1
    assert [c.order for c in chunks] == list(range(len(chunks)))


def test_short_prose_tail_is_repeated_as_overlap() -> None:
    lead = "Lead paragraph. " + "filler " * 40
    bridge = "This short sentence links both halves."
    rest = "Second half. " + "filler " * 40
    message = _message(_text(f"{lead}\n\n{bridge}\n\n{rest}"))

    chunks = chunk_message(message, "conv", max_tokens=90, overlap_tokens=20)

    assert len(chunks) == 2
    assert chunks[0].text.endswith(bridge)
    assert chunks[1].text.startswith(bridge)


def test_fenced_code_stays_whole_and_is_not_used_as_overlap() -> None:
    code = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(12)) + "\n```"
    text = "Before the code. " + "intro " * 45 + f"\n\n{code}\n\nAfter the code. " + "outro " * 45
    message = _message(_text(text))

    chunks = chunk_message(message, "conv", max_tokens=100, overlap_tokens=60)

    holders = [c for c in chunks if "line_0 = 0" in c.text]
    assert len(holders) == 1
    assert "line_11 = 11" in holders[0].text
    assert holders[0].text.count("```") == 2


def test_code_parts_are_rendered_as_fenced_blocks() -> None:
    message = _message(
        _text("Use this:"), ContentPart(type=PartType.CODE, language="sql", text="SELECT 1;")
    )

    chunks = chunk_message(message, "conv")

    assert chunks[0].text == "Use this:\n\n```sql\nSELECT 1;\n```"


def test_oversized_units_are_broken_at_lines_or_sentences() -> None:
    big_code = "```\n" + "\n".join(f"row {i}: " + "x" * 40 for i in range(60)) + "\n```"
    long_prose = " ".join(f"Sentence number {i} goes here." for i in range(80))
    one_line = "y" * 1000

    for part in (_text(big_code), _text(long_prose), _text(one_line)):
        chunks = chunk_message(_message(part), "conv", max_tokens=50, overlap_tokens=0)
        assert len(chunks) > 1
        assert all(0 < len(c.text) <= 50 * CHARS_PER_TOKEN for c in chunks)
    rebuilt = "".join(
        c.text
        for c in chunk_message(_message(_text(one_line)), "c", max_tokens=50, overlap_tokens=0)
    )
    assert rebuilt == one_line


def test_unbalanced_fences_do_not_lose_text() -> None:
    message = _message(_text("Intro paragraph.\n\n```python\nprint(1)\n\nno closing fence here"))

    chunks = chunk_message(message, "conv")

    joined = "\n".join(c.text for c in chunks)
    assert "Intro paragraph." in joined
    assert "print(1)" in joined
    assert "no closing fence here" in joined


def test_tool_output_and_machine_text_are_not_chunked() -> None:
    tool_message = _message(_text("raw tool output"), role=Role.TOOL)
    system_message = _message(_text("system prompt"), role=Role.SYSTEM)
    mixed = _message(
        _text("Here is what I found."),
        ContentPart(type=PartType.OTHER, text="[tool result]\n" + "scraped page " * 500),
    )

    assert chunk_message(tool_message, "conv") == []
    assert chunk_message(system_message, "conv") == []
    assert [c.text for c in chunk_message(mixed, "conv")] == ["Here is what I found."]


def test_empty_messages_yield_nothing() -> None:
    assert chunk_message(_message(_text("   \n\n  ")), "conv") == []
    assert chunk_message(_message(), "conv") == []


def test_ids_are_stable_and_depend_on_the_text() -> None:
    first = chunk_message(_message(_text("same text")), "conv")
    again = chunk_message(_message(_text("same text")), "conv")
    changed = chunk_message(_message(_text("different text")), "conv")

    assert first[0].id == again[0].id
    assert first[0].id != changed[0].id
    assert first[0].id.startswith("chunk_")


def test_invalid_sizes_are_rejected() -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        chunk_message(_message(_text("x")), "conv", max_tokens=10, overlap_tokens=10)
    with pytest.raises(ValueError, match="max_tokens"):
        chunk_message(_message(_text("x")), "conv", max_tokens=0)


def test_conversation_chunks_every_branch() -> None:
    conversation = Conversation(
        id="conv",
        source=SourceKind.CHATGPT,
        external_id="x",
        messages=[
            _message(_text("question"), role=Role.USER, id_="q"),
            Message(id="old", parent_id="q", role=Role.ASSISTANT, content=[_text("old answer")]),
            Message(id="new", parent_id="q", role=Role.ASSISTANT, content=[_text("new answer")]),
        ],
        current_leaf_id="new",
    )

    chunks = chunk_conversation(conversation)

    assert [c.message_id for c in chunks] == ["q", "old", "new"]
    assert {c.conversation_id for c in chunks} == {"conv"}
