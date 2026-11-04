"""Where the app keeps its data.

Packaged app: the platform's per-user data folder
  (macOS: ~/Library/Application Support/Talent Search,
   Windows: %LOCALAPPDATA%\\Talent Search).
Development:  ./data, or TALENT_DATA_DIR if set.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "Talent Search"
APP_SLUG = "talent-search"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_dir() -> Path:
    override = os.environ.get("TALENT_DATA_DIR")
    if override:
        base = Path(override)
    elif is_frozen():
        from platformdirs import user_data_dir

        base = Path(user_data_dir(APP_NAME, appauthor=False))
    else:
        base = Path("data")
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return data_dir() / "talent.db"


def resource_dir() -> Path:
    """Folder holding templates/ and static/ (inside the bundle when frozen)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS")) / "talent"
    return Path(__file__).parent
