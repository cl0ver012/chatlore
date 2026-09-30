"""Private, temporary libraries for the visitors of a public server.

A visitor who brings their own export gets a library of their own, tied to
their browser by a random token kept in a cookie. Only the token's holder can
reach it: its folder is named after a hash of the token, never the token
itself, so the folder names on the server give nothing away. Each library is
deleted when it expires, 24 hours after it was made by default, or earlier when
its visitor deletes it.

A visitor's library always keeps its graph in a SQLite file in its folder, even
when the server's own library is on FalkorDB, so that deleting the folder
deletes everything.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chatlore.store import DATABASE_NAME, GraphStore, SQLiteStore

COOKIE = "chatlore_library"
"""The cookie holding a visitor's token."""
DEFAULT_KEEP = timedelta(hours=24)
_META = "space.json"
_TOKEN_LENGTH = 200


@dataclass(frozen=True, slots=True)
class Space:
    """One visitor's library."""

    id: str
    home: Path
    created_at: datetime
    expires_at: datetime

    def open_store(self) -> GraphStore:
        """The library's graph, in a SQLite file in its folder."""
        self.home.mkdir(parents=True, exist_ok=True)
        return SQLiteStore(self.home / DATABASE_NAME)


class Spaces:
    """The visitors' libraries under one folder."""

    def __init__(
        self,
        root: Path,
        keep: timedelta = DEFAULT_KEEP,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.root = root
        self.keep = keep
        self._clock = clock

    def create(self) -> tuple[str, Space]:
        """A new library and the token that reaches it."""
        token = secrets.token_urlsafe(32)
        created = self._clock()
        space = Space(_id(token), self.root / _id(token), created, created + self.keep)
        space.home.mkdir(parents=True, exist_ok=True)
        meta = {"created_at": created.isoformat(), "expires_at": space.expires_at.isoformat()}
        (space.home / _META).write_text(json.dumps(meta), encoding="utf-8")
        return token, space

    def find(self, token: str | None) -> Space | None:
        """The library a token reaches, unless it expired or was deleted."""
        if not token or len(token) > _TOKEN_LENGTH:
            return None
        space = self._read(self.root / _id(token))
        if space is None or space.expires_at <= self._clock():
            return None
        return space

    def expired(self) -> list[Space]:
        """Every library whose time is up, or whose details cannot be read."""
        if not self.root.exists():
            return []
        found: list[Space] = []
        for home in sorted(self.root.iterdir()):
            if not home.is_dir():
                continue
            space = self._read(home)
            if space is None:
                now = self._clock()
                found.append(Space(home.name, home, now, now))
            elif space.expires_at <= self._clock():
                found.append(space)
        return found

    def delete(self, space: Space) -> None:
        """Delete a library's folder with everything in it."""
        shutil.rmtree(space.home, ignore_errors=True)

    def _read(self, home: Path) -> Space | None:
        try:
            meta = json.loads((home / _META).read_text(encoding="utf-8"))
            return Space(
                home.name,
                home,
                datetime.fromisoformat(meta["created_at"]),
                datetime.fromisoformat(meta["expires_at"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None


def _id(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
