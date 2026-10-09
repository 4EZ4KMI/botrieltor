from __future__ import annotations

import os
from pathlib import Path


def default_data_dir() -> Path:
    """Return the per-user directory for BotRieltor's database and secrets."""
    override = os.environ.get("BOTRIELTOR_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        root = Path.home() / "Library" / "Application Support"
    return root / "BotRieltor"
