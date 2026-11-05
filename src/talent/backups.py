"""The automatic backups in the data folder's backups/: list them, read what's in one, put one back.

App window only. The MCP server has no way to restore (or delete) anything.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .importers import backup_before_import
from .paths import backups_dir
from .store import Store

# backup_before_import names files "before-<reason>-YYYY-mm-dd-HHMMSS[-n].db".
NAME_RE = re.compile(r"^before-(?P<reason>[a-z0-9]+(?:-[a-z0-9]+)*?)-(?P<stamp>\d{4}-\d{2}-\d{2}-\d{6})(?:-\d+)?\.db$")
REASONS = {"import": "Before an import", "delete": "Before a delete", "merge": "Before a merge",
           "sample-removal": "Before removing sample data", "restore": "Before a restore"}
UNDO_KEY = "last_import_undo"  # settings row: which backup undoes the newest import, and the data right after it
LISTED = 60


def _reason(name: str) -> str:
    m = NAME_RE.match(name)
    reason = m["reason"] if m else ""
    return REASONS.get(reason) or ("Before " + reason.replace("-", " ") if reason else "Backup")


def _when(path: Path) -> datetime:
    m = NAME_RE.match(path.name)
    if m:
        try:
            return datetime.strptime(m["stamp"], "%Y-%m-%d-%H%M%S").astimezone()
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone()


def read_counts(path: Path) -> dict[str, Any]:
    """People and jobs in a backup file, read without changing it. ok is False for a file we can't read."""
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?immutable=1", uri=True)
    except sqlite3.Error:
        return {"ok": False}
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        if not {"candidates", "jobs", "settings"} <= tables:
            return {"ok": False}
        return {"ok": True, "people": conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
                "jobs": conn.execute("SELECT count(*) FROM jobs").fetchone()[0]}
    except sqlite3.Error:
        return {"ok": False}
    finally:
        conn.close()


def describe(path: Path) -> dict[str, Any]:
    when = _when(path)
    return {"name": path.name, "path": path, "why": _reason(path.name), "when": when.isoformat(),
            **read_counts(path)}


def list_backups(limit: int = LISTED) -> list[dict[str, Any]]:
    """The automatic backups, newest first, each with when, why and how many people and jobs it holds."""
    files = [f for f in backups_dir().iterdir() if f.is_file() and NAME_RE.match(f.name)]
    files.sort(key=lambda f: (_when(f), f.name), reverse=True)
    return [describe(f) for f in files[:limit]]


def backup_file(name: str) -> Path | None:
    """The backup with that file name, only if it's one of ours, straight inside the backups folder."""
    if not NAME_RE.match(name or ""):
        return None
    folder = backups_dir()
    path = folder / name
    return path if path.is_file() and path.resolve().parent == folder.resolve() else None


def restore(store: Store, name: str) -> dict[str, Any]:
    """Back up the data as it is now, then put the named backup in its place.

    Returns {"restored": describe(backup), "saved": Path of the copy taken first}."""
    path = backup_file(name)
    if path is None:
        raise ValueError("That backup isn't in the backups folder any more. Reload the page and pick another.")
    info = describe(path)
    if not info["ok"]:
        raise ValueError("That backup file can't be read, so nothing was changed. Pick another one.")
    saved = backup_before_import(store, reason="restore")
    try:
        store.restore_from(path)
    except sqlite3.Error as exc:
        raise ValueError(f"Couldn't restore that backup, so your data wasn't changed ({exc}).") from exc
    return {"restored": info, "saved": saved}


# ------------------------------------------------------------ undo an import
def content_digest(store: Store) -> str:
    """A fingerprint of everything the recruiter or the AI can change (not settings), in any row order.
    Settings, the search outbox and the full-text index (virtual tables and their shadow tables) don't count."""
    total, mod = 0, 1 << 128
    with store._lock:
        rows = store._conn.execute(
            "SELECT name, coalesce(sql, '') FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()
        virtual = [n for n, sql in rows if sql.upper().startswith("CREATE VIRTUAL TABLE")]
        skip = {"settings", "search_outbox", *virtual}
        tables = sorted(n for n, _ in rows if n not in skip and not any(n.startswith(v + "_") for v in virtual))
        for table in tables:
            for row in store._conn.execute(f'SELECT * FROM "{table}"'):
                h = hashlib.sha256(repr((table, tuple(row))).encode()).digest()
                total = (total + int.from_bytes(h[:16], "big")) % mod
    return f"{total:032x}"


def remember_import(store: Store, backup: str | Path) -> None:
    """After an import: note its backup and what the data looks like now, so Undo can tell if it changed since."""
    value = json.dumps({"backup": Path(backup).name, "digest": content_digest(store)})
    with store._lock, store._conn:
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (UNDO_KEY, value))


def import_undo_state(store: Store, name: str) -> str:
    """'ok' when Undo can put back the backup taken before this import, 'changed' when something else changed
    since, 'gone' when that backup or its record is no longer here."""
    row = store._row("SELECT value FROM settings WHERE key = ?", (UNDO_KEY,))
    try:
        rec = json.loads(row["value"]) if row else {}
    except ValueError:
        rec = {}
    if not rec or rec.get("backup") != name or backup_file(name) is None:
        return "gone"
    return "ok" if rec.get("digest") == content_digest(store) else "changed"
