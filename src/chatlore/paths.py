"""Where ChatLore keeps its data."""

from __future__ import annotations

import os
from pathlib import Path


def default_home() -> Path:
    """Return the ChatLore data directory.

    Uses ``$CHATLORE_HOME`` when set, otherwise ``~/.chatlore``.
    """
    override = os.environ.get("CHATLORE_HOME")
    return Path(override).expanduser() if override else Path.home() / ".chatlore"
