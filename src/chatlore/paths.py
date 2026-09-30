"""Where ChatLore keeps its data."""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "CHATLORE_HOME"


def default_home() -> Path:
    """Return the ChatLore data directory.

    Uses ``$CHATLORE_HOME`` when set, otherwise ``~/.chatlore``.
    """
    override = os.environ.get(HOME_ENV)
    return Path(override).expanduser() if override else Path.home() / ".chatlore"


def spaces_home() -> Path:
    """Return where a public server keeps its visitors' own libraries."""
    return Path.home() / ".chatlore-spaces"


def demo_home() -> Path:
    """Return where ``chatlore demo`` puts its made-up library, apart from the real one."""
    return Path.home() / ".chatlore-demo"
