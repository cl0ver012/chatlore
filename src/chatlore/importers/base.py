"""Shared building blocks for importers.

An importer turns one vendor's export into canonical ``Conversation`` objects.
It never writes anything and never modifies its input. A record that cannot be
parsed is reported through ``on_issue`` and skipped, so one odd conversation
does not abort an import of thousands.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from chatlore.models import Conversation, SourceKind


class ImporterError(Exception):
    """The export as a whole could not be read."""


@dataclass(frozen=True, slots=True)
class ImportIssue:
    """One record that was skipped, and why."""

    record: str
    reason: str


IssueSink = Callable[[ImportIssue], None]


class Importer(Protocol):
    """What every importer provides."""

    kind: SourceKind

    def parse(self, path: Path, on_issue: IssueSink | None = None) -> Iterator[Conversation]:
        """Yield every conversation found at ``path``."""
        ...


def report(on_issue: IssueSink | None, record: str, reason: str) -> None:
    """Send an issue to the sink when there is one."""
    if on_issue is not None:
        on_issue(ImportIssue(record=record, reason=reason))


def read_candidates(path: Path, filename: str) -> Iterator[tuple[str, bytes]]:
    """Yield ``(display_name, content)`` for every file called ``filename`` under ``path``.

    ``path`` may be the file itself, a directory that contains it at any depth,
    or a zip archive. Candidates closest to the root come first.
    """
    if not path.exists():
        raise ImporterError(f"{path} does not exist")

    if path.is_dir():
        matches = sorted(path.rglob(filename), key=lambda p: (len(p.parts), str(p)))
        for match in matches:
            yield str(match), match.read_bytes()
        return

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if PurePosixPath(n).name == filename]
            names.sort(key=lambda n: (n.count("/"), n))
            for name in names:
                yield f"{path}!{name}", archive.read(name)
        return

    yield str(path), path.read_bytes()


def load_json(display_name: str, content: bytes) -> Any:
    """Decode JSON bytes, turning decode failures into an ``ImporterError``."""
    try:
        return json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImporterError(f"{display_name} is not valid JSON: {error}") from error


def load_first_json(path: Path, filename: str) -> Any:
    """Load the first file called ``filename`` found at ``path``."""
    for display_name, content in read_candidates(path, filename):
        return load_json(display_name, content)
    raise ImporterError(f"no {filename} found in {path}")


def from_epoch(value: Any) -> datetime | None:
    """Convert seconds since the epoch to an aware UTC datetime."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def from_iso(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp, returning ``None`` when it cannot be parsed."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def as_str(value: Any) -> str | None:
    """Return ``value`` when it is a non-empty string."""
    return value if isinstance(value, str) and value else None
