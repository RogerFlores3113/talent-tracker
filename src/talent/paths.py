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


def _private(path: Path, mode: int) -> None:
    """Owner-only access where the OS has POSIX permissions (not Windows)."""
    if os.name == "nt":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def private_dir(path: Path, tighten: bool = True) -> Path:
    """Make a folder only this user can open. tighten: also fix one that already exists."""
    existed = path.exists()
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not existed or (tighten and path.stat().st_mode & 0o077):
        _private(path, 0o700)
    return path


def data_dir() -> Path:
    override = os.environ.get("TALENT_DATA_DIR")
    if override:
        base = Path(override)
    elif is_frozen():
        from platformdirs import user_data_dir

        base = Path(user_data_dir(APP_NAME, appauthor=False))
    else:
        base = Path("data")
    # A folder someone pointed us at with TALENT_DATA_DIR is only locked down if we create it.
    return private_dir(base, tighten=not override)


def db_path() -> Path:
    """The database file, created owner-only (0600) before SQLite opens it."""
    path = data_dir() / "talent.db"
    if os.name != "nt":
        if not path.exists():
            try:
                os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))  # empty file = new SQLite database
            except OSError:
                pass
        elif path.stat().st_mode & 0o077:
            _private(path, 0o600)
    return path


def backups_dir() -> Path:
    """Automatic backups (before every import) live here, inside the data folder."""
    return private_dir(data_dir() / "backups")


def imports_dir() -> Path:
    """Uploads held between the column-matching step and the import itself."""
    return private_dir(data_dir() / "imports")


def resource_dir() -> Path:
    """Folder holding templates/ and static/ (inside the bundle when frozen)."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS")) / "talent"
    return Path(__file__).parent


DEFAULT_PORT = 8765


def port_file() -> Path:
    return data_dir() / "app.port"


def app_url(path: str = "/") -> str:
    """Where the app window is (or will be) served. The app records its port when it starts."""
    try:
        port = int(port_file().read_text().strip())
    except (OSError, ValueError):
        port = DEFAULT_PORT
    return f"http://127.0.0.1:{port}{path}"
