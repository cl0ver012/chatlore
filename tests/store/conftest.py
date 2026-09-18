"""Backends under test. Every contract test runs against each entry."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from chatlore.store import GraphStore, SQLiteStore


@pytest.fixture(params=["sqlite"])
def store(request: pytest.FixtureRequest) -> Iterator[GraphStore]:
    backend = SQLiteStore(":memory:")
    try:
        yield backend
    finally:
        backend.close()
