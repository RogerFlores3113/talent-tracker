"""Is there a newer Talent Search? A daily look at GitHub's latest release: the one thing the app fetches itself.

It asks GitHub's public API for the newest release's version number and sends nothing about the person or their
candidates. The Settings switch turns it off (update_check). The packaged app checks in a background thread
(run_checks, started by cli.run_app), and pages only read the answer kept in settings, so none waits on GitHub.

Update now runs the same one-line installer the README gives, in its own process: it downloads the release,
checks it against SHA256SUMS.txt, closes this copy, replaces it and opens the new one. Only when this copy is
where the installer puts it; anywhere else the banner links to the release page instead.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .store import Store, now_iso

REPO = "RogerFlores3113/talent-tracker"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RAW = f"https://raw.githubusercontent.com/{REPO}/main"
CHECK_EVERY = timedelta(hours=24)

log = logging.getLogger(__name__)


def parse(version: str | None) -> tuple[int, ...] | None:
    """'v0.2.3' or '0.2.3' as (0, 2, 3); None for anything else."""
    parts = str(version or "").strip().removeprefix("v").split(".")
    return tuple(int(p) for p in parts) if parts and all(p.isdigit() for p in parts) else None


def newer(version: str | None, than: str = __version__) -> bool:
    a, b = parse(version), parse(than)
    return bool(a and b and a > b)


def enabled(store: Store) -> bool:
    return store.setting("update_check") != "0"


def fetch_latest(timeout: float = 5.0) -> str | None:
    """The newest release's version from GitHub, or None when it can't be reached."""
    req = urllib.request.Request(LATEST_API, headers={"Accept": "application/vnd.github+json",
                                                      "User-Agent": f"talent-search/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            tag = json.loads(r.read().decode("utf-8")).get("tag_name")
    except Exception as exc:  # offline, rate limited, GitHub down: try again tomorrow
        log.info("Could not check for a new version: %s", exc)
        return None
    return str(tag).removeprefix("v") if parse(tag) else None


def check(store: Store, fetch: Callable[[], str | None] | None = None) -> str | None:
    """Ask GitHub now and remember the answer. The time is kept even when it fails, so an offline computer isn't
    asked again on every start."""
    latest = (fetch or fetch_latest)()
    store.set_settings(update_checked_at=now_iso(), **({"update_latest": latest} if latest else {}))
    return latest


def due(store: Store, now: datetime | None = None) -> bool:
    last = store.setting("update_checked_at") or ""
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - when >= CHECK_EVERY


def run_checks(store: Store, *, every: float = 3600.0) -> threading.Thread:
    """The packaged app's daily check: a daemon thread that looks once an hour whether a day has passed."""
    def loop() -> None:
        while True:
            try:
                if enabled(store) and due(store):
                    check(store)
            except Exception:  # never take the app down over an update check
                log.exception("update check failed")
            time.sleep(every)
    t = threading.Thread(target=loop, name="update-check", daemon=True)
    t.start()
    return t


def notes_url(version: str) -> str:
    return f"https://github.com/{REPO}/releases/tag/v{version}"


def install_command(platform: str | None = None) -> str:
    """The README's one-line install, which also updates."""
    if (platform or sys.platform) == "win32":
        return f"irm {RAW}/install.ps1 | iex"
    return f'curl -fsSL {RAW}/install.sh -o "${{TMPDIR:-/tmp}}/talent-install.sh" && sh "${{TMPDIR:-/tmp}}/talent-install.sh"'


def installer_home(platform: str | None = None, env: dict[str, str] | None = None) -> Path | None:
    """Where the installer puts the app: the folder holding the executable (Windows, Linux) or the .app (Mac)."""
    platform = platform or sys.platform
    env = os.environ if env is None else env
    if platform == "win32":
        local = env.get("LOCALAPPDATA")
        return Path(local) / "Programs" / "Talent Search" if local else None
    if platform == "darwin":
        apps = Path("/Applications") if os.access("/Applications", os.W_OK) else Path.home() / "Applications"
        return apps / "Talent Search.app"
    return Path.home() / ".local" / "share" / "talent-search-app" / "Talent Search"


def can_update_here(executable: str | None = None, frozen: bool | None = None, platform: str | None = None) -> bool:
    """Update now works for the packaged app the installer put in its usual place (it replaces that copy)."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    home = installer_home(platform)
    if not frozen or home is None:
        return False
    try:
        return Path(executable or sys.executable).resolve().is_relative_to(home.resolve())
    except OSError:
        return False


def start_update(platform: str | None = None, popen: Callable[..., Any] = subprocess.Popen,
                 log_path: Path | None = None) -> None:
    """Run the installer in its own process, so it outlives this copy when the installer closes it."""
    platform = platform or sys.platform
    out = open(log_path, "ab") if log_path else subprocess.DEVNULL  # the child process writes to it
    outside = tempfile.gettempdir()  # never the app's own folder: Windows can't replace a folder a process stands in
    if platform == "win32":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", install_command(platform)],
              stdin=subprocess.DEVNULL, stdout=out, stderr=out, creationflags=flags, close_fds=True, cwd=outside)
    else:
        popen(["/bin/sh", "-c", install_command(platform)], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
              start_new_session=True, close_fds=True, cwd=outside)


def dismiss(store: Store, version: str) -> None:
    """Not now: hide the banner until a version newer than this one is out."""
    if parse(version):
        store.set_settings(update_dismissed=version)


def banner(store: Store) -> dict[str, Any] | None:
    """The quiet update line for Today and Settings, or None."""
    latest = store.setting("update_latest") or ""
    if not enabled(store) or not newer(latest) or latest == (store.setting("update_dismissed") or ""):
        return None
    return {"version": latest, "current": __version__, "notes_url": notes_url(latest),
            "can_update": can_update_here(), "command": install_command()}
