"""Shared test helpers."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


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
