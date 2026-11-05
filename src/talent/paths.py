"""Where the app keeps its data.

Packaged app: the platform's per-user data folder
  (macOS: ~/Library/Application Support/Talent Search,
   Windows: %LOCALAPPDATA%\\Talent Search).
Development:  ./data, or TALENT_DATA_DIR if set.
"""
from __future__ import annotations

import hmac
import os
import secrets
import sys
import time
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


# ------------------------------------------------------------ app window key
# The app window is locked with a per-install key so other programs and other
# accounts on this computer can't read it. The key lives in a 0600 file in the
# data folder and reaches the browser only as an HttpOnly cookie. The launch URL
# carries a one-time code instead of the key, so the key never lands in browser
# history, logs, the port file or the AI app's links.
LAUNCH_CODE_TTL = 300  # seconds a launch code works


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    _private(path, 0o600)


def app_key() -> str:
    """This install's secret for the app window, created on first run."""
    path = data_dir() / "app.key"
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError:
        key = ""
    if len(key) < 32:
        key = secrets.token_urlsafe(32)
        _write_private(path, key)
    elif path.stat().st_mode & 0o077:
        _private(path, 0o600)
    return key


def new_launch_code() -> str:
    """A one-time code for the launch URL. Only the newest one works, once, for a few minutes."""
    code = secrets.token_urlsafe(24)
    _write_private(data_dir() / "launch.code", code)
    return code


def use_launch_code(code: str) -> bool:
    """True (and the code is used up) when it's the current, unexpired launch code."""
    path = data_dir() / "launch.code"
    try:
        current = path.read_text(encoding="utf-8").strip()
        fresh = time.time() - path.stat().st_mtime < LAUNCH_CODE_TTL
    except OSError:
        return False
    if not (code and current and hmac.compare_digest(code.encode(), current.encode())):
        return False
    path.unlink(missing_ok=True)
    return fresh


def launch_url(port: int | None = None) -> str:
    """Opens the app window and unlocks it. Never log or store this address."""
    base = f"http://127.0.0.1:{port}" if port else app_url("").rstrip("/")
    return f"{base}/launch?code={new_launch_code()}"
