"""Shared test helpers."""

from __future__ import annotations

import os
import uuid
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def falkordb_graph(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """With ``CHATLORE_STORE=falkordb``, give each test a graph of its own.

    That runs the whole suite against a FalkorDB server, as CI does.
    """
    if os.environ.get("CHATLORE_STORE") != "falkordb":
        yield
        return
    from chatlore.store import FALKORDB_DEFAULT_URL, FALKORDB_URL_ENV
    from chatlore.store.falkordb import FalkorDBStore

    graph = f"test_{uuid.uuid4().hex}"
    # Read now: a test may point the setting elsewhere, and it is still set here after.
    url = os.environ.get(FALKORDB_URL_ENV) or FALKORDB_DEFAULT_URL
    monkeypatch.setenv("CHATLORE_FALKORDB_GRAPH", graph)
    yield
    store = FalkorDBStore(url, graph)
    try:
        store.drop()
    finally:
        store.close()


@pytest.fixture
def fixtures() -> Path:
    """Directory holding the synthetic export fixtures."""
    return FIXTURES


def make_zip(target: Path, members: dict[str, Path | str]) -> Path:
    """Create a zip at ``target``. Values are files to copy in, or literal text."""
    with zipfile.ZipFile(target, "w") as archive:
        for name, source in members.items():
            if isinstance(source, Path):
                archive.write(source, name)
            else:
                archive.writestr(name, source)
    return target
