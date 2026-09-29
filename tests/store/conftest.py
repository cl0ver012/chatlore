"""Backends under test. Every contract test runs against each entry.

FalkorDB needs a server: set ``CHATLORE_TEST_FALKORDB_URL`` to run against one,
for example ``redis://localhost:6379`` with ``docker run -p 6379:6379
falkordb/falkordb``. Each test gets a graph of its own, deleted afterwards.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

from chatlore.store import GraphStore, SQLiteStore

FALKORDB_URL = os.environ.get("CHATLORE_TEST_FALKORDB_URL")


@pytest.fixture(params=["sqlite", "falkordb"])
def store(request: pytest.FixtureRequest) -> Iterator[GraphStore]:
    if request.param == "sqlite":
        backend: GraphStore = SQLiteStore(":memory:")
        try:
            yield backend
        finally:
            backend.close()
        return
    if not FALKORDB_URL:
        pytest.skip("set CHATLORE_TEST_FALKORDB_URL to test against FalkorDB")
    from chatlore.store.falkordb import FalkorDBStore

    falkordb = FalkorDBStore(FALKORDB_URL, f"test_{uuid.uuid4().hex}")
    try:
        yield falkordb
    finally:
        falkordb.drop()
        falkordb.close()
